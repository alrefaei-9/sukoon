"""Sukoon audio engine: microphone -> RNNoise -> adaptive post-processor -> earphones.

Real-time pipeline at 48 kHz in 10 ms frames:
  1. RNNoise (Xiph's recurrent neural network noise suppressor, compiled from pinned source by
     build_rnnoise.py into librnnoise.so next to this file) removes steady background noise and
     gives a per-frame voice probability.
  2. AdaptivePostProcessor reads the room continuously (noise floor, speech level, pitch, peaks)
     and decides, frame by frame, what the wearer should hear: speech is kept and levelled,
     sudden bangs are ducked within one 2.5 ms slice (the 20 ms delay line lets it act just
     before the bang is played), sirens and alarms are turned down but kept audible, and steady
     room noise is not replayed.
  3. A 5-band equalizer and a -9 dBFS peak limiter protect the ear.

The dashboard (dashboard/server.py) starts this file and talks to it over UDP on localhost
(metrics out on 5005, settings in on 5006). It can also be run by hand:
    .venv/bin/python src/sukoon_engine.py          live, default sound card, Test 5
Environment overrides: SUKOON_DEVICE (e.g. hw:1,0), SUKOON_TEST_VERSION (1-5, default 5),
SUKOON_LATENCY, SUKOON_NO_RECORD=1. MODE at the bottom of this file selects 'realtime',
'offline' (process a 48 kHz WAV file) or 'devices' (list sound devices).
"""
from pathlib import Path
import ctypes
import os
import hashlib
import json
import math
import mmap
import signal
import socket
import threading
import time

import numpy as np
import scipy.io.wavfile as wav
import scipy.signal


def _dbfs(value, power=False):
    """Level in dBFS of an amplitude (or a mean power), floored at -120."""
    return round((10.0 if power else 20.0) * math.log10(max(value, 1e-12 if power else 1e-6)), 1)


def _smoothing(tau_seconds, step_seconds=0.01):
    """Per-step weight of a one-pole average with the given time constant."""
    return 1.0 - np.exp(-step_seconds / tau_seconds)


class PeakLimiter:
    """Per-sample peak limiter: instant attack, linear release, one gain shared by all channels."""

    def __init__(self, ceiling_dbfs, release_ms, frame=480):
        self.ceiling = 10.0 ** (ceiling_dbfs / 20.0)
        self.release_step = 1.0 / (release_ms * 48.0)
        self.frame = frame
        self.gain = 1.0
        self.limited_frames = 0

    def process(self, out):
        peak = np.max(np.abs(out), axis=1)
        needed = np.minimum(1.0, self.ceiling / np.maximum(peak, 1e-12))
        if needed.min() < 1.0 or self.gain < 1.0:
            if needed.min() < 1.0:
                self.limited_frames += 1
            envelope = np.empty(self.frame, dtype=np.float32)
            g = self.gain
            for k in range(self.frame):
                g = min(needed[k], g + self.release_step, 1.0)
                envelope[k] = g
            self.gain = g
            out = out * envelope[:, None]
        return out


class AudioTap:
    """Shared-memory ring of the last SLOTS frames for recorders in other processes (timed test, battery clips).

    The audio loop only copies each finished frame (input, output, a few flags) into RAM (/dev/shm): a few
    microseconds, no disk I/O, no locks. Readers (tools/tap.py) poll the write counter and copy what they need.
    Layout: 64-byte header (magic, slots, frame, rate, pid, write count at byte 24), then SLOTS slots of
    META_BYTES of metadata followed by the input and output frames as float32.
    flags: 1 voice hold (speech gate open), 2 pitch heard this frame, 4 bang/pop hold, 8 alarm active, 16 bypass.
    """
    MAGIC = b'SSTAP1\0\0'
    SLOTS = 1024                                   # 10.24 s at 10 ms frames, about 4 MB
    META = np.dtype([('frame', '<u8'), ('triggers', '<u4'), ('flags', '<u4'), ('vad', '<f4'), ('version', '<u4'),
                     ('time', '<f8')])

    def __init__(self, path, frame=480, rate=48000):
        self.frame = frame
        self.slot_bytes = self.META.itemsize + 2 * frame * 4
        size = 64 + self.SLOTS * self.slot_bytes
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.ftruncate(fd, size)
            self.mm = mmap.mmap(fd, size)
        finally:
            os.close(fd)
        np.frombuffer(self.mm, np.uint32, 4, 8)[:] = (self.SLOTS, frame, rate, os.getpid())
        self.count = np.frombuffer(self.mm, np.uint64, 1, 24)
        self.mm[:8] = self.MAGIC
        self.meta = np.ndarray((self.SLOTS,), self.META, self.mm, 64, (self.slot_bytes,))
        self.inp = np.ndarray((self.SLOTS, frame), np.float32, self.mm, 64 + self.META.itemsize, (self.slot_bytes, 4))
        self.out = np.ndarray((self.SLOTS, frame), np.float32, self.mm, 64 + self.META.itemsize + 4 * frame,
                              (self.slot_bytes, 4))

    def write(self, n, data, processed, triggers, flags, vad, version):
        k = n % self.SLOTS
        self.inp[k] = data[:, 0]
        self.out[k] = processed[:, 0]
        self.meta[k] = (n, triggers, flags, vad, version, time.time())
        self.count[0] = n + 1                      # published last: readers never see a half-written slot as new


class Equalizer:
    """5-band equalizer on the processed output (RBJ biquads). Flat (all 0 dB) is skipped entirely.

    Bands: 100 Hz low shelf, 400 Hz / 1 kHz / 2.5 kHz peaks (Q 1), 5 kHz high shelf. The AB13X
    microphone path rolls off above ~3 kHz and RNNoise darkens it further, so the upper bands
    restore clarity. Filter state carries across frames; coefficients change only when a gain does.
    """
    BANDS = (('EQ_100', 'lowshelf', 100.0), ('EQ_400', 'peak', 400.0), ('EQ_1000', 'peak', 1000.0),
             ('EQ_2500', 'peak', 2500.0), ('EQ_5000', 'highshelf', 5000.0))

    def __init__(self, fs=48000):
        self.fs = fs
        self._gains = None
        self._sos = None
        self._zi = None

    @staticmethod
    def _biquad(kind, f0, gain_db, fs, q=1.0):
        a = 10.0 ** (gain_db / 40.0)
        w0 = 2.0 * np.pi * f0 / fs
        cos, alpha = np.cos(w0), np.sin(w0) / (2.0 * q)
        if kind == 'peak':
            b = [1 + alpha * a, -2 * cos, 1 - alpha * a]
            den = [1 + alpha / a, -2 * cos, 1 - alpha / a]
        else:
            sq = 2.0 * np.sqrt(a) * np.sin(w0) / 2.0 * np.sqrt(2.0)      # shelf slope S = 1
            if kind == 'lowshelf':
                b = [a * ((a + 1) - (a - 1) * cos + sq), 2 * a * ((a - 1) - (a + 1) * cos), a * ((a + 1) - (a - 1) * cos - sq)]
                den = [(a + 1) + (a - 1) * cos + sq, -2 * ((a - 1) + (a + 1) * cos), (a + 1) + (a - 1) * cos - sq]
            else:
                b = [a * ((a + 1) + (a - 1) * cos + sq), -2 * a * ((a - 1) + (a + 1) * cos), a * ((a + 1) + (a - 1) * cos - sq)]
                den = [(a + 1) - (a - 1) * cos + sq, 2 * ((a - 1) - (a + 1) * cos), (a + 1) - (a - 1) * cos - sq]
        return np.array(b + den) / den[0]

    def process(self, out, gains):
        """out: (480, channels) float32; gains: tuple of the 5 band gains in dB."""
        if not any(gains):
            self._gains, self._zi = None, None
            return out
        if gains != self._gains:
            self._sos = np.array([self._biquad(kind, f0, g, self.fs) for (_, kind, f0), g in zip(self.BANDS, gains)])
            if self._zi is None or self._zi.shape[2] != out.shape[1]:
                self._zi = np.zeros((len(self.BANDS), 2, out.shape[1]))
            self._gains = gains
        filtered, self._zi = scipy.signal.sosfilt(self._sos, out, axis=0, zi=self._zi)
        return filtered.astype(np.float32)


class AdaptivePostProcessor:
    """Environment-adaptive stage after RNNoise; needs no per-room settings.

    Every 10 ms frame it:
    - tracks the noise floor (minimum of 0.5 s block minima over 3 s) and the
      speech level (average power of voice frames). A frame is voice when
      RNNoise says so or when the last 30 ms has a clear 70-400 Hz pitch;
      RNNoise alone misses many distant or unfamiliar voices;
    - (sustained_voice, default) only counts voice that lasts and stands out:
      RNNoise >= VOICE_VAD (or pitch) in VOICE_NEEDED of the last VOICE_WINDOW
      frames, each >= VOICE_OVER_NOISE_DB above the noise floor. Steady room
      noise (an AC unit) fools RNNoise in short bursts but fails this test.
      With sustained_voice False the simpler Test 1 rule is used;
    - mixes the input back in, delayed to line up with RNNoise, at an
      attenuation floor that keeps leaked noise ~30 dB below speech
      (12-30 dB), only while voice was heard in the last second. This keeps
      words RNNoise wrongly removes; in speech pauses RNNoise acts alone.
      The floor fades in over ~50 ms and out over ~400 ms (time constants);
    - mutes that floor for 300 ms after a bang: a 2.5 ms slice >20 dB above
      the previous 50 ms and louder than both speech+6 dB and noise+30 dB, or
      (mid-speech) >20 dB above the speech level and >6 dB above the previous
      50 ms. The bang is seen 20 ms before it leaves the delay line;
    - slowly sets gain so speech sits near TARGET_SPEECH_DBFS (-6..+18 dB);
      (sustained_voice) after QUIET_AFTER_FRAMES without voice, or before any,
      the gain glides back to at most QUIET_GAIN_DB, so background noise is never
      boosted: the earphones leak the room in anyway;
    - (sustained_guard, Test 3) keeps a bang's mitigation going while the sound
      stays loud. Triggers: a bang, or a steady loud sound (SUSTAIN_STEADY_FRAMES
      frames within SUSTAIN_STEADY_DB of each other, SUSTAIN_OVER_NOISE_DB above
      the room and at least SUSTAIN_MIN_DBFS); sirens ramp up too slowly to trip the bang detector, and speech
      is never that steady. After a trigger, every 10 ms frame within SUSTAIN_WITHIN_DB of
      the bang's level (6 dB = half the amplitude) re-arms the mute. Once the sound
      has lasted SUSTAIN_ALARM_AFTER_FRAMES (claps never do), the output becomes the
      real sound turned down by SUSTAIN_DUCK_DB: audible as an alarm, never loud, and
      not mangled by RNNoise. The reference follows a sound that fades slowly (an ambulance
      driving away); a drop of more than SUSTAIN_WITHIN_DB ends it. Noise and speech
      levels, voice hold and gain are frozen meanwhile so a siren is never learnt
      as the room or as speech;
    - limits output peaks to CEILING_DBFS (instant attack, 50 ms release).
    """
    FRAME = 480
    DELAY = 960
    SLICE = 120
    NOISE_BLOCK_FRAMES = 50
    NOISE_BLOCKS = 6
    SPEECH_VAD = 0.5
    HOLD_VAD = 0.3
    HOLD_FRAMES = 100
    VOICING_MIN = 0.6                 # normalized autocorrelation peak
    VOICING_OVER_NOISE_DB = 10.0
    DECIMATE = 4                      # pitch search at 12 kHz
    LEAK_BELOW_SPEECH_DB = 30.0
    FLOOR_MIN_DB = 12.0
    FLOOR_MAX_DB = 30.0
    POP_JUMP_DB = 20.0
    POP_OVER_SPEECH_DB = 6.0
    POP_OVER_NOISE_DB = 30.0
    LOUD_OVER_SPEECH_DB = 20.0
    LOUD_JUMP_DB = 6.0
    POP_HOLD_FRAMES = 30 + 3          # 300 ms plus the 2-frame delay
    TARGET_SPEECH_DBFS = -23.0
    GAIN_MIN_DB = -6.0
    GAIN_MAX_DB = 18.0
    CEILING_DBFS = -9.0
    RELEASE_MS = 50.0
    FLOOR_FADE_IN_S = 0.05            # dry floor fades in when voice starts or a bang ends
    FLOOR_FADE_OUT_S = 0.4            # and fades out slowly to the RNNoise-only background
    VOICE_VAD = 0.6                   # sustained_voice rule (rejects steady noise such as an AC unit)
    VOICE_OVER_NOISE_DB = 14.0
    VOICE_WINDOW = 6
    VOICE_NEEDED = 4
    QUIET_AFTER_FRAMES = 500          # 5 s without voice
    QUIET_GAIN_DB = 0.0
    SUSTAIN_WITHIN_DB = 6.0           # sustained_guard (Test 3+): "same sound" while within 6 dB
    SUSTAIN_DUCK_DB = 20.0            # how far a sustained loud sound is turned down (dashboard: Alarm reduction)
    SUSTAIN_TRACK_S = 2.0             # reference follows a slowly fading sound
    SUSTAIN_DUCK_RELEASE_S = 0.15
    SUSTAIN_ALARM_AFTER_FRAMES = 10   # 100 ms: a clap is over by then
    SUSTAIN_FADE_S = 2.0              # alarm fades in from SUSTAIN_FADE_FROM_DB below its reduced level (dashboard)
    SUSTAIN_FADE_FROM_DB = 30.0
    ALARM_MIN_OUT_DBFS = -26.0        # alarm_floor (Test 4+): an alarm is never played quieter than this
    ALARM_MAX_BOOST_DB = 10.0         # ... raising a faint alarm by up to this much to reach it (dashboard)
    BANG_DUCK_DB = 30.0               # speech_bang (Test 5): whole output turned down this far during a bang
    BANG_OVER_SPEECH_DB = 15.0        # ... a 2.5 ms slice whose PEAK is this far above the speech level (dashboard) ...
    BANG_JUMP_DB = 10.0               # ... and this far above the highest peak of the previous 50 ms
    BANG_OVER_NOISE_DB = 20.0         # ... and at least this far above the room
    BANG_RELEASE_BELOW_DB = 6.0       # duck ends once slices fall this far below the trigger level ...
    BANG_MIN_SLICES = 12              # ... but lasts at least 30 ms
    BANG_MAX_SLICES = 120             # ... and at most 300 ms (longer loud sounds: the alarm guard)
    BANG_RELEASE_SLICES = 8           # gain comes back over 20 ms
    BANG_HOLD_FRAMES = 10 + 3         # floor mute / level freeze after a speech-time bang (vs POP_HOLD_FRAMES)
    EQ_100 = 0.0                      # equalizer band gains in dB (dashboard); all 0 = off
    EQ_400 = 0.0
    EQ_1000 = 0.0
    EQ_2500 = 0.0
    EQ_5000 = 0.0
    SUSTAIN_EVENT_FRAMES = 50         # counted as an alarm after 0.5 s
    SUSTAIN_STEADY_FRAMES = 15        # steady-sound trigger: 150 ms (dashboard: Alarm detect time) ...
    SUSTAIN_STEADY_MAX = 30           # ring buffer size, the longest detect time
    SUSTAIN_STEADY_DB = 6.0           # ... all within 6 dB of each other ...
    SUSTAIN_OVER_NOISE_DB = 20.0      # ... and 20 dB above the room noise ...
    SUSTAIN_MIN_DBFS = -40.0          # ... and loud in absolute terms (room hum never is)

    def __init__(self):
        self._sos = scipy.signal.butter(4, 900, 'low', fs=48000, output='sos')
        rate = 48000 // self.DECIMATE
        self._pitch_lags = np.arange(rate // 400, rate // 70 + 1)
        self._a_speech = _smoothing(2.0)
        self._a_floor = _smoothing(1.0)
        self._a_gain = _smoothing(3.0)
        self._a_fade_in = _smoothing(self.FLOOR_FADE_IN_S)
        self._a_fade_out = _smoothing(self.FLOOR_FADE_OUT_S)
        self.pop_guard_enabled = True
        self.sustained_voice = True       # False: the Test 1 voice rule and gain
        self.sustained_guard = False      # True: Test 3 sustained-loud-sound guard
        self.alarm_floor = False          # True: Test 4, the alarm stays audible (ALARM_MIN_OUT_DBFS)
        self.speech_bang = False          # True: Test 5, bangs found and ducked during speech too
        self._a_track = _smoothing(self.SUSTAIN_TRACK_S)
        self._a_duck_release = _smoothing(self.SUSTAIN_DUCK_RELEASE_S)
        self.reset()

    def reset(self):
        self._dry = None
        self._zi = np.zeros((self._sos.shape[0], 2))
        self._pitch_buffer = np.zeros(3 * self.FRAME // self.DECIMATE)
        self.voiced_frames = 0
        self._block_min = np.inf
        self._block_count = 0
        self._block_mins = []
        self.noise_db = -70.0
        self.speech_db = -40.0
        self.floor_db = self.FLOOR_MIN_DB
        self.gain_db = 0.0
        self._floor_gain = 0.0
        self._gain = 1.0
        self._peak = PeakLimiter(self.CEILING_DBFS, self.RELEASE_MS, self.FRAME)
        self._eq = Equalizer()
        self._hold = 0
        self._pop_history = np.full(20, 1e-10)
        self._pop_hold = 0
        self.pop_triggers = 0
        self.frames = 0
        self._voice_window = np.zeros(self.VOICE_WINDOW, dtype=bool)
        self._level_db = -120.0
        self._loud_active = False
        self._loud_ref_db = -120.0
        self._loud_frames = 0
        self._alarm_mix = 0.0
        self._alarm_age = 0
        self._alarm_gain = 0.0
        self.alarm_events = 0
        self._recent_levels = np.full(self.SUSTAIN_STEADY_MAX, -120.0)
        self._since_voice = self.QUIET_AFTER_FRAMES + 1      # no voice heard yet
        n_line = (self.DELAY + self.FRAME) // self.SLICE
        self._duck_line = np.zeros(n_line)                    # Test 5: duck (dB) per slice, oldest = leaving now
        self._duck_db = 0.0                                   # duck of the newest slice
        self._duck_out_db = 0.0                               # duck of the last slice played
        self._duck_slices = 0                                 # slices since the bang started (0 = no bang)
        self._duck_ref = 0.0                                  # trigger level of the current bang (power)
        self._peak_history = np.full(20, 1e-10)               # Test 5: slice peaks (squared) of the last 50 ms
        self.duck_frames = 0

    def _voicing(self, data, search):
        """Pitch-periodicity strength (0-1) of the last 30 ms, for 70-400 Hz voices.

        The low-pass state always advances; the pitch search runs only if search.
        """
        low, self._zi = scipy.signal.sosfilt(self._sos, data.mean(axis=1), zi=self._zi)
        step = self.FRAME // self.DECIMATE
        self._pitch_buffer = np.concatenate([self._pitch_buffer[step:], low[::self.DECIMATE]])
        if not search:
            return 0.0
        seg = self._pitch_buffer - self._pitch_buffer.mean()
        spectrum = np.fft.rfft(seg, 2 * len(seg))
        corr = np.fft.irfft(spectrum.real ** 2 + spectrum.imag ** 2)[:len(seg)]
        tail_energy = np.cumsum((seg ** 2)[::-1])[::-1]
        lags = self._pitch_lags
        norm = np.sqrt(corr[0] * tail_energy[lags]) + 1e-20
        return float(np.max(corr[lags] / norm))

    def _track_levels(self, data, vad):
        power = float(np.mean(data.astype(np.float64) ** 2)) + 1e-12
        level_db = 10.0 * np.log10(power)
        self._level_db = level_db
        loud_enough = level_db > self.noise_db + self.VOICING_OVER_NOISE_DB
        voiced = self._voicing(data, loud_enough) >= self.VOICING_MIN
        self.voiced_now = voiced                    # read by the audio tap only
        self.voiced_frames += int(voiced)
        if self.sustained_guard:
            self._recent_levels[self.frames % self.SUSTAIN_STEADY_MAX] = level_db   # ring buffer
            if self._loud_active or self._pop_hold > 0:
                # A siren, or the tail of a bang or alarm, is neither the room nor speech: freeze both levels,
                # and forget the frames that looked like voice so speech must be confirmed afresh afterwards.
                self._voice_window[:] = False
                self._hold = 0
                return
        self._block_min = min(self._block_min, level_db)
        self._block_count += 1
        if self._block_count == self.NOISE_BLOCK_FRAMES:
            self._block_mins = (self._block_mins + [self._block_min])[-self.NOISE_BLOCKS:]
            self._block_min, self._block_count = np.inf, 0
        if self._block_mins:
            self.noise_db = min(min(self._block_mins), self._block_min)
        if not self.sustained_voice:
            if (vad >= self.SPEECH_VAD or voiced) and level_db > self.noise_db + 6.0:
                self.speech_db += self._a_speech * (level_db - self.speech_db)
            if vad >= self.HOLD_VAD or voiced:
                self._hold = self.HOLD_FRAMES
            return
        frame_voice = (vad >= self.VOICE_VAD or voiced) and level_db >= self.noise_db + self.VOICE_OVER_NOISE_DB
        self._voice_window = np.roll(self._voice_window, -1)
        self._voice_window[-1] = frame_voice
        self._since_voice += 1
        if int(self._voice_window.sum()) >= self.VOICE_NEEDED:
            if frame_voice:
                self.speech_db += self._a_speech * (level_db - self.speech_db)
            self._hold = self.HOLD_FRAMES
            self._since_voice = 0

    def _detect_pop(self, data):
        threshold_db = max(self.speech_db + self.POP_OVER_SPEECH_DB,
                           self.noise_db + self.POP_OVER_NOISE_DB)
        min_power = 10.0 ** (threshold_db / 10.0)
        jump = 10.0 ** (self.POP_JUMP_DB / 10.0)
        loud_power = 10.0 ** ((self.speech_db + self.LOUD_OVER_SPEECH_DB) / 10.0)
        loud_jump = 10.0 ** (self.LOUD_JUMP_DB / 10.0)
        slices = data.reshape(-1, self.SLICE, data.shape[1]).astype(np.float64)
        for power in np.mean(slices ** 2, axis=(1, 2)) + 1e-12:
            recent = self._pop_history.max()
            if ((power > min_power and power > jump * recent)
                    or (power > loud_power and power > loud_jump * recent)):
                if self._pop_hold == 0:
                    self.pop_triggers += 1
                self._pop_hold = self.POP_HOLD_FRAMES
            self._pop_history = np.roll(self._pop_history, -1)
            self._pop_history[-1] = power

    def _detect_bang_speech(self, data):
        """Test 5: _detect_pop's rules, plus a bang rule that also works during speech, and a per-slice duck.

        A slice whose peak is BANG_JUMP_DB above the previous 50 ms and BANG_OVER_SPEECH_DB above the speech
        level starts a bang (a clap's peak stands out far more than its 2.5 ms average power). Every slice gets a duck value (0 or -BANG_DUCK_DB, gliding back over BANG_RELEASE_SLICES) that is
        applied to the whole output when the slice leaves the 20 ms delay line, one slice early so the onset is
        covered. The duck ends when slices fall BANG_RELEASE_BELOW_DB below the trigger level (after at least
        BANG_MIN_SLICES), or after BANG_MAX_SLICES.
        """
        threshold_db = max(self.speech_db + self.POP_OVER_SPEECH_DB,
                           self.noise_db + self.POP_OVER_NOISE_DB)
        min_power = 10.0 ** (threshold_db / 10.0)
        jump = 10.0 ** (self.POP_JUMP_DB / 10.0)
        loud_power = 10.0 ** ((self.speech_db + self.LOUD_OVER_SPEECH_DB) / 10.0)
        loud_jump = 10.0 ** (self.LOUD_JUMP_DB / 10.0)
        bang_power = 10.0 ** (max(self.speech_db + self.BANG_OVER_SPEECH_DB,
                                  self.noise_db + self.BANG_OVER_NOISE_DB) / 10.0)
        bang_jump = 10.0 ** (self.BANG_JUMP_DB / 10.0)
        release = 10.0 ** (-self.BANG_RELEASE_BELOW_DB / 10.0)
        step_db = self.BANG_DUCK_DB / max(1, self.BANG_RELEASE_SLICES)
        n = self.FRAME // self.SLICE
        line = np.concatenate([self._duck_line[n:], np.zeros(n)])
        slices = data.reshape(-1, self.SLICE, data.shape[1]).astype(np.float64)
        powers = np.mean(slices ** 2, axis=(1, 2)) + 1e-12
        peaks = np.max(slices ** 2, axis=(1, 2)) + 1e-12
        for k, (power, peak) in enumerate(zip(powers, peaks)):
            recent = self._pop_history.max()
            old_rule = ((power > min_power and power > jump * recent)
                        or (power > loud_power and power > loud_jump * recent))
            new_rule = peak > bang_power and peak > bang_jump * self._peak_history.max()
            self._peak_history = np.roll(self._peak_history, -1)
            self._peak_history[-1] = peak
            if old_rule or new_rule:
                if self._pop_hold == 0:
                    self.pop_triggers += 1
                hold = self.POP_HOLD_FRAMES if old_rule and self._hold == 0 else self.BANG_HOLD_FRAMES
                self._pop_hold = max(self._pop_hold, hold)
                if self._duck_slices == 0:
                    self._duck_ref = power
                    i = len(line) - n + k - 1                 # look ahead: duck the slice before the onset too
                    line[i] = -self.BANG_DUCK_DB
                self._duck_ref = max(self._duck_ref, power)
                self._duck_slices = max(self._duck_slices, 1)
            if self._duck_slices > 0:
                self._duck_slices += 1
                if ((self._duck_slices > self.BANG_MIN_SLICES and power < release * self._duck_ref)
                        or self._duck_slices > self.BANG_MAX_SLICES):
                    self._duck_slices = 0
            if self._duck_slices > 0:
                self._duck_db = -self.BANG_DUCK_DB
            else:
                self._duck_db = min(0.0, self._duck_db + step_db)
            line[len(line) - n + k] = self._duck_db
            self._pop_history = np.roll(self._pop_history, -1)
            self._pop_history[-1] = power
        self._duck_line = line

    def _duck_gains(self):
        """Test 5: per-sample gain for the frame leaving the delay line now (ramps across each slice)."""
        n = self.FRAME // self.SLICE
        db = np.concatenate([[self._duck_out_db], self._duck_line[:n]])
        self._duck_out_db = float(db[-1])
        if not db.any():
            return None
        self.duck_frames += int(db[1:].min() < -1.0)
        g = 10.0 ** (db / 20.0)
        t = (np.arange(self.SLICE) + 1) / self.SLICE
        return np.concatenate([g[j] + (g[j + 1] - g[j]) * t for j in range(n)]).astype(np.float32)

    def process(self, data, denoised, vad):
        """data, denoised: (480, channels) float32; vad: RNNoise voice probability."""
        if self._dry is None:
            self._dry = np.zeros((self.DELAY, data.shape[1]), dtype=np.float32)
        self.frames += 1
        self._track_levels(data, vad)
        if self.pop_guard_enabled:
            triggers = self.pop_triggers
            hold_before = self._pop_hold
            if self.speech_bang:
                self._detect_bang_speech(data)
            else:
                self._detect_pop(data)
            if self.sustained_guard:
                self._sustain(triggered=self.pop_triggers > triggers or self._pop_hold > hold_before)
        else:
            self._pop_hold = 0
            self._loud_active = False

        wanted = self.noise_db - (self.speech_db - self.LEAK_BELOW_SPEECH_DB)
        wanted = float(np.clip(wanted, self.FLOOR_MIN_DB, self.FLOOR_MAX_DB))
        self.floor_db += self._a_floor * (wanted - self.floor_db)
        floor_level = 10.0 ** (-self.floor_db / 20.0) if self._hold > 0 else 0.0
        if self._pop_hold > 0:
            floor_target = 0.0        # duck within one frame, before the bang leaves the delay line
            self._pop_hold -= 1
        else:
            fade = self._a_fade_in if floor_level > self._floor_gain else self._a_fade_out
            floor_target = self._floor_gain + fade * (floor_level - self._floor_gain)
        self._hold = max(self._hold - 1, 0)

        wanted_gain = float(np.clip(self.TARGET_SPEECH_DBFS - self.speech_db,
                                    self.GAIN_MIN_DB, self.GAIN_MAX_DB))
        if self.sustained_voice and self._since_voice > self.QUIET_AFTER_FRAMES:
            wanted_gain = min(wanted_gain, self.QUIET_GAIN_DB)
        if self.sustained_guard and self._loud_active:
            wanted_gain = min(wanted_gain, self.gain_db)          # never raise the gain on a siren
        self.gain_db += self._a_gain * (wanted_gain - self.gain_db)
        gain_target = 10.0 ** (self.gain_db / 20.0)

        dry = self._dry[:self.FRAME]
        self._dry = np.concatenate([self._dry[self.FRAME:], data.astype(np.float32)])
        floor_ramp = np.linspace(self._floor_gain, floor_target, self.FRAME + 1, dtype=np.float32)[1:]
        gain_ramp = np.linspace(self._gain, gain_target, self.FRAME + 1, dtype=np.float32)[1:]
        self._floor_gain, self._gain = floor_target, gain_target
        out = (denoised + dry * floor_ramp[:, None]) * gain_ramp[:, None]
        if self.speech_bang:
            duck = self._duck_gains()
            if duck is not None:
                out = out * duck[:, None]
        if self.sustained_guard:
            if self._loud_active and self._loud_frames >= self.SUSTAIN_ALARM_AFTER_FRAMES:
                mix_target = 1.0                                     # within one frame
            else:
                mix_target = self._alarm_mix - self._a_duck_release * self._alarm_mix
                if mix_target < 1e-4:
                    mix_target = 0.0
            if mix_target == 1.0:
                if self._alarm_mix < 1.0:
                    self._alarm_age = 0                              # alarm mode starts: fade in from very low
                self._alarm_age += 1
            if self._alarm_mix > 0.0 or mix_target > 0.0:            # no extra work when no alarm
                fade = min(1.0, self._alarm_age / max(1.0, self.SUSTAIN_FADE_S * 100.0))
                duck_db = self.SUSTAIN_DUCK_DB
                if self.alarm_floor:          # Test 4: reduce by up to DUCK, but keep the alarm at or above the floor
                    duck_db = min(self.SUSTAIN_DUCK_DB, max(-self.ALARM_MAX_BOOST_DB,
                                                            self._loud_ref_db - self.ALARM_MIN_OUT_DBFS))
                gain = 10.0 ** (-(duck_db + self.SUSTAIN_FADE_FROM_DB * (1.0 - fade)) / 20.0)
                if self._alarm_mix == 0.0:
                    self._alarm_gain = 10.0 ** (-(self.SUSTAIN_DUCK_DB + self.SUSTAIN_FADE_FROM_DB) / 20.0)
                gain_ramp = np.linspace(self._alarm_gain, gain, self.FRAME + 1, dtype=np.float32)[1:, None]
                self._alarm_gain = gain
                mix_ramp = np.linspace(self._alarm_mix, mix_target, self.FRAME + 1, dtype=np.float32)[1:, None]
                out = out * (1.0 - mix_ramp) + dry * gain_ramp * mix_ramp
            self._alarm_mix = mix_target
        out = self._eq.process(out, (self.EQ_100, self.EQ_400, self.EQ_1000, self.EQ_2500, self.EQ_5000))
        return self._peak.process(out).astype(np.float32)

    def _sustain(self, triggered):
        """Test 3: keep mitigating while the triggering sound stays within SUSTAIN_WITHIN_DB of it."""
        was_active = self._loud_active
        level = self._level_db
        recent = self._recent_levels[(self.frames - np.arange(self.SUSTAIN_STEADY_FRAMES)) % self.SUSTAIN_STEADY_MAX]
        if (not triggered and not self._loud_active and recent.max() - recent.min() <= self.SUSTAIN_STEADY_DB
                and recent.mean() >= max(self.noise_db + self.SUSTAIN_OVER_NOISE_DB, self.SUSTAIN_MIN_DBFS)):
            triggered = True                                      # steady loud sound (siren, alarm)
            self._pop_hold = self.POP_HOLD_FRAMES
            self._loud_frames = self.SUSTAIN_STEADY_FRAMES          # already steady for 200 ms
            self._loud_active = True
            self._loud_ref_db = float(recent.mean())
        elif triggered:
            if not self._loud_active:
                self._loud_frames = 0
                self._loud_ref_db = level
            else:
                self._loud_ref_db = max(level, self._loud_ref_db)
            self._loud_active = True
        elif self._loud_active:
            if level >= self._loud_ref_db - self.SUSTAIN_WITHIN_DB:
                self._pop_hold = self.POP_HOLD_FRAMES           # still the same sound: stay mitigated
                if level > self._loud_ref_db:
                    self._loud_ref_db = level
                else:
                    self._loud_ref_db += self._a_track * (level - self._loud_ref_db)
            else:
                self._loud_active = False                         # much quieter: back to normal
        if self._loud_active:
            if not was_active and self._loud_frames >= self.SUSTAIN_STEADY_FRAMES:
                self._since_voice = self.QUIET_AFTER_FRAMES + 1   # its "voice" frames were the siren: no boost after
            self._loud_frames += 1
            if self._loud_frames == self.SUSTAIN_EVENT_FRAMES:
                self.alarm_events += 1

    def stats(self):
        return {'noise_floor_dbfs': round(self.noise_db, 1), 'speech_level_dbfs': round(self.speech_db, 1),
                'attenuation_floor_db': round(self.floor_db, 1), 'output_gain_db': round(self.gain_db, 1),
                'pop_guard_triggers': self.pop_triggers, 'limited_frames': self._peak.limited_frames,
                'voiced_seconds': round(self.voiced_frames * self.FRAME / 48000.0, 1),
                'alarm_events': self.alarm_events, 'bang_duck_frames': self.duck_frames}


class AttenuationMeter:
    """Measures, never changes, the audio: how much quieter each bang or alarm is at the output than at the mic,
    and how far steady room noise is turned down (all electrical: mic signal vs earbud signal).

    Bang: loudest input from 3 frames before the guard fired to 4 after, against the loudest output over the
    same span shifted by the 20 ms processing delay. Alarm: average level over its whole length, in vs out."""
    DELAY_FRAMES = 2                 # output lags input by 20 ms (960 samples, measured)
    PRE_FRAMES = 3
    ONSET_FRAMES = 5
    AMBIENT_TC_FRAMES = 500          # 5 s average for room noise
    KEEP = 10

    def __init__(self):
        self.records, self.seq = [], 0
        self._pre_in = [0.0] * self.PRE_FRAMES
        self._pre_out = [0.0] * self.DELAY_FRAMES
        self._event = None
        self._amb_in = self._amb_out = None
        self._last_triggers = 0

    def update(self, in_peak, out_peak, in_power, out_power, active, alarm, voice, triggers=0):
        e = self._event
        if active and e is None:
            e = self._event = {'k': 0, 'triggers': self._last_triggers, 'in_peak': max(self._pre_in), 'out_peak': max(self._pre_out[-1:]),
                               'in_power': 0.0, 'out_power': 0.0, 'frames': 0, 'alarm': False, 'done': False}
        if e is not None:
            k = e['k']
            if k < self.ONSET_FRAMES:
                e['in_peak'] = max(e['in_peak'], in_peak)
            if k < self.ONSET_FRAMES + self.DELAY_FRAMES:
                e['out_peak'] = max(e['out_peak'], out_peak)
            if active:
                e['in_power'] += in_power
                e['out_power'] += out_power
                e['frames'] += 1
                e['alarm'] = e['alarm'] or alarm
            else:
                e['done'] = True
            e['bang'] = triggers > e['triggers']
            e['k'] = k + 1
            if e['done'] and e['k'] >= self.ONSET_FRAMES + self.DELAY_FRAMES:
                self._finish()
        elif not voice:
            a = 1.0 / self.AMBIENT_TC_FRAMES
            self._amb_in = in_power if self._amb_in is None else self._amb_in + a * (in_power - self._amb_in)
            self._amb_out = out_power if self._amb_out is None else self._amb_out + a * (out_power - self._amb_out)
        self._pre_in = self._pre_in[1:] + [in_peak]
        self._pre_out = self._pre_out[1:] + [out_peak]
        self._last_triggers = triggers

    def _finish(self):
        e, self._event = self._event, None
        self.seq += 1
        in_pk, out_pk = _dbfs(e['in_peak']), _dbfs(e['out_peak'])
        frames = max(e['frames'], 1)
        in_rms, out_rms = _dbfs(e['in_power'] / frames, power=True), _dbfs(e['out_power'] / frames, power=True)
        kind = 'alarm' if e['alarm'] else 'bang' if e['bang'] else 'steady'   # steady: Test 3 hold that never became an alarm
        reduction = in_rms - out_rms if kind == 'alarm' else in_pk - out_pk
        self.records = (self.records + [{
            'seq': self.seq, 'kind': kind, 'ms': 10 * e['frames'], 'reduction_db': round(reduction, 1),
            'in_peak_dbfs': round(in_pk, 1), 'out_peak_dbfs': round(out_pk, 1),
            'in_rms_dbfs': round(in_rms, 1), 'out_rms_dbfs': round(out_rms, 1)}])[-self.KEEP:]

    def view(self):
        amb = None
        if self._amb_in is not None:
            i, o = _dbfs(self._amb_in, power=True), _dbfs(self._amb_out, power=True)
            amb = {'in_dbfs': round(i, 1), 'out_dbfs': round(o, 1), 'reduction_db': round(i - o, 1)}
        return {'events': self.records, 'ambient': amb}


class SpectrumMeter:
    """Spectrum of the microphone input and the earbud output, per bang, and averaged every 3 minutes.

    Every frame, the last four frames (40 ms) of input and of output are Hann-windowed (1920-point FFT, 25 Hz
    bins, so even the 125 Hz band has a bin) and summed into 1/3-octave bands from 100 Hz to 10 kHz, as mean-square power (dBFS like the level
    meters). The input bands are delayed DELAY_FRAMES so they line up with the output that came from them.
    - live: both spectra averaged over LIVE_TC_FRAMES (0.5 s);
    - bang: after a bang trigger, input and output energy per band over BANG_FRAMES aligned frames (the
      frame before the bang included): the before/after spectrum of the spike and its reduction;
    - average: every AVG_WINDOW_FRAMES (3 min) the overall attenuation, 10 log10(input energy / output
      energy) over all frames, broadband and per band, is stored; the window in progress is shown too.
    Reduction in percent is how much smaller the sound pressure (amplitude) is: 100 (1 - 10^(-dB/20)),
    so 6 dB = 50%, 20 dB = 90%, 40 dB = 99%. Electrical (mic signal vs earbud signal), like AttenuationMeter.
    Costs two 1920-point FFTs per frame (~0.2 ms on the Pi 4); runs after the frame was written.
    """
    FS = 48000
    FRAME = 480
    DELAY_FRAMES = 2
    BANDS = [round(1000 * 2 ** (k / 3)) for k in range(-10, 11)]      # 100 Hz ... 10 kHz
    WINDOW_FRAMES = 4
    LIVE_TC_FRAMES = 50
    BANG_FRAMES = 8
    AVG_WINDOW_FRAMES = 18000                                          # 3 minutes
    KEEP_WINDOWS = 20
    KEEP_BANGS = 100
    MAX_DB = 60.0

    def __init__(self):
        n = self.WINDOW_FRAMES * self.FRAME
        f = np.fft.rfftfreq(n, 1.0 / self.FS)
        self._window = np.hanning(n).astype(np.float64)
        self._scale = 2.0 / (n * float(np.sum(self._window ** 2)))
        edges = [fc / 2 ** (1 / 6) for fc in self.BANDS] + [self.BANDS[-1] * 2 ** (1 / 6)]
        self._edges = np.searchsorted(f, edges)                         # bands are contiguous bin ranges
        self._a_live = 1.0 / self.LIVE_TC_FRAMES
        self._buf = np.zeros((2, n))                                   # input, output
        self._queue = []                                               # input bands/power waiting DELAY_FRAMES
        self._live_in = np.full(len(self.BANDS), 1e-12)
        self._live_out = np.full(len(self.BANDS), 1e-12)
        self._last_triggers = None
        self._bang_wait = -1
        self._bang_in = self._bang_out = None
        self._bang_left = 0
        self.bangs = []
        self._summary = self._summary_last = None
        self.windows = []
        self._start_window()
        self.frames = 0

    def _start_window(self):
        self._sum_in = 0.0
        self._sum_out = 0.0
        self._band_in = np.zeros(len(self.BANDS))
        self._band_out = np.zeros(len(self.BANDS))
        self._window_frames = 0
        self._window_bangs = 0

    def _bands(self):
        """Band powers of the input and output windows, one FFT call for both: (2, bands)."""
        spectrum = np.fft.rfft(self._buf * self._window, axis=1)[:, :self._edges[-1]]
        power = np.add.reduceat(spectrum.real ** 2 + spectrum.imag ** 2, self._edges[:-1], axis=1)
        return power * self._scale + 1e-12

    def _db(self, num, den):
        return float(np.clip(10.0 * np.log10((num + 1e-12) / (den + 1e-12)), -self.MAX_DB, self.MAX_DB))

    @staticmethod
    def pct(db):
        return round(100.0 * (1.0 - 10.0 ** (-db / 20.0)), 1)

    def update(self, data, processed, in_power, out_power, triggers=0):
        """data, processed: this frame's input and output, (480, channels)."""
        self.frames += 1
        self._buf[:, :-self.FRAME] = self._buf[:, self.FRAME:]
        self._buf[0, -self.FRAME:] = data[:, 0] if data.shape[1] == 1 else data.mean(axis=1)
        self._buf[1, -self.FRAME:] = processed[:, 0] if processed.shape[1] == 1 else processed.mean(axis=1)
        both = self._bands()
        self._queue.append((both[0], in_power))
        out_bands = both[1]
        if len(self._queue) <= self.DELAY_FRAMES:
            return
        in_bands, in_power = self._queue.pop(0)
        self._live_in += self._a_live * (in_bands - self._live_in)
        self._live_out += self._a_live * (out_bands - self._live_out)
        # bang: start one frame after the trigger (window then holds the frame before the bang), BANG_FRAMES long
        new_bang = self._last_triggers is not None and triggers > self._last_triggers
        self._last_triggers = triggers
        if self._bang_left > 0:
            self._bang_in += in_bands
            self._bang_out += out_bands
            self._bang_left -= 1
            if self._bang_left == 0:
                self._finish_bang()
        elif new_bang:
            self._bang_in, self._bang_out = in_bands.copy() * 0.0, out_bands.copy() * 0.0
            self._bang_left = self.BANG_FRAMES
            self._window_bangs += 1
        # 3-minute average
        self._sum_in += in_power
        self._sum_out += out_power
        self._band_in += in_bands
        self._band_out += out_bands
        self._window_frames += 1
        if self._window_frames >= self.AVG_WINDOW_FRAMES:
            self.windows = (self.windows + [self._window_record(final=True)])[-self.KEEP_WINDOWS:]
            self._start_window()

    def _finish_bang(self):
        bi, bo = self._bang_in, self._bang_out
        per_band = [round(self._db(i, o), 1) for i, o in zip(bi, bo)]
        overall = self._db(float(bi.sum()), float(bo.sum()))
        k = int(np.argmax(bi))
        self.bangs = (self.bangs + [{'time': time.time(), 'in_db': [round(10 * math.log10(v / self.BANG_FRAMES), 1) for v in bi],
                                     'out_db': [round(10 * math.log10(v / self.BANG_FRAMES), 1) for v in bo],
                                     'reduction_db': per_band, 'overall_db': round(overall, 1),
                                     'overall_pct': self.pct(overall), 'peak_hz': self.BANDS[k],
                                     'peak_db': per_band[k], 'peak_pct': self.pct(per_band[k])}])[-self.KEEP_BANGS:]

    def _window_record(self, final):
        db = self._db(self._sum_in, self._sum_out) if self._window_frames else 0.0
        n = max(self._window_frames, 1)
        return {'time': time.time(), 'seconds': round(self._window_frames * self.FRAME / self.FS, 1),
                'reduction_db': round(db, 1), 'pct': self.pct(db), 'bangs': self._window_bangs,
                'in_dbfs': round(_dbfs(self._sum_in / n, power=True), 1),
                'out_dbfs': round(_dbfs(self._sum_out / n, power=True), 1),
                'band_db': np.round(np.clip(10.0 * np.log10((self._band_in + 1e-12) / (self._band_out + 1e-12)),
                                            -self.MAX_DB, self.MAX_DB), 1).tolist(),
                'in_band_db': np.round(10.0 * np.log10(self._band_in / n + 1e-12), 1).tolist(),
                'out_band_db': np.round(10.0 * np.log10(self._band_out / n + 1e-12), 1).tolist(), 'final': final}

    def _bang_summary(self):
        overall = [b['overall_db'] for b in self.bangs]
        med = float(np.median(overall))
        return {'n': len(self.bangs), 'median_db': round(med, 1), 'median_pct': self.pct(med),
                'min_db': round(min(overall), 1), 'min_pct': self.pct(min(overall)),
                'band_db': np.round(np.median([b['reduction_db'] for b in self.bangs], axis=0), 1).tolist()}

    def view(self):
        if self.bangs and (self._summary is None or self._summary['n'] != len(self.bangs)
                           or self._summary_last is not self.bangs[-1]):
            self._summary, self._summary_last = self._bang_summary(), self.bangs[-1]
        return {'bands': self.BANDS, 'window_s': self.AVG_WINDOW_FRAMES * self.FRAME / self.FS,
                'live': {'in_db': np.round(10.0 * np.log10(self._live_in), 1).tolist(),
                         'out_db': np.round(10.0 * np.log10(self._live_out), 1).tolist()},
                'bang': self.bangs[-1] if self.bangs else None, 'bangs': self._summary if self.bangs else None,
                'current': self._window_record(final=False),
                'windows': [{k: v for k, v in w.items() if not k.endswith('band_db')} for w in self.windows[:-1]]
                + self.windows[-1:]}


class DynamicAdaptiveMitigator:
    SAMPLE_RATE = 48000
    FRAME_SAMPLES = 480
    DELAY_SAMPLES = 960
    SOURCE_COMMIT = '70f1d256acd4b34a572f999a05c87bf00b67730d'
    MODEL_SHA256 = '0a8755f8e2d834eff6a54714ecc7d75f9932e845df35f8b59bc52a7cfe6e8b37'
    # Pop guard: a 2.5 ms slice more than 20 dB above the loudest slice of the
    # previous 50 ms, and above -35 dBFS, mutes the dry floor for 300 ms.
    POP_SLICE_SAMPLES = 120
    POP_HISTORY_SLICES = 20
    POP_JUMP_DB = 20.0
    POP_MIN_DBFS = -35.0
    POP_HOLD_MS = 300
    # Localhost control bridge (dashboard): metrics out at 10 Hz, settings in, both non-blocking.
    METRICS_ADDRESS = ('127.0.0.1', 5005)
    SETTINGS_ADDRESS = ('127.0.0.1', 5006)
    METRICS_EVERY_FRAMES = 10
    SETTING_SWITCHES = ('bypass', 'clip_guard', 'pop_guard_enabled', 'recording')
    SETTING_RANGES = {'TARGET_SPEECH_DBFS': (-30.0, -15.0), 'FLOOR_MIN_DB': (6.0, 30.0),
                      'FLOOR_MAX_DB': (12.0, 40.0), 'POP_JUMP_DB': (12.0, 30.0), 'POP_HOLD_FRAMES': (15, 60),
                      'SUSTAIN_DUCK_DB': (6.0, 30.0), 'SUSTAIN_FADE_S': (0.5, 5.0), 'ALARM_MIN_OUT_DBFS': (-40.0, -15.0), 'ALARM_MAX_BOOST_DB': (0.0, 20.0), 'BANG_DUCK_DB': (10.0, 50.0), 'BANG_OVER_SPEECH_DB': (4.0, 20.0), 'SUSTAIN_STEADY_FRAMES': (10, 30), 'EQ_100': (-12.0, 12.0), 'EQ_400': (-12.0, 12.0),
                      'EQ_1000': (-12.0, 12.0), 'EQ_2500': (-12.0, 12.0), 'EQ_5000': (-12.0, 12.0)}
    # Test versions selectable from the dashboard. Test 5 is the default and recommended version.
    #   1: simple speech rule (any RNNoise frame >= 0.3 opens the ambient path; gain up to +18 dB)
    #   2: sustained-voice rule, no background boost (AdaptivePostProcessor.sustained_voice)
    #   3: Test 2 plus the sustained-loud-sound (alarm) guard (AdaptivePostProcessor.sustained_guard)
    #   4: Test 3 plus the audible-alarm floor (AdaptivePostProcessor.alarm_floor)
    #   5: Test 4 plus bangs found and ducked during speech (AdaptivePostProcessor.speech_bang)
    TEST_VERSIONS = {1: {'sustained_voice': False, 'sustained_guard': False, 'alarm_floor': False, 'speech_bang': False},
                     2: {'sustained_voice': True, 'sustained_guard': False, 'alarm_floor': False, 'speech_bang': False},
                     3: {'sustained_voice': True, 'sustained_guard': True, 'alarm_floor': False, 'speech_bang': False},
                     4: {'sustained_voice': True, 'sustained_guard': True, 'alarm_floor': True, 'speech_bang': False},
                     5: {'sustained_voice': True, 'sustained_guard': True, 'alarm_floor': True, 'speech_bang': True}}

    def __init__(self, library_path=None, sample_rate=48000, clip_guard=False,
                 max_attenuation_db=None, pop_guard=False, adaptive=False):
        if sample_rate != self.SAMPLE_RATE:
            raise ValueError('This RNNoise model requires 48000 Hz; resample explicitly first.')
        self.fs = self.SAMPLE_RATE
        self.library_path = Path(library_path or Path(__file__).with_name('librnnoise.so')).resolve()
        metadata_path = self.library_path.with_suffix('.json')
        if not self.library_path.is_file() or not metadata_path.is_file():
            raise FileNotFoundError('Run build_rnnoise.py first, or pass its library_path; the .so and matching .json are required.')
        self.build_info = json.loads(metadata_path.read_text())
        expected = {'source_commit': self.SOURCE_COMMIT, 'model_archive_sha256': self.MODEL_SHA256,
                    'delay_samples': self.DELAY_SAMPLES, 'frame_samples': self.FRAME_SAMPLES}
        if any(self.build_info.get(key) != value for key, value in expected.items()):
            raise ValueError('The library metadata does not match the tested RNNoise revision and delay.')
        actual_hash = hashlib.sha256(self.library_path.read_bytes()).hexdigest()
        if actual_hash != self.build_info.get('library_sha256'):
            raise ValueError('The library hash does not match its build metadata; rebuild it.')
        self.lib = ctypes.CDLL(str(self.library_path))
        self.FloatPtr = ctypes.POINTER(ctypes.c_float)
        self.lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_create.restype = ctypes.c_void_p
        self.lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self.lib.rnnoise_destroy.restype = None
        self.lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p, self.FloatPtr, self.FloatPtr]
        self.lib.rnnoise_process_frame.restype = ctypes.c_float
        self.lib.rnnoise_get_frame_size.argtypes = []
        self.lib.rnnoise_get_frame_size.restype = ctypes.c_int
        if self.lib.rnnoise_get_frame_size() != self.FRAME_SAMPLES:
            raise ValueError('Unexpected RNNoise frame length.')
        self._states = []
        self._channels = None
        self.last_vad = None
        self.clip_guard = bool(clip_guard)
        self._clip_guard_default = self.clip_guard
        self._guard_frame = 0
        self._guard_triggers = []
        self.clip_guard_triggers = 0
        self.bypass = False
        self.recording = False
        self._recording_default = False
        self._inject = None                         # recording fed into the live input (dashboard playback)
        self._inject_gen = 0                        # bumped by every play/stop, so a late loader cannot restart
        self.live = {}
        if max_attenuation_db is not None and not max_attenuation_db > 0:
            raise ValueError('max_attenuation_db must be positive, or None to disable the floor.')
        self.max_attenuation_db = max_attenuation_db
        self.pop_guard = bool(pop_guard)
        self.adaptive = AdaptivePostProcessor() if adaptive else None
        self._reset_floor()
        self._reset_bypass()
        self._in = np.empty(self.FRAME_SAMPLES, dtype=np.float32)
        self._out = np.empty(self.FRAME_SAMPLES, dtype=np.float32)
        self._in_ptr = self._in.ctypes.data_as(self.FloatPtr)
        self._out_ptr = self._out.ctypes.data_as(self.FloatPtr)

    @staticmethod
    def _to_float(raw):
        if raw.dtype == np.uint8:
            return (raw.astype(np.float32) - 128.0) / 128.0
        if np.issubdtype(raw.dtype, np.signedinteger):
            return raw.astype(np.float32) / float(2 ** (8 * raw.dtype.itemsize - 1))
        if np.issubdtype(raw.dtype, np.floating):
            return raw.astype(np.float32)
        raise ValueError(f'Unsupported WAV sample type: {raw.dtype}')

    def reset(self):
        """Discard history. Call between recordings, never between live frames."""
        for state in self._states:
            self.lib.rnnoise_destroy(state)
        self._states = []
        self._channels = None
        self.last_vad = None
        self._guard_frame = 0
        self._guard_triggers = []
        self.clip_guard_triggers = 0
        self._reset_floor()
        self._reset_bypass()
        if getattr(self, 'adaptive', None) is not None:
            self.adaptive.reset()

    def _reset_floor(self):
        self._dry = None
        self._dry_gain = 0.0
        self._pop_history = np.full(self.POP_HISTORY_SLICES, 1e-10)
        self._pop_hold = 0
        self.pop_triggers = 0

    def _reset_bypass(self):
        self._bypass_delay = None
        self._bypass_mix = 1.0 if self.bypass else 0.0
        self._bypass_limiter = PeakLimiter(AdaptivePostProcessor.CEILING_DBFS, AdaptivePostProcessor.RELEASE_MS,
                                           self.FRAME_SAMPLES)

    def _apply_bypass(self, data, output):
        """Crossfade (2 frames) between the processed output and the input delayed by DELAY_SAMPLES.

        The delay line always runs so switching is instant and the delay stays 20 ms.
        While any dry signal is mixed in, the result goes through a peak limiter at the
        adaptive ceiling. With bypass off and the fade finished, output is returned unchanged.
        """
        if self._bypass_delay is None:
            self._bypass_delay = np.zeros((self.DELAY_SAMPLES, data.shape[1]), dtype=np.float32)
        dry = self._bypass_delay[:self.FRAME_SAMPLES]
        self._bypass_delay = np.concatenate([self._bypass_delay[self.FRAME_SAMPLES:], data.astype(np.float32)])
        target = 1.0 if self.bypass else 0.0
        if target == 0.0 and self._bypass_mix == 0.0:
            return output
        step = np.arange(1, self.FRAME_SAMPLES + 1, dtype=np.float32) / (2 * self.FRAME_SAMPLES)
        if target > self._bypass_mix:
            mix = np.minimum(target, self._bypass_mix + step)
        else:
            mix = np.maximum(target, self._bypass_mix - step)
        self._bypass_mix = float(mix[-1])
        mixed = output * (1.0 - mix[:, None]) + dry * mix[:, None]
        return self._bypass_limiter.process(mixed).astype(np.float32)

    RECORDINGS_DIR = Path(__file__).resolve().parent / 'dashboard' / 'recordings'
    INJECT_GAIN_RANGE = (-30.0, 10.0)

    def _load_inject(self, name, gain_db, keep_mic, gen):
        """Background thread: read a recording (disk I/O stays out of the audio loop), then hand it over."""
        try:
            path = (self.RECORDINGS_DIR / name).resolve()
            if path.parent != self.RECORDINGS_DIR.resolve() or path.suffix.lower() != '.wav':
                raise ValueError('not a recording')
            rate, raw = wav.read(path)
            if rate != self.fs:
                raise ValueError(f'expected {self.fs} Hz, got {rate}')
            gain = np.float32(10 ** (gain_db / 20))
            x = np.empty(len(raw), dtype=np.float32)
            for i in range(0, len(raw), self.fs // 4):   # small steps, so the audio loop never waits long
                part = self._to_float(raw[i:i + self.fs // 4])
                x[i:i + len(part)] = (part.mean(axis=1) if part.ndim > 1 else part) * gain
                time.sleep(0)
            if gen == self._inject_gen:
                self._inject = {'x': x, 'pos': 0, 'name': name, 'keep_mic': keep_mic, 'gain_db': gain_db}
        except Exception as error:                   # reported in the log; the audio carries on untouched
            print(f'Recording playback: cannot load {name!r} ({error}).')

    def _mix_inject(self, data):
        """Add the next frame of the playing recording to the live input (or replace the mic)."""
        inj = self._inject
        if inj is None:
            return data
        pos, x = inj['pos'], inj['x']
        chunk = x[pos:pos + len(data)]
        out = data.copy() if inj['keep_mic'] else np.zeros_like(data)
        out[:len(chunk)] += chunk[:, None]
        np.clip(out, -1.0, 1.0, out=out)               # like a real input: it cannot go past full scale
        inj['pos'] = pos + len(data)
        if inj['pos'] >= len(x):
            self._inject = None
        return out

    def inject_view(self):
        inj = self._inject
        if inj is None:
            return None
        return {'name': inj['name'], 'pos_s': round(inj['pos'] / self.fs, 1), 'len_s': round(len(inj['x']) / self.fs, 1),
                'keep_mic': inj['keep_mic'], 'gain_db': inj['gain_db']}

    def apply_settings(self, packet):
        """Apply a JSON settings packet (bytes, str or dict) between frames; returns what was applied.

        Only SETTING_SWITCHES (bools), SETTING_RANGES (numbers, clamped) and reset_defaults
        are accepted; anything else is ignored. FLOOR_MIN_DB <= FLOOR_MAX_DB is kept.
        """
        try:
            settings = packet if isinstance(packet, dict) else json.loads(packet)
        except (ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(settings, dict):
            return {}
        applied = {}
        if settings.get('inject_stop') is True:
            self._inject_gen += 1
            self._inject = None
            applied['inject_stop'] = True
        name = settings.get('inject_play')
        if isinstance(name, str) and name:
            gain = settings.get('inject_gain_db', 0.0)
            gain = 0.0 if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) else gain
            gain = float(min(max(gain, self.INJECT_GAIN_RANGE[0]), self.INJECT_GAIN_RANGE[1]))
            keep = settings.get('inject_keep_mic', True) is not False
            self._inject_gen += 1
            self._inject = None
            threading.Thread(target=self._load_inject, args=(name, gain, keep, self._inject_gen), daemon=True).start()
            applied['inject_play'] = name
        a = self.adaptive
        version = settings.get('test_version')
        if a is not None and not isinstance(version, bool) and version in self.TEST_VERSIONS:
            for flag, value in self.TEST_VERSIONS[version].items():   # not touched by reset_defaults
                setattr(a, flag, value)
            if not a.sustained_guard:
                a._loud_active, a._alarm_mix = False, 0.0
            applied['test_version'] = int(version)
        if settings.get('reset_defaults') is True:
            if a is not None:
                for key in self.SETTING_RANGES:
                    a.__dict__.pop(key, None)
                a.pop_guard_enabled = True
            self.bypass = False
            self.clip_guard = self._clip_guard_default
            self.recording = self._recording_default
            applied['reset_defaults'] = True
        for key in self.SETTING_SWITCHES:
            value = settings.get(key)
            if not isinstance(value, bool):
                continue
            if key == 'pop_guard_enabled':
                if a is None:
                    continue
                a.pop_guard_enabled = value
            else:
                setattr(self, key, value)
            applied[key] = value
        if a is not None:
            for key, (low, high) in self.SETTING_RANGES.items():
                value = settings.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    continue
                value = min(max(value, low), high)
                value = int(round(value)) if isinstance(low, int) else float(value)
                setattr(a, key, value)
                applied[key] = value
            if a.FLOOR_MIN_DB > a.FLOOR_MAX_DB:
                if 'FLOOR_MIN_DB' in applied:
                    a.FLOOR_MAX_DB = applied['FLOOR_MAX_DB'] = float(a.FLOOR_MIN_DB)
                else:
                    a.FLOOR_MIN_DB = applied['FLOOR_MIN_DB'] = float(a.FLOOR_MAX_DB)
        return applied

    def test_version(self):
        a = self.adaptive
        if a is None:
            return None
        return next((v for v, flags in self.TEST_VERSIONS.items()
                     if all(getattr(a, f) == value for f, value in flags.items())), None)

    def metrics(self):
        """Snapshot for the dashboard: live loop stats, adaptive state, switches and tunables."""
        a = self.adaptive
        return {'time': time.time(), 'pid': os.getpid(), 'live': self.live,
                'adaptive': a.stats() if a is not None else None,
                'vad': float(np.max(self.last_vad)) if self.last_vad is not None else None,
                'voice_hold': bool(a is not None and a._hold > 0),
                'pop_triggers': a.pop_triggers if a is not None else self.pop_triggers,
                'pop_hold': bool(a._pop_hold > 0) if a is not None else self._pop_hold > 0,
                'alarm_active': bool(a is not None and a.sustained_guard and a._loud_active),
                'alarm_events': a.alarm_events if a is not None else 0,
                'clip_guard_triggers': self.clip_guard_triggers,
                'inject': self.inject_view(),
                'state': {'bypass': self.bypass, 'clip_guard': self.clip_guard, 'recording': self.recording,
                          'pop_guard_enabled': a.pop_guard_enabled if a is not None else self.pop_guard,
                          'test_version': self.test_version()},
                'settings': {k: getattr(a, k) for k in self.SETTING_RANGES} if a is not None else {},
                'defaults': {k: getattr(AdaptivePostProcessor, k) for k in self.SETTING_RANGES},
                'ranges': self.SETTING_RANGES}

    def _floor_mix(self, data):
        """Delayed dry input scaled to the attenuation floor, for adding to RNNoise output.

        The dry path is delayed by DELAY_SAMPLES so it lines up with RNNoise's
        output. A detected pop is known 20 ms before it leaves that delay, so
        the floor is ramped to zero (10 ms) before the pop reaches the output.
        """
        channels = data.shape[1]
        if self._dry is None:
            self._dry = np.zeros((self.DELAY_SAMPLES, channels), dtype=np.float32)
        target = 10.0 ** (-self.max_attenuation_db / 20.0)
        if self.pop_guard:
            slices = data.reshape(-1, self.POP_SLICE_SAMPLES, channels)
            min_power = 10.0 ** (self.POP_MIN_DBFS / 10.0)
            jump = 10.0 ** (self.POP_JUMP_DB / 10.0)
            for power in np.mean(slices.astype(np.float64) ** 2, axis=(1, 2)) + 1e-10:
                if power > min_power and power > jump * self._pop_history.max():
                    if self._pop_hold == 0:
                        self.pop_triggers += 1
                    # Covers the 2-frame delay plus the hold time.
                    self._pop_hold = self.POP_HOLD_MS // 10 + 3
                self._pop_history = np.roll(self._pop_history, -1)
                self._pop_history[-1] = power
            if self._pop_hold > 0:
                target = 0.0
                self._pop_hold -= 1
        dry = self._dry[:self.FRAME_SAMPLES]
        self._dry = np.concatenate([self._dry[self.FRAME_SAMPLES:], data.astype(np.float32)])
        ramp = np.linspace(self._dry_gain, target, self.FRAME_SAMPLES + 1, dtype=np.float32)[1:]
        self._dry_gain = target
        return dry * ramp[:, None]

    def _clip_guard_gain(self, data):
        """Envelope for the returned frame, whose input coordinates are f-2.

        A trigger needs one complete 10 ms input frame and has a 5 ms pre-attack.
        It is known before the corresponding samples leave RNNoise's delay.
        """
        a = self._guard_frame * self.FRAME_SAMPLES
        if np.count_nonzero(np.any(np.abs(data) >= 32767.0 / 32768.0, axis=1)) >= 3:
            self._guard_triggers.append(a)
            self.clip_guard_triggers += 1
        begin = a - self.DELAY_SAMPLES
        t = begin + np.arange(self.FRAME_SAMPLES)
        gain = np.ones(self.FRAME_SAMPLES)
        self._guard_triggers = [a for a in self._guard_triggers if a + 3360 > begin]
        for a in self._guard_triggers:
            start = a - 240
            end = a + self.FRAME_SAMPLES + 960
            active = (t >= max(start, 0)) & (t < end + 1920)
            local_t = t[active]
            local = np.full(len(local_t), .25)
            before = local_t < a
            after = local_t >= end
            local[before] = .25 + .75 * .5 * (1 + np.cos(np.pi * (local_t[before] - start) / 240))
            local[after] = .25 + .75 * .5 * (1 - np.cos(np.pi * (local_t[after] - end) / 1920))
            gain[active] = np.minimum(gain[active], local)
        self._guard_frame += 1
        return gain

    def close(self):
        self.reset()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        if hasattr(self, '_states'):
            self.close()

    def process_frame(self, data):
        """Process exactly 480 normalized float samples per channel.

        Shapes: (480,) mono or (480, 1/2) mono/stereo. The output has the same
        shape and 20 ms signal delay, plus up to 10 ms frame accumulation and device/compute costs.
        Keep this instance alive across frames. The optional clipping guard
        shares one envelope across channels. No input normalization, output
        clipping, or reference speech is used. One instance per stream;
        calls on an instance must be sequential, not concurrent.
        """
        data = np.asarray(data)
        if not np.issubdtype(data.dtype, np.floating):
            raise ValueError('process_frame expects normalized floating-point audio, not PCM integers.')
        mono = data.ndim == 1
        if mono:
            data = data[:, None]
        if data.ndim != 2 or data.shape[0] != self.FRAME_SAMPLES or data.shape[1] not in (1, 2):
            raise ValueError('Each frame must have shape (480,), (480, 1), or (480, 2).')
        if not np.all(np.isfinite(data)):
            raise ValueError('Input contains NaN or infinity.')
        if np.max(np.abs(data)) > 1.0:
            raise ValueError('Input must be normalized to [-1, 1]; this wrapper never rescales automatically.')
        channels = data.shape[1]
        if self._channels is None:
            try:
                for _ in range(channels):
                    state = self.lib.rnnoise_create(None)
                    if not state:
                        raise RuntimeError('RNNoise model initialization failed.')
                    self._states.append(state)
            except Exception:
                self.reset()
                raise
            self._channels = channels
        elif self._channels != channels:
            raise ValueError('Channel count changed mid-stream; call reset() before starting a new stream.')
        output = np.empty(data.shape, dtype=np.float32)
        self.last_vad = np.empty(channels, dtype=np.float32)
        for channel, state in enumerate(self._states):
            np.multiply(data[:, channel], 32768.0, out=self._in, casting='unsafe')
            self.last_vad[channel] = self.lib.rnnoise_process_frame(state, self._out_ptr, self._in_ptr)
            output[:, channel] = self._out / 32768.0
        if self.clip_guard:
            gain = self._clip_guard_gain(data)
            output = (output.astype(np.float64) * gain[:, None]).astype(np.float32)
        if self.adaptive is not None:
            output = self.adaptive.process(data, output, float(np.max(self.last_vad)))
        elif self.max_attenuation_db is not None:
            output = output + self._floor_mix(data)
        output = self._apply_bypass(data, output)
        return output[:, 0] if mono else output

    def process_realtime(self, input_device=None, output_device=None, channels=1,
                         duration_sec=None, latency='high', record_prefix=None,
                         record_max_sec=600.0, control_bridge=True, prime_frames=0, tap_path=None):
        """Live microphone -> the same persistent processor -> audio output.

        Uses blocking full-duplex I/O, not a Python audio callback. Each block
        contains 480 samples/channel at 48 kHz. This is a soft-real-time test loop:
        scheduling, hardware buffers and processing must be checked on the Pi.
        Ctrl+C stops the loop. No plotting, disk writes or per-frame printing.
        With record_prefix, input and output are kept in memory and written to
        <prefix>_input.wav and <prefix>_output.wav (PCM16) after the stream closes;
        only the first record_max_sec seconds are kept, so memory stays bounded.
        SIGTERM (e.g. systemctl stop) stops the loop like Ctrl+C.
        self.live holds per-frame loop statistics. With control_bridge, a JSON metrics()
        packet goes to METRICS_ADDRESS every METRICS_EVERY_FRAMES frames and settings packets
        from SETTINGS_ADDRESS are applied between frames; all socket calls are non-blocking
        and happen after the frame is written. self.recording can be toggled live; when
        turned on without a record_prefix, one is made from the start time.
        """
        import sounddevice as sd

        if channels not in (1, 2):
            raise ValueError('Use one or two channels supported by BOTH devices.')
        if duration_sec is not None and (not np.isfinite(duration_sec) or duration_sec <= 0):
            raise ValueError('duration_sec must be positive, or None for Ctrl+C.')
        n_frames = (None if duration_sec is None else
                    int(np.ceil(duration_sec * self.fs / self.FRAME_SAMPLES)))
        sd.check_input_settings(device=input_device, channels=channels,
                                dtype='float32', samplerate=self.fs)
        sd.check_output_settings(device=output_device, channels=channels,
                                 dtype='float32', samplerate=self.fs)

        n_samples = 0
        blocks = 0
        input_overflows = 0
        output_underflows = 0
        input_rail_samples = 0
        output_clipped_samples = 0
        processing_seconds = 0.0
        maximum_processing_seconds = 0.0
        over_budget_blocks = 0
        frame_seconds = self.FRAME_SAMPLES / self.fs
        stopped_by = 'duration'
        recorded_in, recorded_out = [], []
        record_max_blocks = int(record_max_sec * self.fs / self.FRAME_SAMPLES)
        self._recording_default = self.recording = record_prefix is not None
        start_stamp = time.strftime('live_%Y%m%d_%H%M%S')
        recent = [0.0] * 100
        recent_sum = 0.0
        block_sum = block_max = 0.0                # 10 s blocks of processing time, for the battery log
        window = []
        meter = AttenuationMeter()
        spectrum = SpectrumMeter()
        bridge_seconds_max = 0.0
        tx = rx = None
        if control_bridge:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.setblocking(False)
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.setblocking(False)
            try:
                rx.bind(self.SETTINGS_ADDRESS)
            except OSError as error:
                print(f'Control bridge: cannot listen on {self.SETTINGS_ADDRESS} ({error}); settings disabled.')
                rx.close()
                rx = None

        tap = None
        if tap_path:
            try:
                tap = AudioTap(tap_path, self.FRAME_SAMPLES, self.fs)
            except OSError as error:
                print(f'Audio tap: cannot create {tap_path} ({error}); recorders disabled.')
        sigterm_received = []

        def stop_on_sigterm(signum, frame):
            sigterm_received.append(signum)
            raise KeyboardInterrupt
        previous_sigterm = signal.signal(signal.SIGTERM, stop_on_sigterm)
        print('Live processing: 480 samples/channel at 48000 Hz; Ctrl+C to stop.')
        print('Clipping guard:', 'ON (stronger attenuation)' if self.clip_guard else 'OFF')
        self.reset()
        start = time.perf_counter()
        try:
            with sd.Stream(samplerate=self.fs, blocksize=self.FRAME_SAMPLES,
                           device=(input_device, output_device), channels=channels,
                           dtype='float32', latency=latency) as stream:
                input_latency, output_latency = stream.latency
                for _ in range(prime_frames):    # silent cushion: small buffers start full instead of empty
                    stream.write(np.zeros((self.FRAME_SAMPLES, channels), dtype=np.float32))
                self.live = {'frames': 0, 'input_overflows': 0, 'output_underflows': 0,
                             'processing_ms_mean_1s': 0.0, 'processing_ms_max': 0.0, 'over_budget_frames': 0,
                             'input_rail_samples': 0, 'output_clipped_samples': 0,
                             'device_input_latency_ms': 1000.0 * input_latency,
                             'device_output_latency_ms': 1000.0 * output_latency,
                             'latency_setting': str(latency), 'prime_frames': prime_frames,
                             'algorithm_delay_ms': 1000.0 * self.DELAY_SAMPLES / self.fs,
                             'frame_ms': 1000.0 * frame_seconds, 'bridge_ms_max': 0.0,
                             'in_rms_dbfs': -120.0, 'in_peak_dbfs': -120.0,
                             'out_rms_dbfs': -120.0, 'out_peak_dbfs': -120.0}
                live = self.live
                try:
                    while n_frames is None or blocks < n_frames:
                        data, overflowed = stream.read(self.FRAME_SAMPLES)
                        input_overflows += int(overflowed)
                        if self._inject is not None:
                            data = self._mix_inject(data)
                        begin = time.perf_counter()
                        processed = self.process_frame(data)
                        if not np.all(np.isfinite(processed)):
                            raise RuntimeError('Non-finite processor output; stopping audio.')
                        input_rail_samples += int(np.count_nonzero(np.abs(data) >= 32767.0 / 32768.0))
                        output_clipped_samples += int(np.count_nonzero(
                            (processed < -1.0) | (processed > 32767.0 / 32768.0)))
                        # Bound the device output. This is not loudness normalization.
                        np.clip(processed, -1.0, 32767.0 / 32768.0, out=processed)
                        elapsed = time.perf_counter() - begin
                        underflowed = stream.write(processed)
                        if self.recording and len(recorded_in) < record_max_blocks:
                            recorded_in.append(data.copy())
                            recorded_out.append(processed.copy())
                        # Keep timing statistics aligned with completed blocks.
                        processing_seconds += elapsed
                        maximum_processing_seconds = max(maximum_processing_seconds, elapsed)
                        over_budget_blocks += int(elapsed > frame_seconds)
                        output_underflows += int(underflowed)
                        n_samples += len(data)
                        recent_sum += elapsed - recent[blocks % 100]
                        recent[blocks % 100] = elapsed
                        blocks += 1
                        block_sum += elapsed
                        block_max = max(block_max, elapsed)
                        if blocks % 1000 == 0:
                            live['processing_ms_mean_10s'] = 1000.0 * block_sum / 1000
                            live['processing_ms_max_10s'] = 1000.0 * block_max
                            block_sum = block_max = 0.0

                        bridge_begin = time.perf_counter()
                        in_power = float(np.mean(np.square(data)))
                        out_power = float(np.mean(np.square(processed)))
                        in_peak = float(np.max(np.abs(data)))
                        out_peak = float(np.max(np.abs(processed)))
                        live.update(
                            frames=blocks, input_overflows=input_overflows, output_underflows=output_underflows,
                            processing_ms_mean_1s=1000.0 * recent_sum / min(blocks, 100),
                            processing_ms_max=1000.0 * maximum_processing_seconds,
                            over_budget_frames=over_budget_blocks, input_rail_samples=input_rail_samples,
                            output_clipped_samples=output_clipped_samples,
                            in_rms_dbfs=_dbfs(in_power, power=True), in_peak_dbfs=_dbfs(in_peak),
                            out_rms_dbfs=_dbfs(out_power, power=True), out_peak_dbfs=_dbfs(out_peak))
                        if rx is not None:
                            for _ in range(8):
                                try:
                                    packet = rx.recv(4096, socket.MSG_DONTWAIT)
                                except OSError:
                                    break
                                self.apply_settings(packet)
                        ad = self.adaptive
                        meter.update(in_peak, out_peak, in_power, out_power,
                                     active=bool(ad._pop_hold > 0) if ad is not None else self._pop_hold > 0,
                                     alarm=bool(ad is not None and ad.sustained_guard and ad._loud_active
                                                and ad._loud_frames >= ad.SUSTAIN_EVENT_FRAMES),
                                     voice=bool(ad is not None and ad._hold > 0),
                                     triggers=ad.pop_triggers if ad is not None else self.pop_triggers)
                        spectrum.update(data, processed, in_power, out_power,
                                        triggers=ad.pop_triggers if ad is not None else self.pop_triggers)
                        if tap is not None:
                            flags = (self.bypass << 4) | (1 if ad is not None and ad._hold > 0 else 0)
                            if ad is not None:
                                flags |= (2 if getattr(ad, 'voiced_now', False) else 0) | (4 if ad._pop_hold > 0 else 0) \
                                    | (8 if ad.sustained_guard and ad._loud_active else 0)
                            tap.write(blocks - 1, data, processed, ad.pop_triggers if ad is not None else self.pop_triggers,
                                      flags, float(np.max(self.last_vad)) if self.last_vad is not None else 0.0,
                                      self.test_version() or 0)
                        if tx is not None:
                            window.append((in_power, in_peak, out_power, out_peak))
                            if blocks % self.METRICS_EVERY_FRAMES == 0:
                                columns = list(zip(*window))
                                window = []
                                packet = self.metrics()
                                packet['attenuation'] = meter.view()
                                packet['spectrum'] = spectrum.view()
                                packet['window'] = {'in_rms_dbfs': _dbfs(sum(columns[0]) / len(columns[0]), power=True),
                                                    'in_peak_dbfs': _dbfs(max(columns[1])),
                                                    'out_rms_dbfs': _dbfs(sum(columns[2]) / len(columns[2]), power=True),
                                                    'out_peak_dbfs': _dbfs(max(columns[3]))}
                                try:
                                    tx.sendto(json.dumps(packet).encode(), self.METRICS_ADDRESS)
                                except OSError:
                                    pass
                        bridge_seconds_max = max(bridge_seconds_max, time.perf_counter() - bridge_begin)
                        live['bridge_ms_max'] = 1000.0 * bridge_seconds_max
                except KeyboardInterrupt:
                    stopped_by = 'SIGTERM' if sigterm_received else 'Ctrl+C'
                    stream.abort(ignore_errors=True)
                except BaseException:
                    # Discard queued output on failure; never bypass raw audio.
                    stream.abort(ignore_errors=True)
                    raise
        finally:
            # The stream context stops/closes hardware before model state is freed.
            signal.signal(signal.SIGTERM, previous_sigterm)
            for sock in (tx, rx):
                if sock is not None:
                    sock.close()
            pop_triggers = self.pop_triggers
            adaptive_stats = self.adaptive.stats() if self.adaptive is not None else None
            self.reset()
        elapsed = time.perf_counter() - start
        recordings = {}
        if recorded_in:
            record_prefix = record_prefix or start_stamp
            for name, blocks_list in (('input', recorded_in), ('output', recorded_out)):
                audio = np.clip(np.concatenate(blocks_list), -1.0, 32767.0 / 32768.0)
                path = f'{record_prefix}_{name}.wav'
                wav.write(path, self.fs, np.round(audio * 32768.0).astype(np.int16))
                recordings[name] = path
        return {'sample_rate_hz': self.fs, 'channels': channels,
                'samples': n_samples, 'processed_blocks': blocks,
                'audio_seconds': n_samples / self.fs, 'wall_seconds': elapsed,
                'frame_samples': self.FRAME_SAMPLES, 'frame_budget_ms': 10.0,
                'mean_processing_ms': 1000.0 * processing_seconds / max(blocks, 1),
                'maximum_processing_ms': 1000.0 * maximum_processing_seconds,
                'processing_over_budget_blocks': over_budget_blocks,
                'input_overflow_blocks': input_overflows,
                'output_underflow_blocks': output_underflows,
                'input_rail_channel_samples': input_rail_samples,
                'output_clipped_channel_samples': output_clipped_samples,
                'algorithm_signal_delay_ms': 20.0,
                'device_reported_input_latency_ms': 1000.0 * input_latency,
                'device_reported_output_latency_ms': 1000.0 * output_latency,
                'latency_note': '20 ms is the algorithm delay, not measured end-to-end latency.',
                'clip_guard_enabled': self.clip_guard, 'stopped_by': stopped_by,
                'max_attenuation_db': None if self.adaptive else self.max_attenuation_db,
                'pop_guard': None if self.adaptive else self.pop_guard,
                'pop_guard_triggers': None if self.adaptive else pop_triggers, 'adaptive': adaptive_stats,
                'recordings': recordings, 'bridge_ms_max': 1000.0 * bridge_seconds_max,
                'source_commit': self.SOURCE_COMMIT,
                'library_sha256': self.build_info['library_sha256']}

    def process_offline_file(self, input_filename, output_filename, *, float_output_filename=None):
        """Write PCM16 output, preserving sample count/rate/channels.

        The optional float WAV retains the raw model output for numerical
        analysis. PCM16 export rounds and clips only values beyond its range;
        the returned diagnostics count any such values. The clean reference
        is never an input to this processor.
        """
        fs, raw = wav.read(input_filename)
        if fs != self.SAMPLE_RATE:
            raise ValueError('Input WAV must be 48000 Hz; resample explicitly first.')
        data = self._to_float(raw)
        mono = data.ndim == 1
        if mono:
            data = data[:, None]
        if data.ndim != 2 or data.shape[1] not in (1, 2) or len(data) == 0:
            raise ValueError('Expect a non-empty mono or stereo WAV.')
        if not np.all(np.isfinite(data)) or np.max(np.abs(data)) > 1.0:
            raise ValueError('Input WAV must contain finite samples in [-1, 1].')
        n_samples, channels = data.shape
        blocks = (n_samples + self.FRAME_SAMPLES - 1) // self.FRAME_SAMPLES + 2
        padded = np.zeros((blocks * self.FRAME_SAMPLES, channels), dtype=np.float32)
        padded[:n_samples] = data
        output = np.empty_like(padded)
        self.reset()
        start = time.perf_counter()
        try:
            for index in range(blocks):
                begin = index * self.FRAME_SAMPLES
                output[begin:begin + self.FRAME_SAMPLES] = self.process_frame(
                    padded[begin:begin + self.FRAME_SAMPLES])
        finally:
            self.reset()
        elapsed = time.perf_counter() - start
        output = output[self.DELAY_SAMPLES:self.DELAY_SAMPLES + n_samples]
        if mono:
            output = output[:, 0]
        if not np.all(np.isfinite(output)):
            raise RuntimeError('RNNoise returned non-finite output.')
        clipped = int(np.count_nonzero((output < -1.0) | (output > 32767.0 / 32768.0)))
        pcm = np.rint(np.clip(output, -1.0, 32767.0 / 32768.0) * 32768.0).astype(np.int16)
        wav.write(output_filename, fs, pcm)
        if float_output_filename is not None:
            wav.write(float_output_filename, fs, output)
        return {'input_filename': str(input_filename), 'output_filename': str(output_filename),
                'sample_rate_hz': fs, 'samples': n_samples, 'channels': channels,
                'frame_samples': self.FRAME_SAMPLES, 'compensated_delay_samples': self.DELAY_SAMPLES,
                'signal_delay_ms': 20.0, 'frame_duration_ms': 10.0, 'core_wall_seconds': elapsed,
                'real_time_factor': elapsed / (n_samples / fs),
                'output_peak': float(np.max(np.abs(output))), 'pcm_clipped_values': clipped,
                'clip_guard_enabled': self.clip_guard,
                'source_commit': self.SOURCE_COMMIT, 'library_sha256': self.build_info['library_sha256']}


if __name__ == '__main__':
    # Settings for running by hand. The dashboard overrides them through SUKOON_* environment variables.
    MODE = 'realtime'             # 'realtime', 'offline', or 'devices'
    CLIP_GUARD = False            # True: stronger bang attenuation, more speech loss
    ADAPTIVE = True               # Self-adjusting floor, bang guard, speech gain and limiter; no per-room tuning
    MAX_ATTENUATION_DB = 15.0     # Used only when ADAPTIVE=False. None=RNNoise only (choppy speech)
    POP_GUARD = True              # Used only when ADAPTIVE=False
    INPUT_DEVICE = 'USB Audio'        # Device ID or name substring from MODE='devices'; None uses default
    OUTPUT_DEVICE = 'USB Audio'
    CHANNELS = 1                 # 1=mono; 2=stereo if both devices support it
    DURATION_SEC = None           # None=run continuously until Ctrl+C/SIGTERM; e.g. 30.0 = 30 seconds
    LATENCY = 'high'              # Start here; try 'low' after a stable test
    PRIME_FRAMES = 0              # silent frames written first (use 3 with LATENCY=0.01)
    if os.environ.get('SUKOON_LATENCY'):                 # set by the dashboard's Buffer choice
        LATENCY = os.environ['SUKOON_LATENCY']
        try:
            LATENCY = float(LATENCY)
        except ValueError:
            pass
        PRIME_FRAMES = int(os.environ.get('SUKOON_PRIME', '0'))
    RECORD_PREFIX = time.strftime('live_%Y%m%d_%H%M%S')  # None=don't save; else <prefix>_input/_output.wav
    if os.environ.get('SUKOON_NO_RECORD') == '1':      # Set by the dashboard: start with recording off
        RECORD_PREFIX = None
    RECORD_MAX_SEC = 600.0        # Only the first 10 minutes are kept, so long runs don't fill memory
    CONTROL_BRIDGE = True         # Dashboard link: metrics to UDP 127.0.0.1:5005, settings from 127.0.0.1:5006
    TAP_PATH = os.environ.get('SUKOON_TAP') or None    # set by the dashboard: /dev/shm ring for recorders
    input_filename = 'Bf8_mix.wav'
    output_filename = 'processed_output.wav'

    if os.environ.get('SUKOON_DEVICE'):                 # set by the dashboard: the active card, e.g. 'hw:1,0'
        INPUT_DEVICE = OUTPUT_DEVICE = os.environ['SUKOON_DEVICE']
    for key, attr in (('SUKOON_METRICS_PORT', 'METRICS_ADDRESS'), ('SUKOON_SETTINGS_PORT', 'SETTINGS_ADDRESS')):
        if os.environ.get(key, '').isdigit():              # a second dashboard instance for testing
            setattr(DynamicAdaptiveMitigator, attr, ('127.0.0.1', int(os.environ[key])))

    if MODE == 'devices':
        import sounddevice as sd
        print(sd.query_devices())
    elif MODE in ('realtime', 'offline'):
        with DynamicAdaptiveMitigator(clip_guard=CLIP_GUARD,
                                      max_attenuation_db=MAX_ATTENUATION_DB,
                                      pop_guard=POP_GUARD, adaptive=ADAPTIVE) as mitigator:
            version = os.environ.get('SUKOON_TEST_VERSION', '')       # set by the dashboard's test-version list
            mitigator.apply_settings({'test_version': int(version) if version.isdigit() else 5})
            if MODE == 'realtime':
                info = mitigator.process_realtime(
                    input_device=INPUT_DEVICE, output_device=OUTPUT_DEVICE,
                    channels=CHANNELS, duration_sec=DURATION_SEC, latency=LATENCY, prime_frames=PRIME_FRAMES,
                    record_prefix=RECORD_PREFIX, record_max_sec=RECORD_MAX_SEC,
                    control_bridge=CONTROL_BRIDGE, tap_path=TAP_PATH)
            else:
                info = mitigator.process_offline_file(
                    input_filename=input_filename, output_filename=output_filename,
                    float_output_filename='processed_output_float.wav')
        print(json.dumps(info, indent=2))
    else:
        raise ValueError("MODE must be 'realtime', 'offline', or 'devices'.")
