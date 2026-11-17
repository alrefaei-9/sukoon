"""Short-lived audio helper for the dashboard's system check (runs only while sukoon_engine.py is off).

  audio_check.py mic          record 1 s and print a JSON verdict on the microphone signal
  audio_check.py play FILE    play a 48 kHz WAV on the sound card
  audio_check.py latency [LATENCY PRIME]
                              measure the real delay: quiet chirps out of the earbud, found again in the
                              microphone (hold the earbud against the mic). Same blocking 10 ms loop as sukoon_engine.py.

A fresh process each time, so PortAudio sees a card that was just plugged in.
The device is the active card ('hw:N,0' from env SUKOON_DEVICE, else chosen by USB id with soundcards.py).
"""
import json, os, sys
import numpy as np
import scipy.io.wavfile as w
import sounddevice as sd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # -I leaves the script's folder off the path
import soundcards


def _default_device():
    """The dashboard passes the active card ('hw:N,0'); run by hand, pick it the same way (by USB id)."""
    card, _ = soundcards.choose(soundcards.list_cards())
    return card['device'] if card else 'USB Audio'


DEVICE = os.environ.get('SUKOON_DEVICE') or _default_device()
FS = 48000
DEAD_DBFS = -90.0          # quieter than this: no signal at all
MAX_CLIPPED = 0.01         # more than 1% of samples at full scale: overloaded


def check_mic():
    sd.default.device = (DEVICE, DEVICE)
    x = sd.rec(int(FS * 1.2), samplerate=FS, channels=1, dtype='int16', blocking=True)[int(FS * 0.2):, 0]
    rms = float(np.sqrt(np.mean((x.astype(np.float64) / 32768) ** 2)))
    rms_dbfs = round(20 * np.log10(max(rms, 1e-9)), 1)
    clipped = float(np.mean(np.abs(x.astype(np.int32)) >= 32767))
    distinct = int(len(np.unique(x)))
    if rms_dbfs < DEAD_DBFS or distinct < 4:
        ok, detail = False, 'No signal from the microphone input'
    elif clipped > MAX_CLIPPED:
        ok, detail = False, 'Microphone input is overloaded'
    else:
        ok, detail = True, 'Signal OK'
    return {'ok': ok, 'detail': detail, 'rms_dbfs': rms_dbfs, 'clipped_pct': round(100 * clipped, 2)}


ALGORITHM_DELAY_MS = 20.0   # RNNoise delay, not part of the acoustic round trip


SWEEP_SNR_DB = 13.0     # a sweep counts as heard when its match peak is this far above the floor
CLUSTER_MS = 15.0       # heard sweeps must sit within this of their median delay
MIN_SWEEPS = 5          # of 10, so a chance cluster of noise peaks cannot pass


def chirp(seconds=0.02, f0=1000.0, f1=4000.0, level_dbfs=-24.0):
    t = np.arange(int(FS * seconds)) / FS
    x = np.sin(2 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2 * seconds))) * np.hanning(len(t))
    return 10 ** (level_dbfs / 20) * x / np.abs(x).max()


def measure_latency(latency='high', prime=0, count=10, spacing_s=0.35, warmup_s=1.0):
    """Round trip of the same blocking loop sukoon_engine.py runs. Works with the headphones worn: only a faint leak
    from earbud to mic is needed: each 100 ms sweep is found by whitened (GCC-PHAT) matching. The loop's delay
    wanders by a few ms during a run, so every sweep is timed on its own and the median and range reported.
    The first sweep waits warmup_s so the stream has settled, as it has during normal use."""
    probe = chirp(seconds=0.1, f0=500.0, f1=4000.0, level_dbfs=-24.0)
    total = int(FS * (warmup_s + count * spacing_s + 0.5))
    total -= total % 480
    track = np.zeros(total, dtype=np.float32)
    starts = [int(FS * (warmup_s + k * spacing_s)) for k in range(count)]
    for start in starts:
        track[start:start + len(probe)] = probe
    recorded = np.zeros(total, dtype=np.float32)
    with sd.Stream(samplerate=FS, blocksize=480, device=(DEVICE, DEVICE), channels=1, dtype='float32',
                   latency=latency) as stream:
        for _ in range(prime):
            stream.write(np.zeros((480, 1), dtype=np.float32))
        for i in range(total // 480):
            data, _ = stream.read(480)
            recorded[i * 480:(i + 1) * 480] = data[:, 0]
            stream.write(track[i * 480:(i + 1) * 480, None])
        reported = [round(1000 * v, 1) for v in stream.latency]
    return analyse_latency(recorded, probe, starts, latency, prime, reported)


def _peak(segments, probe):
    """GCC-PHAT of the averaged segments against the sweep, limited to the sweep's band: whitening keeps steady
    hum or tonal noise (AC, fans) from hiding the peak. Returns (lag in samples, peak over floor in dB)."""
    avg = np.mean(segments, axis=0)
    n = 1 << int(np.ceil(np.log2(len(avg) + len(probe))))
    cross = np.fft.rfft(avg, n) * np.conj(np.fft.rfft(probe, n))
    freqs = np.fft.rfftfreq(n, 1 / FS)
    band = (freqs >= 600) & (freqs <= 3900)
    cross = np.where(band, cross / (np.abs(cross) + 1e-12), 0)
    gcc = np.abs(np.fft.irfft(cross, n)[:len(avg) - len(probe) + 1])
    k = int(np.argmax(gcc))
    guard = int(FS * 0.003)                       # leave the peak itself out of the floor estimate
    rest = np.concatenate([gcc[:max(k - guard, 0)], gcc[k + guard:]])
    floor = float(np.sqrt(np.mean(rest ** 2))) + 1e-12
    return k, 20 * np.log10(gcc[k] / floor)


def analyse_latency(recorded, probe, starts, latency='high', prime=0, reported=None):
    window = int(FS * 0.25)                       # look up to 250 ms after each sweep was written
    found = [_peak(recorded[s:s + window + len(probe)][None, :], probe) for s in starts]
    lags = np.array([k / FS * 1000.0 for k, _ in found])
    snrs = np.array([snr for _, snr in found])
    heard = snrs >= SWEEP_SNR_DB
    inliers = np.zeros(len(lags), bool)
    if heard.any():                               # noise peaks land anywhere in 250 ms; real ones cluster
        centre = float(np.median(lags[heard]))
        inliers = heard & (np.abs(lags - centre) <= CLUSTER_MS)
    base = {'lags_ms': [round(v, 1) for v in lags], 'snr_db': [round(v, 1) for v in snrs],
            'mode_latency': str(latency), 'prime': prime}
    if inliers.sum() < MIN_SWEEPS:
        return {'ok': False, **base, 'detail': 'Sweeps not heard. Wear the headphones (or hold an earbud near '
                                               'the mic), keep the room quiet and try again.'}
    good = lags[inliers]
    round_trip = float(np.median(good))
    lo, hi = float(good.min()) + ALGORITHM_DELAY_MS, float(good.max()) + ALGORITHM_DELAY_MS
    return {'ok': True, **base, 'round_trip_ms': round(round_trip, 1),
            'mic_to_ear_ms': round(round_trip + ALGORITHM_DELAY_MS, 1),
            'range_ms': [round(lo, 1), round(hi, 1)], 'sweeps_used': int(inliers.sum()),
            'reported_buffers_ms': reported,
            'detail': f'{round_trip + ALGORITHM_DELAY_MS:.0f} ms mic to ear (varied {lo:.0f} to {hi:.0f} ms)'}


def play(path):
    rate, x = w.read(path)
    if rate != FS:
        raise SystemExit(f'{path}: expected {FS} Hz, got {rate}')
    sd.play(x, FS, device=DEVICE, blocking=True)
    return {'ok': True}


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'mic':
            result = check_mic()
        elif sys.argv[1] == 'latency':
            lat = sys.argv[2] if len(sys.argv) > 2 else 'high'
            try:
                lat = float(lat)
            except ValueError:
                pass
            result = measure_latency(lat, int(sys.argv[3]) if len(sys.argv) > 3 else 0)
        else:
            result = play(sys.argv[2])
    except Exception as error:                       # report device errors as a verdict, not a traceback
        result = {'ok': False, 'detail': f'Sound card error: {error}'}
    print(json.dumps(result))
