# -*- coding: utf-8 -*-

import os
import numpy as np
import matplotlib.pyplot as plt
from pydub import AudioSegment, effects
import argparse
from scipy import signal, linalg

# https://en.wikipedia.org/wiki/Surround_sound
CHANNELS = ['FL', 'FR', 'FC', 'BL', 'BR', 'SL', 'SR']

# Each channel, left and right
IR_ORDER = []
for _ch in CHANNELS:
    IR_ORDER.append(_ch+'-left')
    IR_ORDER.append(_ch+'-right')

# See README for details how these were obtained
# Two samples (@48kHz) added to beginning
DELAYS = {
    'FL': 0.1487,
    'FR': 0.1487,
    'FC': 0.2557,
    'BL': 0.1487,  # TODO: Confirm this
    'BR': 0.1487,  # TODO: Confirm this
    'SL': 0.0417,
    'SR': 0.0417,
}


def fft(x, fs):
    nfft = len(x)
    df = fs / nfft
    f = np.arange(0, fs - df, df)
    X = np.fft.fft(x)
    X_mag = 20 * np.log10(np.abs(X))
    return f[0:int(np.ceil(nfft/2))], X_mag[0:int(np.ceil(nfft/2))]


def to_float(x):
    """Normalizes numpy array into range -1..1

    Args:
        x: Numpy array

    Returns:
        Numpy array with values in range -1..1
    """
    if type(x) != np.array:
        x = np.array(x)
    dtype = x.dtype
    x = x.astype('float64')
    if dtype == 'int32':
        x /= 2.0 ** 31
    if dtype == 'int16':
        x /= 2.0 ** 15
    elif dtype == 'uint8':
        x /= 2.0 ** 8
        x *= 2.0
        x -= 1.0
    return x


def plot_sweep(x, fs, name=None):
    plt.plot(np.arange(0, len(x) / fs, 1 / fs), x)
    plt.xlim([0.0, len(x) / fs])
    plt.ylim([-1.0, 1.0])
    plt.ylabel('Amplitude')
    if name is not None:
        plt.legend([name])


def split_recording(recording, test_signal, speakers, silence_length=2.0):
    """Splits sine sweep recording into individual speaker-ear pairs

    Recording looks something like this (stereo only in this example):
    --/\/\/\----/\/\/\--
    ---/\/\/\--/\/\/\---
    There are two tracks, one for each ear. Dashes represent silence and sawtooths recorded signal. First saw tooths
    of both tracks are signal played on left front speaker and the second ones are signal played of right front speaker.

    There can be any (even) number of tracks. Output will be two tracks per speaker, left ear first and then right.
    Speakers are in the same order as in the original file, read from left to right and top to bottom. In the example
    above the output track order would be:
    1. Front left speaker - left ear
    2. Front left speaker - right ear
    3. Front right speaker - left ear
    4. Front right speaker - right ear

    Args:
        recording: AudioSegment for the sine sweep recording
        test_signal: AudioSegment for the test signal
        speakers: Speaker order as a list of strings
        silence_length: Length of silence in the beginning, end and between test signals in seconds

    Returns:

    """
    # Number of speakers in each track
    n_speakers = len(speakers) // (recording.channels // 2)

    # Split sections in time to columns
    columns = []
    ms = silence_length*1000 + len(test_signal)
    remainder = recording[silence_length*1000:]
    for _ in range(n_speakers):
        columns.append(remainder[:ms].split_to_mono())  # Add list of mono channel AudioSegments
        remainder = remainder[ms:]  # Cut to the beginning of next signal

    # Split each track by columns
    tracks = []
    i = 0
    while i < recording.channels:
        for column in columns:
            tracks.append(column[i])  # Left ear of current speaker
            tracks.append(column[i+1])  # Right ear of current speaker
        i += 2

    return AudioSegment.from_mono_audiosegments(*tracks)


def crop_ir_tail(response):
    """Crops out silent tail of an impulse response.

    Args:
        response: AudioSegment for the impulse response

    Returns:
        Cropped AudioSegment
    """
    # Find highest peak
    data = np.array(response.get_array_of_samples())

    # Sliding window RMS
    rms = []
    n = response.frame_rate // 1000  # Window size
    for j in range(1, len(response)):
        window = data[(j - 1) * n:j * n]  # Select window
        window = to_float(window)  # Normalize between -1..1
        rms.append(np.sqrt(np.mean(np.square(window))))  # RMS

    # Detect noise floor
    # 10 dB above RMS at the end (last 10 windows) or -60 dB, whichever is greater
    noise_floor = np.max([np.mean(rms[-10:-1]) * 10, 10.0 ** (-60.0 / 10.0)])

    # Remove tail below noise floor
    end = -1
    for j in range(len(rms) - 1, 1, -1):
        if rms[j] > noise_floor:
            break
        end = j

    # Return cropped. Delay before peak and to "silence"
    return response[:end]


def crop_ir_head(left, right, speaker):
    """Crops out silent head of left and right ear impulse responses and sets delay to correct value according to
    speaker channel

    Args:
        left: AudioSegment for left ear impulse response
        right: AudioSegment for right ear impulse response
        speaker: Speaker channel name

    Returns:
        Cropped left and right AudioSegments as a tuple (left, right)
    """
    # Peaks
    peak_left, _ = signal.find_peaks(to_float(np.array(left.normalize().get_array_of_samples())), height=0.1)
    peak_left = peak_left[0] / left.frame_rate * 1000
    peak_right, _ = signal.find_peaks(to_float(np.array(right.normalize().get_array_of_samples())), height=0.1)
    peak_right = peak_right[0] / right.frame_rate * 1000
    # Inter aural time difference
    itd = np.abs(peak_left - peak_right)

    # Speaker channel delay
    delay = DELAYS[speaker]
    if peak_left < peak_right:
        # Left side speaker
        if speaker[1] == 'R':
            raise ValueError(speaker, 'impulse response has lower delay to left ear than to right.')
        left = left[peak_left-delay:]
        right = right[peak_right-(delay+itd):]
    else:
        # Right side speaker
        if speaker[1] == 'L':
            raise ValueError(speaker, 'impulse response has lower delay to right ear than to left.')
        left = left[peak_left-(delay+itd):]
        right = right[peak_right-delay:]

    # Make sure impulse response starts from silence
    left = left.fade_in(2/left.frame_rate/1000)
    right = right.fade_in(2/right.frame_rate/1000)

    # Crop to integer number of samples
    left = left[:(int(len(left) / 1000 * left.frame_rate) - 1) / left.frame_rate * 1000]
    right = right[:(int(len(right) / 1000 * right.frame_rate) - 1) / right.frame_rate * 1000]

    return left, right


def zero_pad(seg, max_samples, fs):
    seg_samples = np.array(seg.get_array_of_samples())
    silence_length = max_samples - len(seg_samples)
    silence = np.zeros(silence_length, dtype=seg_samples.dtype)
    zero_padded = np.concatenate([seg_samples, silence])
    return AudioSegment(
        zero_padded.tobytes(),
        sample_width=seg.sample_width,
        frame_rate=fs,
        channels=1
    )


def deconv(y, x, domain='time'):
    """Calculates deconvolution in frequency or time domain.

    Args:
        y: Recording as numpy array
        x: Test signal as numpy array
        domain: "time" or "frequency"

    Returns:
        Impulse response
    """
    if domain == 'frequency':
        # Division in frequency domain is deconvolution in time domain
        X = np.fft.fft(x)
        Y = np.fft.fft(y)
        H = Y / X
        h = np.fft.ifft(H)
        h = np.real(h)
    elif domain == 'time':
        # Toepliz convolution: https://en.wikipedia.org/wiki/Toeplitz_matrix
        # Create zero padded matrix where each column is shifted one step down
        X = linalg.toeplitz(x, np.concatenate((x[0:1], np.zeros(len(x) - 1))))
        h = np.matmul(np.matmul(np.linalg.inv(np.matmul(np.transpose(X), X)), np.transpose(X)), y)
        # h = linalg.solve_toeplitz(
        #     (x, np.concatenate((x[0:1], np.zeros(len(x) - 1)))),
        #     y
        # )
    else:
        raise ValueError('"{}" is not one of the supported "domain" parameter values "time" or "frequency".')
    return h


def main(measure=False,
         preprocess=False,
         deconvolve=False,
         postprocess=False,
         equalize=False,
         recording=None,
         test=None,
         responses=None,
         speakers=None,
         silence_length=None,
         headphones=None):
    """"""
    if measure:
        raise NotImplementedError('Measurement is not yet implemented.')

    # Parameter checks
    if preprocess or postprocess:
        if not speakers:
            raise TypeError('Parameter "speakers" is required for pre-processing.')
    if preprocess:
        if not (recording or measure):
            raise TypeError('Parameter "recording" is required for pre-processing when not measuring.')
        if not test:
            raise TypeError('Parameter "test" is required for pre-processing.')
        if not silence_length:
            raise TypeError('Parameter "silence_length" is required for pre-processing.')
    if deconvolve:
        if not recording:
            raise TypeError('Parameter "recording" is required for deconvolution.')
        if not test:
            raise TypeError('Parameter "test" is required for deconvolution.')
    if postprocess:
        if not (responses or deconvolve):
            raise TypeError('Parameter "responses" is required for post-processing when not doing deconvolution.')
    if equalize:
        if not headphones:
            raise TypeError('Parameter "headphones" is required for equalization.')

    # Read files
    if preprocess and recording is not None:
        recording = AudioSegment.from_wav(recording)
    if (preprocess or deconvolve) and test is not None:
        test = AudioSegment.from_wav(test)
    if postprocess and responses is not None:
        responses = AudioSegment.from_wav(responses)

    if not os.path.isdir('out'):
        # Output directory does not exist, create it
        os.makedirs('out', exist_ok=True)

    # Logarithmic sine sweep measurement
    if measure:  # TODO
        raise NotImplementedError('Measurement is not yet implemented.')

    # Pre-processing
    if preprocess:
        # Split recording WAV file into individual mono tracks
        sweeps = split_recording(recording, test, speakers=speakers, silence_length=silence_length)

        # Reorder tracks
        sweeps = sweeps.split_to_mono()
        sweep_order = []
        for speaker in speakers:
            sweep_order.append(speaker + '-left')
            sweep_order.append(speaker + '-right')
        reordered = []
        for ch in IR_ORDER:
            if ch not in sweep_order:
                reordered.append(AudioSegment.silent(len(sweeps[0]), sweeps[0].frame_rate))
            else:
                reordered.append(sweeps[sweep_order.index(ch)])
        preprocessed = AudioSegment.from_mono_audiosegments(*reordered)

        # Normalize to -0.1 dB
        preprocessed = preprocessed.normalize()

        # # Plot waveforms for inspection
        # data = np.vstack([tr.get_array_of_samples() for tr in sweeps.split_to_mono()])
        # for j in range(data.shape[0]):
        #     plt.subplot(data.shape[0], 1, j + 1)
        #     plot_sweep(
        #         normalize(data[j, :]),
        #         recording.frame_rate,
        #         name=IR_ORDER[j]
        #     )
        # plt.xlabel('Time (s)')
        # plt.show()

        # Write multi-channel WAV file with sine sweeps
        preprocessed.export('out/preprocessed.wav', format='wav')
        # Write multi-channel WAV file with test track duplicated. Useful for Voxengo deconvolver.
        test_duplicated = [test for _ in range(preprocessed.channels)]
        AudioSegment.from_mono_audiosegments(*test_duplicated).export('out/tests.wav', format='wav')

    # Deconvolution
    if deconvolve:  # TODO
        # Make sure sampling rates match
        if preprocessed.frame_rate != test.frame_rate:
            raise ValueError('Sampling frequencies of test tone and recording must match!')

        # Pad test signal to pre-processed recording length with zeros
        test_padded = zero_pad(
            test,
            len(preprocessed.split_to_mono()[0].get_array_of_samples()),
            preprocessed.frame_rate
        )

        # Fourier transform of test signal
        x = np.array(test_padded.get_array_of_samples(), dtype='float32')

        responses = []
        for lines in preprocessed.split_to_mono():
            if lines.rms > 2:
                # Do deconvolution

                h = deconv(
                    np.array(lines.get_array_of_samples(), dtype='float32'),
                    x,
                    domain='frequency'
                )

                # Add to responses
                responses.append(AudioSegment(
                    np.multiply(h, 2**31).astype('int32').tobytes(),
                    frame_rate=preprocessed.frame_rate,
                    sample_width=4,
                    channels=1
                ))
            else:
                responses.append(lines)
        responses = AudioSegment.from_mono_audiosegments(*responses)

        responses.export('out/responses.wav', format='wav')

    # Post-processing for setting channel delays, channel order and cropping out the impulse response tails
    if postprocess:
        # Read WAV file
        if type(responses) == str:
            responses = AudioSegment.from_wav(responses)
        fs = responses.frame_rate
        # Crop
        cropped = []
        i = 0
        responses = responses.split_to_mono()
        while i < len(responses):
            left = responses[i]
            right = responses[i+1]
            if left.rms > 2 and right.rms > 2:
                # Crop tails
                left = crop_ir_tail(left)
                right = crop_ir_tail(right)
                # Crop head
                speaker = CHANNELS[i//2]
                left, right = crop_ir_head(left, right, speaker)
                cropped.append(left)
                cropped.append(right)
            else:
                cropped.append(left[:1])  # For left
                cropped.append(right[:1])  # For right
            i += 2

        # Zero pad to longest
        max_samples = max([len(ir.get_array_of_samples()) for ir in cropped])
        padded = []
        for response in cropped:
            padded.append(zero_pad(response, max_samples, fs))

        # Write standard channel order HRIR
        standard = AudioSegment.from_mono_audiosegments(*padded)
        standard.export('out/hrir.wav', format='wav')

        # Write HeSuVi channel order HRIR
        hesuvi_order = ['FL-left', 'FL-right', 'SL-left', 'SL-right', 'BL-left', 'BL-right', 'FC-left', 'FR-right',
                        'FR-left', 'SR-right', 'SR-left', 'BR-right', 'BR-left', 'FC-right']
        hesuvi = AudioSegment.from_mono_audiosegments(*[padded[IR_ORDER.index(ch)] for ch in hesuvi_order])
        hesuvi.export('out/hesuvi.wav', format='wav')

    if equalize:
        # Read WAV file
        if type(headphones) == str:
            headphones = AudioSegment.from_wav(headphones)
        for i, ch in enumerate(headphones.split_to_mono()):
            f, m = fft(ch.get_array_of_samples(), headphones.frame_rate)
            lines = ['frequency,raw']
            for j in range(len(f)):
                if f == 0.0:
                    continue
                lines.append('{f:.2f},{m:.2f}'.format(f=f[j], m=m[j]))
            with open('out/headphones-{}.csv'.format('left' if i == 0 else 'right'), 'w') as file:
                file.write('\n'.join(lines))


def create_cli():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--measure', action='store_true',
                            help='Measure sine sweeps? Uses default audio output and input devices.')
    arg_parser.add_argument('--preprocess', action='store_true',
                            help='Pre-process recording to produce individual tracks for each speaker-ear pair? '
                                 'If this is not given then recording file is assumed to be already preprocessed.')
    arg_parser.add_argument('--deconvolve', action='store_true', help='Run deconvolution?')
    arg_parser.add_argument('--postprocess', action='store_true',
                            help='Post-process impulse responses to match channel delays and crop tails?')
    arg_parser.add_argument('--equalize', action='store_true',
                            help='Produce CSV file for AutoEQ from headphones sine sweep recordgin?')
    arg_parser.add_argument('--recording', type=str, help='File path to sine sweep recording.')
    arg_parser.add_argument('--responses', type=str, help='File path to impulse responses.')
    arg_parser.add_argument('--test', type=str, help='File path to sine sweep test signal.')
    arg_parser.add_argument('--speakers', type=str,
                            help='Order of speakers in the recording as a comma separated list of speaker channel '
                                 'names. Supported names are "FL" (front left), "FR" (front right), '
                                 '"FC" (front center), "BL" (back left), "BR" (back right), '
                                 '"SL" (side left), "SR" (side right)."')
    arg_parser.add_argument('--silence_length', type=float,
                            help='Length of silence in the beginning, end and between recordings.')
    arg_parser.add_argument('--headphones', type=str,
                            help='File path to headphones sine sweep recording. Stereo WAV file is expected.')
    # TODO: filtfilt
    args = vars(arg_parser.parse_args())
    args['speakers'] = args['speakers'].upper().split(',')
    return args


if __name__ == '__main__':
    main(**create_cli())
