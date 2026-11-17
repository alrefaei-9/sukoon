"""Timed test (1 to 5 minutes) while Sukoon runs: records input and output, logs measurements, builds one zip.

Started by the dashboard (POST /test/start), which runs it under nice. It never touches the sound card: audio comes
from sukoon_engine.py's shared-memory tap (tools/tap.py), measurements from the dashboard's /metrics on localhost.

Writes to OUT (logs/tests/<stamp>/):
  test_<stamp>_input.wav, test_<stamp>_output.wav   mono 48 kHz PCM16 on the same time axis
  bands.csv     about 1 Hz: input and output level per 1/3-octave band (SpectrumMeter, last 0.5 s) and in - out
  system.csv    about 1 Hz: levels, output sample peak, CPU, temperature, processing time, underflows, clips, ...
  bangs.csv     every bang trigger: time, peak, reduction, per-band reduction, speech gate, voice probability, pitch
  summary.json, summary.txt, README.txt, status.json (progress for the page)
  test_<stamp>.zip   all of the above
SIGTERM stops early and still finishes the files and the zip.
  .venv/bin/python tools/timed_test.py --minutes 2 --out logs/tests/X --metrics http://127.0.0.1:5000 --tap /dev/shm/sukoon_tap
"""
import argparse, csv, json, os, signal, subprocess, sys, time, urllib.request, wave, zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tap as taplib

ROOT = Path(__file__).resolve().parent.parent
stop = []


def get(url, timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def vcgencmd(*args):
    try:
        return subprocess.run(['vcgencmd', *args], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def commit():
    try:
        out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return out + ('+uncommitted' if dirty else '')
    except OSError:
        return 'unknown'


def write_status(out, **status):
    tmp = out / 'status.json.tmp'
    tmp.write_text(json.dumps(status))
    tmp.replace(out / 'status.json')


def pcm16(x):
    return np.round(np.clip(x, -1.0, 32767.0 / 32768.0) * 32768.0).astype('<i2').tobytes()


def gentle_zip(zpath, files, folder, chunk=256 * 1024, pause=0.02):
    """Zip without compressing (WAVs barely shrink) and in small chunks with pauses, so building it while
    Sukoon runs doesn't burst the SD card (its interrupts share core 0 with the USB audio)."""
    tmp = zpath.with_suffix('.tmp')
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as z:
        for f in files:
            with open(f, 'rb') as src, z.open(zipfile.ZipInfo.from_file(f, f'{folder}/{f.name}'), 'w') as dst:
                while True:
                    block = src.read(chunk)
                    if not block:
                        break
                    dst.write(block)
                    time.sleep(pause)
    tmp.replace(zpath)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--minutes', type=int, choices=range(1, 6), required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--metrics', default='http://127.0.0.1:5000')
    p.add_argument('--tap', default='/dev/shm/sukoon_tap')
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = out.name
    signal.signal(signal.SIGTERM, lambda *_: stop.append('stopped'))
    total_frames = a.minutes * 60 * 100
    started_wall, started_mono = time.time(), time.monotonic()
    write_status(out, state='starting', total_s=a.minutes * 60, elapsed_s=0, started=started_wall)

    first = get(a.metrics + '/metrics')
    if not first or not first.get('packet'):
        write_status(out, state='error', error='Sukoon is not running.', total_s=a.minutes * 60, elapsed_s=0)
        sys.exit(1)
    reader = taplib.TapReader(a.tap)
    reader.poll(start_at_now=True)
    if reader.mm is None or not reader.alive():
        write_status(out, state='error', error='No audio tap: restart Sukoon once (Off, then On) after the update.',
                     total_s=a.minutes * 60, elapsed_s=0)
        sys.exit(1)

    wav_in = wave.open(str(out / f'test_{stamp}_input.wav'), 'wb')
    wav_out = wave.open(str(out / f'test_{stamp}_output.wav'), 'wb')
    for w in (wav_in, wav_out):
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(taplib.FS)
    bands = first['spectrum']['bands'] if first.get('spectrum') else taplib.BANDS
    fb = open(out / 'bands.csv', 'w', newline='')
    wb = csv.writer(fb)
    wb.writerow(['elapsed_s', 'wall_time'] + [f'in_{f}hz_dbfs' for f in bands] + [f'out_{f}hz_dbfs' for f in bands]
                + [f'reduction_{f}hz_db' for f in bands])
    fs_ = open(out / 'system.csv', 'w', newline='')
    sys_fields = ['elapsed_s', 'wall_time', 'in_rms_dbfs', 'in_peak_dbfs', 'out_rms_dbfs', 'out_sample_peak_dbfs',
                  'cpu_pct', 'temp_c', 'arm_mhz', 'processing_ms_mean_1s', 'processing_ms_max_run', 'over_budget_frames',
                  'output_underflows', 'input_overflows', 'input_clipped_samples', 'output_clipped_samples',
                  'mic_level_pct', 'throttled_raw', 'throttled_now', 'under_voltage_now', 'test_version', 'speech']
    ws = csv.DictWriter(fs_, fieldnames=sys_fields)
    ws.writeheader()
    fbg = open(out / 'bangs.csv', 'w', newline='')
    bang_fields = ['elapsed_s', 'wall_time', 'peak_dbfs', 'out_peak_dbfs', 'reduction_db', 'speech_gate_open',
                   'rnnoise_voice_prob', 'pitch_heard', 'test_version'] + [f'reduction_{f}hz_db' for f in bands]
    wbg = csv.DictWriter(fbg, fieldnames=bang_fields)
    wbg.writeheader()

    keep_in, keep_out, keep_meta = [], [], []          # last ~1 s, for bangs near the end of a poll
    frames = 0
    last_triggers = None
    pending = []                                       # bang frame numbers waiting for 7 more frames
    peak_window, run_peak = 0.0, 0.0
    bang_count = 0
    last_second = -1
    under0 = (first['packet']['live'] or {}).get('output_underflows', 0)
    over0 = (first['packet']['live'] or {}).get('over_budget_frames', 0)
    proc_means, proc_max = [], 0.0
    temps, cpus = [], []
    error = None
    last_metrics = first
    first_frame = None
    while frames < total_frames and not stop:
        time.sleep(0.5)                                # larger, fewer writes; the ring holds 10 s
        meta, inp, outp = reader.poll()
        if len(meta) == 0:
            if not reader.alive():
                error = 'Sukoon stopped during the test.'
                break
            continue
        if first_frame is None:
            first_frame = int(meta['frame'][0])
        take = min(len(meta), total_frames - frames)
        meta, inp, outp = meta[:take], inp[:take], outp[:take]
        wav_in.writeframes(pcm16(inp.ravel()))
        wav_out.writeframes(pcm16(outp.ravel()))
        peak = float(np.max(np.abs(outp)))
        peak_window, run_peak = max(peak_window, peak), max(run_peak, peak)
        keep_meta.append(meta); keep_in.append(inp); keep_out.append(outp)
        m_all = np.concatenate(keep_meta); i_all = np.concatenate(keep_in); o_all = np.concatenate(keep_out)
        for j, m in enumerate(meta):
            t = int(m['triggers'])
            if last_triggers is not None and t > last_triggers:
                pending.append(int(m['frame']))
            last_triggers = t
        still = []
        for fnum in pending:
            k = int(np.searchsorted(m_all['frame'], fnum))
            if k + 7 < len(m_all) or frames + take >= total_frames:
                rec = taplib.bang_measure(i_all, o_all, k)
                m = m_all[k]
                row = {'elapsed_s': round((fnum - first_frame) / 100.0, 2),     # position in the WAV files
                       'wall_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(m['time']))),
                       'peak_dbfs': rec['peak_dbfs'], 'out_peak_dbfs': rec['out_peak_dbfs'],
                       'reduction_db': rec['reduction_db'], 'speech_gate_open': int(bool(m['flags'] & taplib.FLAG_VOICE)),
                       'rnnoise_voice_prob': round(float(m['vad']), 3), 'pitch_heard': int(bool(m['flags'] & taplib.FLAG_PITCH)),
                       'test_version': int(m['version'])}
                for f, v in zip(bands, rec['band_reduction_db'] or [''] * len(bands)):
                    row[f'reduction_{f}hz_db'] = v
                wbg.writerow(row); fbg.flush()
                bang_count += 1
            else:
                still.append(fnum)
        pending = still
        n_keep = sum(len(x) for x in keep_meta)
        while n_keep - len(keep_meta[0]) >= 100:        # keep about 1 s
            n_keep -= len(keep_meta[0]); keep_meta.pop(0); keep_in.pop(0); keep_out.pop(0)
        frames += take
        second = frames // 100
        if second != last_second:
            last_second = second
            d = get(a.metrics + '/metrics') or last_metrics
            last_metrics = d
            pk = d.get('packet') or {}
            live, window, sysm = pk.get('live') or {}, pk.get('window') or {}, d.get('system') or {}
            elapsed = round(frames / 100.0, 2)
            wall = time.strftime('%Y-%m-%d %H:%M:%S')
            spec = (d.get('spectrum') or {}).get('live') or {}
            if spec.get('in_db'):
                wb.writerow([elapsed, wall] + spec['in_db'] + spec['out_db']
                            + [round(i - o, 1) for i, o in zip(spec['in_db'], spec['out_db'])])
            raw = (sysm.get('power') or {}).get('raw', '')
            try:
                bits = int(raw, 16)
            except ValueError:
                bits = 0
            row = {'elapsed_s': elapsed, 'wall_time': wall, 'in_rms_dbfs': window.get('in_rms_dbfs'),
                   'in_peak_dbfs': window.get('in_peak_dbfs'), 'out_rms_dbfs': window.get('out_rms_dbfs'),
                   'out_sample_peak_dbfs': taplib.dbfs(peak_window), 'cpu_pct': sysm.get('cpu_pct'),
                   'temp_c': sysm.get('temp_c'), 'arm_mhz': sysm.get('clock_mhz'),
                   'processing_ms_mean_1s': round(live.get('processing_ms_mean_1s', 0), 3),
                   'processing_ms_max_run': round(live.get('processing_ms_max', 0), 3),
                   'over_budget_frames': live.get('over_budget_frames'), 'output_underflows': live.get('output_underflows'),
                   'input_overflows': live.get('input_overflows'), 'input_clipped_samples': live.get('input_rail_samples'),
                   'output_clipped_samples': live.get('output_clipped_samples'),
                   'mic_level_pct': (d.get('mic') or {}).get('pct'), 'throttled_raw': raw,
                   'throttled_now': int(bool(bits & 0x6)), 'under_voltage_now': int(bool(bits & 0x1)),
                   'test_version': (pk.get('state') or {}).get('test_version'),
                   'speech': int(bool(pk.get('voice_hold')))}
            ws.writerow(row); fs_.flush(); fb.flush()
            peak_window = 0.0
            if live.get('processing_ms_mean_1s') is not None:
                proc_means.append(live['processing_ms_mean_1s'])
            proc_max = max(proc_max, live.get('processing_ms_max', 0) or 0)
            if sysm.get('temp_c'):
                temps.append(sysm['temp_c'])
            if sysm.get('cpu_pct') is not None:
                cpus.append(sysm['cpu_pct'])
            write_status(out, state='running', total_s=a.minutes * 60, elapsed_s=elapsed, started=started_wall,
                         bangs=bang_count, out_peak_dbfs=taplib.dbfs(run_peak), lost_frames=reader.lost)

    for w in (wav_in, wav_out):
        w.close()
    for f in (fb, fs_, fbg):
        f.close()
    write_status(out, state='finishing', total_s=a.minutes * 60, elapsed_s=round(frames / 100.0, 1),
                 started=started_wall)
    d = last_metrics or {}
    end = get(a.metrics + '/metrics') or d
    pk, ctl = end.get('packet') or d.get('packet') or {}, end.get('control') or {}
    live = pk.get('live') or {}
    lat = ctl.get('latency_result') or {}
    summary = {
        'test': 'timed', 'stamp': stamp, 'start_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_wall)),
        'start_time_note': 'Pi clock (no RTC: may be off if the Pi had no network). Elapsed times are exact.',
        'duration_requested_s': a.minutes * 60, 'duration_recorded_s': round(frames / 100.0, 2),
        'stopped_by': stop[0] if stop else ('error' if error else 'timer'), 'error': error,
        'test_version': (pk.get('state') or {}).get('test_version'), 'commit': commit(),
        'card': ctl.get('card'), 'cards_detected': ctl.get('cards'), 'mic_level_pct': (end.get('mic') or {}).get('pct'),
        'mic_level_db': (end.get('mic') or {}).get('db'),
        'settings': pk.get('settings'), 'switches': pk.get('state'),
        'buffer_setting': {'mode': ctl.get('latency_mode'), 'latency': live.get('latency_setting'),
                           'prime_frames': live.get('prime_frames'),
                           'device_buffers_reported_ms': [live.get('device_input_latency_ms'), live.get('device_output_latency_ms')]},
        'latency_last_measured': ({'mic_to_ear_ms': lat.get('mic_to_ear_ms'), 'range_ms': lat.get('range_ms'),
                                   'buffer_mode': lat.get('mode'),
                                   'measured_at': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(lat['time']))}
                                  if lat.get('ok') and lat.get('time') else None),
        'latency_note': 'Latency cannot be measured during a test (the card is busy); this is the last measurement.',
        'output_sample_peak_max_dbfs': taplib.dbfs(run_peak),
        'limiter_ceiling_dbfs': -9.0,
        'bangs': bang_count,
        'underflows_during_test': (live.get('output_underflows') or 0) - under0,
        'over_budget_frames_during_test': (live.get('over_budget_frames') or 0) - over0,
        'processing_ms_mean': round(float(np.mean(proc_means)), 3) if proc_means else None,
        'processing_ms_max_run': round(proc_max, 3),
        'temp_c_min_mean_max': [min(temps), round(float(np.mean(temps)), 1), max(temps)] if temps else None,
        'cpu_pct_mean': round(float(np.mean(cpus)), 1) if cpus else None,
        'tap_frames_lost': reader.lost,
    }
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    lines = [f'Sukoon timed test {stamp}', '']
    for k, v in summary.items():
        if k not in ('settings', 'switches'):
            lines.append(f'{k}: {v}')
    lines += ['', 'settings:'] + [f'  {k}: {v}' for k, v in (summary['settings'] or {}).items()]
    lines += ['switches:'] + [f'  {k}: {v}' for k, v in (summary['switches'] or {}).items()]
    (out / 'summary.txt').write_text('\n'.join(lines) + '\n')
    (out / 'README.txt').write_text(f"""Sukoon timed test {stamp}

test_{stamp}_input.wav    microphone, mono 48 kHz 16-bit
test_{stamp}_output.wav   what Sukoon played to the earbuds. Same time axis as the input (the processing
                          delay of 20 ms is inside the output, like the live_*_input/output.wav recordings).
bands.csv     about once a second: microphone and earbud level per 1/3-octave band (dBFS, average of the last
              0.5 s) and their difference (positive = quieter in the earbuds).
system.csv    about once a second: levels, output sample peak in that second (largest absolute sample, dBFS),
              CPU, temperature, clock, processing time per 10 ms frame, underflows (clicks), clipped samples,
              mic level, throttling (raw vcgencmd value; throttled_now = frequency capped or throttled now).
bangs.csv     one row per bang trigger: peak input level, output peak, reduction (dB), per-band reduction over
              80 ms, speech gate (1 = speech path open), RNNoise voice probability, pitch heard, test version.
summary.json  settings, test version, commit, card, mic level, duration, start time (Pi clock), highest output
              sample peak, last measured latency with its date and the buffer setting.

dBFS: level relative to the loudest sample the card can take (0 = full scale).
""")
    zpath = out / f'test_{stamp}.zip'
    gentle_zip(zpath, [f for f in sorted(out.iterdir()) if f.suffix in ('.wav', '.csv', '.json', '.txt')
                       and f.name != 'status.json'], f'test_{stamp}')
    write_status(out, state='error' if error else 'done', error=error, total_s=a.minutes * 60,
                 elapsed_s=round(frames / 100.0, 1), started=started_wall, bangs=bang_count,
                 out_peak_dbfs=taplib.dbfs(run_peak), stopped_by=summary['stopped_by'])


if __name__ == '__main__':
    main()
