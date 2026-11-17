"""How much do the headphones attenuate on their own (Sukoon off, nothing playing in them)?

Measures the passive insertion loss of the earbuds per 1/3-octave band, 100 Hz to 10 kHz, and draws it as a
frequency response. Turn Sukoon OFF first (the USB card must be free if you use its mic).

Two-microphone method (recommended; any two mics the Pi can record from at once):
  1. OPEN:  both mics side by side at the ear position, no headphones. Calibrates the mics against each other.
  2. WORN:  INNER mic under the earbud (in the ear or a coupler), headphones on; OUTER mic just outside.
  Passive attenuation(f) = [outer - inner]_worn - [outer - inner]_open   (mic differences cancel out)

One-microphone method (--single): record at the ear position without, then with, the earbud on top.
  Needs the speaker level and position to stay exactly the same between the two recordings.

Sound source: pink noise from a speaker about 1 m away. Either the script plays it (--play DEVICE, e.g. a
speaker on the Pi's 3.5 mm jack: --play bcm2835) or it writes pink_noise.wav for you to play from a phone or
laptop (default) and waits for Enter.

  .venv/bin/python tools/passive_attenuation.py --list
  .venv/bin/python tools/passive_attenuation.py --outer "USB Audio" --inner "Other Mic"
  .venv/bin/python tools/passive_attenuation.py --outer "USB Audio" --outer-ch 0 --inner "USB Audio" --inner-ch 1
  .venv/bin/python tools/passive_attenuation.py --single "USB Audio" --play bcm2835
  .venv/bin/python tools/passive_attenuation.py --outer A --inner B --claps       # also: clap peaks, worn only

Results go to report/passive/<time>/: attenuation.csv, frequency_response.png, levels.png and the raw WAVs.

Dashboard (guided) mode, one step per call, JSON on the last line of stdout; cards are found by USB id
(dashboard/soundcards.py) so hw numbers never matter:
  passive_attenuation.py --step roles [--seconds 4]       record every USB card; the one tapped is the outer mic
  passive_attenuation.py --step record --session DIR --name open_silence|probe|open|worn_silence|worn|claps
                         [--play DEVICE --level DB]       record both mics (and play the pink noise at DB re file)
  passive_attenuation.py --step finish --session DIR [--no-recordings]
"""
import argparse, csv, json, sys, threading, time
from pathlib import Path
import numpy as np
import scipy.io.wavfile as wav
from scipy.signal import welch
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
FS = 48000
BANDS = [round(1000 * 2 ** (k / 3)) for k in range(-10, 11)]          # 100 Hz ... 10 kHz, 1/3 octave
MIN_SNR_DB = 10.0                                                      # band must be this far above silence


def pink_noise(seconds, level_dbfs=-12.0, seed=1):
    n = int(FS * seconds)
    spec = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / FS)
    spec[1:] /= np.sqrt(f[1:])
    spec[0] = 0
    x = np.fft.irfft(spec, n)
    x *= 10 ** (level_dbfs / 20) / np.sqrt(np.mean(x ** 2))
    ramp = int(0.05 * FS)
    x[:ramp] *= np.linspace(0, 1, ramp); x[-ramp:] *= np.linspace(1, 0, ramp)
    return np.clip(x, -0.99, 0.99).astype(np.float32)


def band_levels(x):
    """Level in dB (re full scale) of each 1/3-octave band."""
    f, p = welch(x, FS, nperseg=8192)
    out = []
    for fc in BANDS:
        sel = (f >= fc / 2 ** (1 / 6)) & (f < fc * 2 ** (1 / 6))
        out.append(10 * np.log10(np.sum(p[sel]) * (f[1] - f[0]) + 1e-20))
    return np.array(out)


def record(mics, seconds, stimulus=None, play_device=None):
    """Record every (device, channel) in mics at the same time; optionally play the stimulus meanwhile.

    Each device gets its own explicit stream. (sd.rec/sd.play share one global stream, so playing used to stop
    the recordings and leave them silent.)"""
    results = [None] * len(mics)
    by_device = {}
    for k, (dev, ch) in enumerate(mics):
        by_device.setdefault(dev, []).append((k, ch))
    n = int(FS * seconds)
    errors = []

    def grab(dev, slots):
        channels = max(ch for _, ch in slots) + 1
        try:
            with sd.InputStream(samplerate=FS, device=dev, channels=channels, dtype='float32') as stream:
                x, _ = stream.read(n)
            for k, ch in slots:
                results[k] = x[:, ch].copy()
        except Exception as error:
            errors.append(f'{dev}: {error}')

    def play():
        try:
            with sd.OutputStream(samplerate=FS, device=play_device, channels=1, dtype='float32') as stream:
                stream.write(np.ascontiguousarray(stimulus[:, None] if stimulus.ndim == 1 else stimulus, dtype=np.float32))
        except Exception as error:
            errors.append(f'{play_device}: {error}')
    threads = [threading.Thread(target=grab, args=(dev, slots)) for dev, slots in by_device.items()]
    for t in threads:
        t.start()
    player = None
    if stimulus is not None and play_device is not None:
        time.sleep(0.3)
        player = threading.Thread(target=play)
        player.start()
    for t in threads:
        t.join()
    if player is not None:
        player.join()
    if errors or any(r is None for r in results):
        raise RuntimeError('recording failed: ' + '; '.join(errors))
    return [r[int(0.5 * FS):] for r in results]           # drop the first 0.5 s (device start, ramp)


def take(name, prompt, mics, args, stimulus, out):
    """One measurement: silence (noise floor) then the pink noise, for every mic."""
    input(f'\n{prompt}\nKeep the room quiet. Press Enter to record 3 s of silence... ')
    silence = record(mics, 3.5)
    if args.play:
        input('Press Enter to play the pink noise and record... ')
        sig = record(mics, args.seconds + 0.5, stimulus, args.play)
    else:
        input('Start pink_noise.wav on the phone/laptop speaker now (same volume every time), then press Enter... ')
        sig = record(mics, args.seconds + 0.5)
    for k, (s, q) in enumerate(zip(sig, silence)):
        wav.write(out / f'{name}_mic{k}.wav', FS, (np.clip(s, -1, 1) * 32767).astype(np.int16))
        wav.write(out / f'{name}_mic{k}_silence.wav', FS, (np.clip(q, -1, 1) * 32767).astype(np.int16))
        rms = 20 * np.log10(np.sqrt(np.mean(s ** 2)) + 1e-12)
        clip = np.mean(np.abs(s) > 0.99) * 100
        print(f'  mic{k}: {rms:.1f} dBFS' + (f'  WARNING: {clip:.2f}% clipped, turn the speaker down' if clip > 0.01 else ''))
    return [band_levels(s) for s in sig], [band_levels(q) for q in silence]


def clap_test(mics, out, seconds=15.0):
    input(f'\nCLAPS (headphones worn, inner mic under the earbud). Press Enter, then clap ~10 times, 1 s apart, '
          f'about 1 m away ({seconds:.0f} s)... ')
    outer, inner = record(mics, seconds + 0.5)
    hop = FS // 100
    env = lambda s: 20 * np.log10(np.abs(s[:len(s) // hop * hop]).reshape(-1, hop).max(axis=1) + 1e-9)
    eo, ei = env(outer), env(inner)
    peaks, k = [], 0
    floor = np.median(eo)
    while k < len(eo):
        if eo[k] > floor + 20:
            seg = slice(max(k - 2, 0), k + 10)
            peaks.append((k / 100, eo[seg].max(), ei[seg].max()))
            k += 50
        else:
            k += 1
    wav.write(out / 'claps_outer.wav', FS, (np.clip(outer, -1, 1) * 32767).astype(np.int16))
    wav.write(out / 'claps_inner.wav', FS, (np.clip(inner, -1, 1) * 32767).astype(np.int16))
    return peaks


def resolve(dev):
    try:
        return int(dev)
    except (TypeError, ValueError):
        return dev


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--list', action='store_true', help='list audio devices and exit')
    p.add_argument('--outer'); p.add_argument('--inner'); p.add_argument('--single')
    p.add_argument('--outer-ch', type=int, default=0); p.add_argument('--inner-ch', type=int, default=0)
    p.add_argument('--play', help='output device for the pink noise (a speaker, not the earbuds)')
    p.add_argument('--seconds', type=float, default=8.0)
    p.add_argument('--claps', action='store_true', help='also measure clap peaks with the headphones worn')
    args = p.parse_args()
    if args.list:
        print(sd.query_devices()); return
    if not (args.single or (args.outer and args.inner)):
        p.error('give --outer and --inner (two mics), or --single (one mic)')
    if _sukoon_running():
        sys.exit('Sukoon is running and holds the sound card. Turn it off on the dashboard first.')

    out = ROOT / 'report' / 'passive' / time.strftime('%Y%m%d_%H%M%S')
    out.mkdir(parents=True, exist_ok=True)
    stimulus = pink_noise(args.seconds + 2.0)
    wav.write(out / 'pink_noise.wav', FS, (stimulus * 32767).astype(np.int16))
    wav.write(ROOT / 'pink_noise.wav', FS, (stimulus * 32767).astype(np.int16))
    if not args.play:
        print(f'Copy {ROOT / "pink_noise.wav"} to a phone or laptop and play it from a speaker about 1 m away.')

    if args.single:
        mics = [(resolve(args.single), args.outer_ch)]
        (bare,), (bare_q,) = take('bare', 'BARE: mic at the ear position, NO headphones.', mics, args, stimulus, out)
        (worn,), (worn_q,) = take('worn', 'WORN: same mic position, now with the earbud/headphone on top. '
                                          'Do not move the speaker or change its volume.', mics, args, stimulus, out)
        atten = bare - worn
        valid = (bare - bare_q >= MIN_SNR_DB) & (worn - worn_q >= MIN_SNR_DB)
        levels = {'Bare mic': bare, 'Under the earbud': worn, 'Silence (worn)': worn_q}
        method = 'one microphone, bare vs worn'
    else:
        mics = [(resolve(args.outer), args.outer_ch), (resolve(args.inner), args.inner_ch)]
        (o_open, i_open), (oq_open, iq_open) = take(
            'open', 'OPEN: put BOTH mics side by side at the ear position, NO headphones.', mics, args, stimulus, out)
        (o_worn, i_worn), (oq_worn, iq_worn) = take(
            'worn', 'WORN: INNER mic under the earbud, headphones on as normal; OUTER mic just outside the ear.',
            mics, args, stimulus, out)
        atten = (o_worn - i_worn) - (o_open - i_open)
        valid = ((i_worn - iq_worn) >= MIN_SNR_DB) & ((o_open - oq_open) >= MIN_SNR_DB) & ((i_open - iq_open) >= MIN_SNR_DB)
        levels = {'Outer mic (worn)': o_worn, 'Inner mic (worn)': i_worn, 'Inner mic silence': iq_worn}
        method = 'two microphones, insertion loss'

    with open(out / 'attenuation.csv', 'w', newline='') as fh:
        wr = csv.writer(fh)
        wr.writerow(['band_hz', 'passive_attenuation_db', 'reliable'] + [f'{k} (dB)' for k in levels])
        for j, fc in enumerate(BANDS):
            wr.writerow([fc, round(float(atten[j]), 1), bool(valid[j])] + [round(float(v[j]), 1) for v in levels.values()])
    good = atten[valid]
    print(f'\nPassive attenuation ({method}):')
    for fc, a, v in zip(BANDS, atten, valid):
        print(f'  {fc:>6} Hz  {a:6.1f} dB' + ('' if v else '   (below the noise floor: not reliable)'))
    if len(good):
        speech = valid & (np.array(BANDS) >= 300) & (np.array(BANDS) <= 4000)
        print(f'Average over reliable bands: {good.mean():.1f} dB'
              + (f'; speech range 300 Hz to 4 kHz: {atten[speech].mean():.1f} dB' if speech.any() else ''))
    peaks = clap_test(mics, out) if args.claps and not args.single else []
    if peaks:
        cal = float(np.mean(o_open - i_open))                       # broadband mic difference from the OPEN step
        red = [o - i - cal for _, o, i in peaks]
        print(f'Claps: {len(red)} found, peak reduction median {np.median(red):.1f} dB (range {min(red):.1f} to {max(red):.1f})')
    plot(out, atten, valid, levels, method, peaks, (float(np.mean(o_open - i_open)) if peaks else 0.0))
    print(f'\nSaved to {out}')


def _sukoon_running():
    import subprocess
    return subprocess.run(['pgrep', '-f', '[r]nnoise/code[.]py'], capture_output=True).returncode == 0


def plot(out, atten, valid, levels, method, peaks, cal):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    ink2, grid, c1, c2, c3 = '#52514e', '#e4e3df', '#2a78d6', '#eb6834', '#8a8984'
    plt.rcParams.update({'savefig.dpi': 300, 'font.size': 9.5, 'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.grid': True, 'grid.color': grid, 'axes.edgecolor': grid, 'axes.labelcolor': ink2,
                         'xtick.color': ink2, 'ytick.color': ink2, 'legend.frameon': False,
                         'axes.titleweight': 'bold', 'axes.titlelocation': 'left'})
    x = np.array(BANDS)
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    ax.semilogx(x, atten, color=c1, lw=1.0, alpha=0.5)
    ax.semilogx(x[valid], atten[valid], 'o-', color=c1, lw=2, ms=5, label='Passive attenuation')
    if (~valid).any():
        ax.semilogx(x[~valid], atten[~valid], 'o', color=c3, mfc='white', ms=5, label='Below noise floor (unreliable)')
    ax.axhline(0, color=ink2, lw=0.8)
    ax.set(xlabel='Frequency (Hz)', ylabel='Attenuation (dB)', xlim=(90, 11000))
    ax.set_xticks([125, 250, 500, 1000, 2000, 4000, 8000], ['125', '250', '500', '1k', '2k', '4k', '8k'])
    mean = atten[valid].mean() if valid.any() else float('nan')
    ax.set_title(f'Headphones alone: {mean:.0f} dB average passive attenuation', pad=22)
    ax.legend(loc='lower left', bbox_to_anchor=(0, 1.0), ncol=2, fontsize=8, borderaxespad=0.2)
    fig.text(0.01, 0.01, f'1/3-octave bands, pink noise, {method}. Sukoon off.', fontsize=7.5, color=ink2)
    fig.tight_layout(rect=(0, 0.04, 1, 1)); fig.savefig(out / 'frequency_response.png'); plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    for (name, v), c, ls in zip(levels.items(), (c2, c1, c3), ('-', '-', '--')):
        ax.semilogx(x, v, ls, color=c, lw=1.8, marker='o', ms=3.5, label=name)
    ax.set(xlabel='Frequency (Hz)', ylabel='Band level (dB re full scale)', xlim=(90, 11000))
    ax.set_xticks([125, 250, 500, 1000, 2000, 4000, 8000], ['125', '250', '500', '1k', '2k', '4k', '8k'])
    ax.set_title('Measured band levels', pad=22)
    ax.legend(loc='lower left', bbox_to_anchor=(0, 1.0), ncol=3, fontsize=8, borderaxespad=0.2)
    fig.tight_layout(); fig.savefig(out / 'levels.png'); plt.close(fig)

    if peaks:
        fig, ax = plt.subplots(figsize=(7.2, 2.8))
        red = [o - i - cal for _, o, i in peaks]
        ax.bar(range(1, len(red) + 1), red, color=c1, width=0.6, edgecolor='white', linewidth=2)
        ax.axhline(np.median(red), color=ink2, ls='--', lw=1)
        ax.set(xlabel='Clap', ylabel='Peak reduction (dB)')
        ax.set_title(f'Claps, headphones alone: median {np.median(red):.0f} dB quieter in the ear', pad=10)
        ax.grid(axis='x', visible=False)
        fig.tight_layout(); fig.savefig(out / 'claps.png'); plt.close(fig)


if __name__ == '__main__':
    if '--step' in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import passive_steps
        passive_steps.main(sys.argv[1:])
    else:
        main()
