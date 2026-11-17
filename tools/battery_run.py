"""Dashboard battery test: logs until the power bank dies, the user stops it, or the optional max duration ends.

Started by the dashboard as a systemd user service (survives SSH logout, the dashboard restarting and the LAN cable
being unplugged; linger is on for the service user):
  systemd-run --user --unit=sukoon-battery ... python tools/battery_log.py --run logs/battery_runs/<stamp>
Needs nothing but localhost: the dashboard's /metrics (127.0.0.1) and sukoon_engine.py's audio tap (/dev/shm). If the
dashboard is down, rows still log the Pi's own numbers.

Files in the run folder (every row is flushed and fsynced; state.json is rewritten at every row):
  form.json           what was entered on the start form, plus the phone clock offset
  battery.csv         one row every 10 s
  attenuation.csv     every 3 minutes: average attenuation % and dB, per 1/3-octave band
  bangs.csv           one row per bang trigger (and the clip saved for it)
  events.csv          Stimulus started taps, clip saving switched off, Sukoon turned back on, ...
  clips/              about 1 s around each bang: stereo WAV, left = microphone, right = earbud output; index.csv
  state.json          running / stopped / interrupted / finished, last row, counters
  summary.json/.txt, graphs/*.png, README.txt, battery_<stamp>.zip   made at the end (or after an interruption)
  --finalize DIR      write summary, graphs, README and zip for a run (used after a power loss)
"""
import csv, json, os, queue, signal, subprocess, sys, threading, time, wave, zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'dashboard'))
import battery_log as bl
import tap as taplib

ROOT = Path(__file__).resolve().parent.parent
EVERY_S = 10.0
CLIP_PRE_FRAMES, CLIP_POST_FRAMES = 40, 60          # 0.4 s before the trigger, 0.6 s after
MAX_CLIPS, MAX_CLIP_BYTES = 200, 50 * 10 ** 6
MIN_FREE_FOR_CLIPS = 10 ** 9                         # stop saving clips below 1 GB free; the log continues
FIELDS = ['elapsed_s', 'monotonic_s', 'wall_time_pi', 'phone_time', 'sukoon', 'test_version', 'throttled_raw',
          'under_voltage_now', 'under_voltage_since_boot', 'freq_capped_now', 'freq_capped_since_boot',
          'throttled_now', 'throttled_since_boot', 'temp_limit_now', 'temp_limit_since_boot', 'arm_mhz', 'temp_c',
          'cpu0_pct', 'cpu1_pct', 'cpu2_pct', 'cpu3_pct', 'cpu_pct', 'mem_available_mb', 'disk_free_gb',
          'processing_ms_mean_10s', 'processing_ms_max_10s', 'over_budget_frames', 'over_budget_delta',
          'output_underflows', 'underflows_delta', 'input_clipped_samples', 'output_sample_peak_dbfs', 'bang_count',
          'speech_fraction', 'in_level_dbfs', 'out_level_dbfs', 'mic_level_pct', 'volume_target_dbfs', 'sound_card',
          'hotspot_devices', 'clips_saved', 'clips_skipped', 'tap_frames_lost', 'note']
BANG_FIELDS = ['elapsed_s', 'wall_time_pi', 'phone_time', 'peak_dbfs', 'out_peak_dbfs', 'reduction_db',
               'speech_gate_open', 'rnnoise_voice_prob', 'pitch_heard', 'test_version', 'clip_file'] + \
              [f'reduction_{f}hz_db' for f in taplib.BANDS]
stop = []


def fsync_write(fh):
    fh.flush()
    os.fsync(fh.fileno())


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=1)
        fsync_write(f)
    tmp.replace(path)


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return ''


def per_core():
    out = {}
    for line in Path('/proc/stat').read_text().splitlines():
        if line.startswith('cpu') and line[3:4].isdigit():
            v = list(map(int, line.split()[1:]))
            out[line.split()[0]] = (sum(v), v[3] + v[4])
    return out


def hotspot_devices():
    try:
        out = subprocess.run(['iw', 'dev', 'wlan0', 'station', 'dump'], capture_output=True, text=True, timeout=2).stdout
        return out.count('Station ')
    except (OSError, subprocess.SubprocessError):
        return ''


def flags(raw):
    try:
        bits = int(raw, 16)
    except (TypeError, ValueError):
        return {k: '' for k in ('under_voltage_now', 'under_voltage_since_boot', 'freq_capped_now',
                                'freq_capped_since_boot', 'throttled_now', 'throttled_since_boot',
                                'temp_limit_now', 'temp_limit_since_boot')}
    f = lambda m: int(bool(bits & m))
    return {'under_voltage_now': f(0x1), 'freq_capped_now': f(0x2), 'throttled_now': f(0x4), 'temp_limit_now': f(0x8),
            'under_voltage_since_boot': f(0x10000), 'freq_capped_since_boot': f(0x20000),
            'throttled_since_boot': f(0x40000), 'temp_limit_since_boot': f(0x80000)}


class ClipWriter(threading.Thread):
    """Writes clips from a queue at the lowest priority, never on the audio path (sukoon_engine.py only fills the tap)."""

    def __init__(self, folder):
        super().__init__(daemon=True)
        self.folder, self.q = folder, queue.Queue()
        self.index = open(folder / 'index.csv', 'a', newline='')
        self.wr = csv.writer(self.index)
        if self.index.tell() == 0:
            self.wr.writerow(['clip_file', 'elapsed_s', 'wall_time_pi', 'peak_dbfs', 'reduction_db', 'speech_gate_open'])
        self.written_at = []                              # monotonic times of finished writes

    def run(self):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except OSError:
            pass
        while True:
            item = self.q.get()
            if item is None:
                return
            name, inp, out, row = item
            stereo = np.stack([inp, out], axis=1)
            pcm = np.round(np.clip(stereo, -1.0, 32767.0 / 32768.0) * 32768.0).astype('<i2')
            with wave.open(str(self.folder / name), 'wb') as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(taplib.FS)
                w.writeframes(pcm.tobytes())
            self.wr.writerow([name, row['elapsed_s'], row['wall_time_pi'], row['peak_dbfs'], row['reduction_db'],
                              row['speech_gate_open']])
            fsync_write(self.index)
            self.written_at.append(time.monotonic())


class TapWatcher(threading.Thread):
    """Reads the audio tap twice a second: window stats for the rows, bang triggers, and clips around them."""

    def __init__(self, run, tap_path):
        super().__init__(daemon=True)
        self.run_ = run
        self.reader = taplib.TapReader(tap_path)
        self.lock = threading.Lock()
        self.reset_window()
        self.run_peak = 0.0
        self.keep_meta, self.keep_in, self.keep_out = [], [], []
        self.pending = []
        self.last_triggers = None

    def reset_window(self):
        self.win = {'peak': 0.0, 'in_pow': 0.0, 'out_pow': 0.0, 'frames': 0, 'voice': 0}

    def take_window(self):
        with self.lock:
            w = self.win
            self.reset_window()
        return w

    def run(self):
        self.reader.poll(start_at_now=True)
        while not stop:
            time.sleep(0.5)
            try:
                self.step()
            except Exception as error:                     # never let the logger die over one bad read
                self.run_.event(f'tap reader error: {error}')
                time.sleep(2)

    def step(self):
        meta, inp, out = self.reader.poll()
        if not len(meta):
            return
        with self.lock:
            peak = float(np.max(np.abs(out)))
            self.win['peak'] = max(self.win['peak'], peak)
            self.run_peak = max(self.run_peak, peak)
            self.win['in_pow'] += float(np.mean(inp.astype(np.float64) ** 2)) * len(meta)
            self.win['out_pow'] += float(np.mean(out.astype(np.float64) ** 2)) * len(meta)
            self.win['frames'] += len(meta)
            self.win['voice'] += int(np.count_nonzero(meta['flags'] & taplib.FLAG_VOICE))
        self.keep_meta.append(meta); self.keep_in.append(inp); self.keep_out.append(out)
        for m in meta:
            t = int(m['triggers'])
            if self.last_triggers is not None and t > self.last_triggers:
                self.pending.append(int(m['frame']))
            self.last_triggers = t
        m_all = np.concatenate(self.keep_meta)
        i_all, o_all = np.concatenate(self.keep_in), np.concatenate(self.keep_out)
        still = []
        for fnum in self.pending:
            k = int(np.searchsorted(m_all['frame'], fnum))
            if k >= len(m_all) or m_all['frame'][k] != fnum:
                continue                                    # lost frames: skip this one
            if k + CLIP_POST_FRAMES <= len(m_all):
                self.run_.bang(m_all, i_all, o_all, k)
            else:
                still.append(fnum)
        self.pending = still
        total = len(m_all)
        while total - len(self.keep_meta[0]) >= CLIP_PRE_FRAMES + CLIP_POST_FRAMES + 60:
            total -= len(self.keep_meta[0])
            self.keep_meta.pop(0); self.keep_in.pop(0); self.keep_out.pop(0)


class Run:
    def __init__(self, folder, metrics_url, tap_path):
        self.dir = folder
        self.url = metrics_url
        self.form = json.loads((folder / 'form.json').read_text())
        self.offset = float(self.form.get('phone_offset_s') or 0.0)
        self.clips_on = bool(self.form.get('save_clips', True))
        self.clips_off_reason = None
        self.clip_dir = folder / 'clips'
        self.clips_saved = self.clips_skipped = 0
        self.clip_bytes = 0
        self.bangs = 0
        self.lock = threading.Lock()
        self.start_mono = time.monotonic()
        self.start_wall = time.time()
        self.events = open(folder / 'events.csv', 'a', newline='')
        self.ev = csv.writer(self.events)
        if self.events.tell() == 0:
            self.ev.writerow(['elapsed_s', 'wall_time_pi', 'phone_time', 'event'])
        self.fb = open(folder / 'bangs.csv', 'w', newline='')
        self.wb = csv.DictWriter(self.fb, fieldnames=BANG_FIELDS)
        self.wb.writeheader(); fsync_write(self.fb)
        self.writer = None
        if self.clips_on:
            self.clip_dir.mkdir(exist_ok=True)
            self.writer = ClipWriter(self.clip_dir)
            self.writer.start()
        self.tap = TapWatcher(self, tap_path)
        self.version = None

    def times(self):
        now = time.time()
        return (round(time.monotonic() - self.start_mono, 2), time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now + self.offset)))

    def event(self, text):
        with self.lock:
            self.ev.writerow([*self.times(), text])
            fsync_write(self.events)

    def bang(self, m_all, i_all, o_all, k):
        rec = taplib.bang_measure(i_all, o_all, k)
        m = m_all[k]
        el, wall, phone = self.times()
        el = round(el - (len(m_all) - 1 - k) / 100.0, 2)     # when the trigger happened, not when it was read
        row = {'elapsed_s': el, 'wall_time_pi': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(m['time']))),
               'phone_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(m['time']) + self.offset)),
               'peak_dbfs': rec['peak_dbfs'], 'out_peak_dbfs': rec['out_peak_dbfs'], 'reduction_db': rec['reduction_db'],
               'speech_gate_open': int(bool(m['flags'] & taplib.FLAG_VOICE)), 'rnnoise_voice_prob': round(float(m['vad']), 3),
               'pitch_heard': int(bool(m['flags'] & taplib.FLAG_PITCH)), 'test_version': int(m['version']), 'clip_file': ''}
        for f, v in zip(taplib.BANDS, rec['band_reduction_db'] or [''] * len(taplib.BANDS)):
            row[f'reduction_{f}hz_db'] = v
        if self.clips_on:
            reason = None
            if self.clips_saved >= MAX_CLIPS:
                reason = f'{MAX_CLIPS} clips saved (cap)'
            elif self.clip_bytes >= MAX_CLIP_BYTES:
                reason = '50 MB of clips saved (cap)'
            elif bl.os.statvfs(str(ROOT)).f_bavail * bl.os.statvfs(str(ROOT)).f_frsize < MIN_FREE_FOR_CLIPS:
                reason = 'less than 1 GB free'
            if reason:
                self.clips_skipped += 1
            else:
                a, b = k - CLIP_PRE_FRAMES, k + CLIP_POST_FRAMES
                if a < 0:
                    self.clips_skipped += 1
                else:
                    name = f'clip_{self.clips_saved + 1:04d}_{el:09.2f}s.wav'
                    self.writer.q.put((name, i_all[a:b].ravel().copy(), o_all[a:b].ravel().copy(), row))
                    self.clips_saved += 1
                    self.clip_bytes += (b - a) * taplib.FRAME * 4 + 44
                    row['clip_file'] = f'clips/{name}'
        elif self.form.get('save_clips', True):
            self.clips_skipped += 1                          # saving was switched off during the run
        self.bangs += 1
        self.wb.writerow(row)
        fsync_write(self.fb)

    def clips_auto_off(self, reason):
        if self.clips_on:
            self.clips_on = False
            self.clips_off_reason = reason
            self.event(f'Clip saving switched off automatically: {reason}')


def run(folder, metrics_url, tap_path):
    r = Run(folder, metrics_url, tap_path)
    signal.signal(signal.SIGTERM, lambda *_: stop.append('stopped from the dashboard'))
    max_s = float(r.form.get('max_hours') or 0) * 3600
    state = {'state': 'running', 'pid': os.getpid(), 'boot_id': boot_id(), 'start_wall': r.start_wall,
             'start_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r.start_wall)),
             'start_mono': r.start_mono, 'rows': 0, 'clips_enabled': r.clips_on}
    write_json(folder / 'state.json', state)
    r.event('Battery test started')
    r.tap.start()
    fc = open(folder / 'battery.csv', 'w', newline='')
    wr = csv.DictWriter(fc, fieldnames=FIELDS)
    wr.writeheader(); fsync_write(fc)
    fa = open(folder / 'attenuation.csv', 'w', newline='')
    wa = csv.writer(fa)
    wa.writerow(['elapsed_s', 'wall_time_pi', 'window_end_pi', 'seconds', 'attenuation_pct', 'attenuation_db', 'in_dbfs',
                 'out_dbfs', 'bangs'] + [f'band_{f}hz_db' for f in taplib.BANDS])
    fsync_write(fa)
    cores0 = per_core()
    first = bl.dashboard('/metrics') or {}
    live0 = ((first.get('packet') or {}).get('live') or {})
    base_under, base_over = live0.get('output_underflows'), live0.get('over_budget_frames')
    last_under, last_over, last_frames, last_window_t = base_under, base_over, None, None
    rows_clip, rows_plain = [], []          # (underflows delta, processing mean) with / without clip writes
    clip_writes_seen = 0
    next_t = time.monotonic()
    while not stop:
        next_t += EVERY_S
        while not stop and time.monotonic() < next_t:
            time.sleep(min(0.5, max(0.0, next_t - time.monotonic())))
        if stop:
            break
        notes = []
        el, wall, phone = r.times()
        raw = bl.vcgencmd('get_throttled').replace('throttled=', '')
        cores1 = per_core()
        loads = {}
        for k in ('cpu0', 'cpu1', 'cpu2', 'cpu3'):
            if k in cores0 and k in cores1:
                dt, di = cores1[k][0] - cores0[k][0], cores1[k][1] - cores0[k][1]
                loads[k] = round(100.0 * (1 - di / max(dt, 1)), 1)
        cores0 = cores1
        try:
            temp = round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000.0, 1)
        except (OSError, ValueError):
            temp = ''
        try:
            mhz = round(int(bl.vcgencmd('measure_clock', 'arm').split('=')[1]) / 1e6)
        except (IndexError, ValueError):
            mhz = ''
        mem = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            mem[key] = int(value.split()[0]) * 1024
        st = os.statvfs(str(ROOT))
        d = bl.dashboard('/metrics')
        pk = (d or {}).get('packet') or {}
        live = pk.get('live') or {}
        ctl = (d or {}).get('control') or {}
        power = ctl.get('power', 'dashboard not answering' if not d else '')
        if d and power == 'off' and not stop:
            ok = bl.dashboard('/power', {'on': True, 'source': 'battery'})
            notes.append('Sukoon was off: turned it back on' if ok and ok.get('ok') else 'Sukoon is off and could not be turned on')
        frames = live.get('frames')
        if power == 'on' and frames is not None and frames == last_frames:
            notes.append('Sukoon is on but its audio frames stopped advancing')
        last_frames = frames
        under, over = live.get('output_underflows'), live.get('over_budget_frames')
        if under is not None and (last_under is None or under < last_under):
            last_under, last_over = under, over                # sukoon_engine.py restarted: counters start again
            base_under = (base_under or 0)
        du = (under - last_under) if under is not None and last_under is not None else ''
        do = (over - last_over) if over is not None and last_over is not None else ''
        if under is not None:
            last_under, last_over = under, over
        w = r.tap.take_window()
        n = max(w['frames'], 1)
        settings = pk.get('settings') or {}
        card = ctl.get('card') or {}
        r.version = (pk.get('state') or {}).get('test_version')
        row = {'elapsed_s': el, 'monotonic_s': round(time.monotonic(), 2), 'wall_time_pi': wall, 'phone_time': phone,
               'sukoon': power, 'test_version': r.version, 'throttled_raw': raw, **flags(raw),
               'arm_mhz': mhz, 'temp_c': temp, **{f'{k}_pct': v for k, v in loads.items()},
               'cpu_pct': round(sum(loads.values()) / max(len(loads), 1), 1) if loads else '',
               'mem_available_mb': round(mem.get('MemAvailable', 0) / 1e6), 'disk_free_gb': round(st.f_bavail * st.f_frsize / 1e9, 2),
               'processing_ms_mean_10s': round(live['processing_ms_mean_10s'], 3) if 'processing_ms_mean_10s' in live else '',
               'processing_ms_max_10s': round(live['processing_ms_max_10s'], 3) if 'processing_ms_max_10s' in live else '',
               'over_budget_frames': over if over is not None else '', 'over_budget_delta': do,
               'output_underflows': under if under is not None else '', 'underflows_delta': du,
               'input_clipped_samples': live.get('input_rail_samples', ''),
               'output_sample_peak_dbfs': taplib.dbfs(w['peak']) if w['frames'] else '',
               'bang_count': r.bangs, 'speech_fraction': round(w['voice'] / n, 3) if w['frames'] else '',
               'in_level_dbfs': taplib.dbfs(w['in_pow'] / n, power=True) if w['frames'] else '',
               'out_level_dbfs': taplib.dbfs(w['out_pow'] / n, power=True) if w['frames'] else '',
               'mic_level_pct': ((d or {}).get('mic') or {}).get('pct', ''), 'volume_target_dbfs': settings.get('TARGET_SPEECH_DBFS', ''),
               'sound_card': f"{card.get('label', '')} {card.get('usbid', '')}".strip(), 'hotspot_devices': hotspot_devices(),
               'clips_saved': r.clips_saved, 'clips_skipped': r.clips_skipped, 'tap_frames_lost': r.tap.reader.lost,
               'note': '; '.join(notes)}
        wr.writerow(row)
        fsync_write(fc)
        for note in notes:
            r.event(note)
        # 3-minute attenuation windows (SpectrumMeter), logged once each
        spec = (d or {}).get('spectrum') or {}
        wins = spec.get('windows') or []
        if wins and wins[-1].get('final') and wins[-1].get('time') != last_window_t and 'band_db' in wins[-1]:
            win = wins[-1]
            last_window_t = win['time']
            wa.writerow([el, wall, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(win['time'])), win['seconds'],
                         win['pct'], win['reduction_db'], win['in_dbfs'], win['out_dbfs'], win['bangs']] + win['band_db'])
            fsync_write(fa)
        # clip writing must not cost the audio anything: compare rows with and without a clip write
        if r.writer is not None and r.clips_on and du != '' and row['processing_ms_mean_10s'] != '':
            writes = len(r.writer.written_at)
            (rows_clip if writes > clip_writes_seen else rows_plain).append((du, row['processing_ms_mean_10s']))
            clip_writes_seen = writes
            if len(rows_clip) >= 6 and len(rows_plain) >= 6:
                uc = float(np.mean([u for u, _ in rows_clip])); up = float(np.mean([u for u, _ in rows_plain]))
                pc = float(np.mean([p for _, p in rows_clip])); pp = float(np.mean([p for _, p in rows_plain]))
                if sum(u for u, _ in rows_clip) >= 3 and uc > 2 * up + 0.2:
                    r.clips_auto_off(f'underflows {uc:.2f} per 10 s with clip writes vs {up:.2f} without')
                elif pc > pp * 1.15 + 0.3:
                    r.clips_auto_off(f'processing {pc:.2f} ms with clip writes vs {pp:.2f} ms without')
        state.update(rows=state['rows'] + 1, last_elapsed_s=el, last_wall=wall, last_phone=phone, last_temp_c=temp,
                     last_throttled=raw, sukoon=power, clips_saved=r.clips_saved, clips_skipped=r.clips_skipped,
                     clips_enabled=r.clips_on, clips_off_reason=r.clips_off_reason, bangs=r.bangs,
                     run_peak_dbfs=taplib.dbfs(r.tap.run_peak) if r.tap.run_peak else None)
        write_json(folder / 'state.json', state)
        if max_s and el >= max_s:
            stop.append('max duration reached')
    reason = stop[0] if stop else 'stopped'
    r.event(f'Battery test ended: {reason}')
    if r.writer is not None:
        r.writer.q.put(None)
        r.writer.join(timeout=30)
    for f in (fc, fa, r.fb, r.events):
        f.close()
    state.update(state='stopped', end_reason=reason, end_wall=time.time())
    write_json(folder / 'state.json', state)
    finalize(folder)


# ----- after the run: summary, graphs, README, zip -----
def read_rows(path):
    try:
        with open(path, newline='') as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def num(rows, key):
    out = []
    for r in rows:
        try:
            out.append(float(r[key]))
        except (KeyError, TypeError, ValueError):
            out.append(np.nan)
    return np.array(out)


def commit():
    try:
        out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        return out + ('+uncommitted' if dirty else '')
    except OSError:
        return 'unknown'


def summarize(folder):
    form = json.loads((folder / 'form.json').read_text())
    state = json.loads((folder / 'state.json').read_text()) if (folder / 'state.json').exists() else {}
    notes = json.loads((folder / 'notes.json').read_text()) if (folder / 'notes.json').exists() else {}
    rows = read_rows(folder / 'battery.csv')
    bangs = read_rows(folder / 'bangs.csv')
    events = read_rows(folder / 'events.csv')
    el = num(rows, 'elapsed_s')
    temp, cpu = num(rows, 'temp_c'), num(rows, 'cpu_pct')
    du, do = num(rows, 'underflows_delta'), num(rows, 'over_budget_delta')
    peaks = num(rows, 'output_sample_peak_dbfs')
    on_rows = sum(1 for r in rows if r.get('sukoon') == 'on')
    throttle_rows = [r for r in rows if any(r.get(k) == '1' for k in ('under_voltage_now', 'freq_capped_now', 'throttled_now', 'temp_limit_now'))]
    events_throttle, prev = 0, False
    for r in rows:
        now = any(r.get(k) == '1' for k in ('under_voltage_now', 'freq_capped_now', 'throttled_now', 'temp_limit_now'))
        events_throttle += int(now and not prev)
        prev = now
    stat = lambda a: [round(float(np.nanmin(a)), 1), round(float(np.nanmean(a)), 1), round(float(np.nanmax(a)), 1)] if np.isfinite(a).any() else None
    duration = float(el[-1]) if len(el) else 0.0
    summary = {
        'test': 'battery', 'run': folder.name, 'state': state.get('state'),
        'interrupted': state.get('state') == 'interrupted',
        'start_time': state.get('start_time'), 'start_time_phone': form.get('start_time_phone'),
        'end_time': rows[-1]['wall_time_pi'] if rows else None, 'end_time_phone': rows[-1]['phone_time'] if rows else None,
        'end_reason': state.get('end_reason'),
        'end_note': 'For an interrupted run the end is the last row written (logged every 10 s): when the power bank died.'
        if state.get('state') == 'interrupted' else None,
        'duration_s': round(duration, 1), 'duration_h': round(duration / 3600, 3), 'rows': len(rows),
        'sukoon_on_h': round(on_rows * EVERY_S / 3600, 3),
        'throttling_rows': len(throttle_rows), 'throttling_events': events_throttle,
        'under_voltage_ever': any(r.get('under_voltage_now') == '1' for r in rows),
        'temp_c_min_mean_max': stat(temp), 'cpu_pct_mean': round(float(np.nanmean(cpu)), 1) if np.isfinite(cpu).any() else None,
        'underflows_total': int(np.nansum(du)), 'over_budget_total': int(np.nansum(do)),
        'bang_count': len(bangs), 'clips_saved': state.get('clips_saved', 0), 'clips_skipped': state.get('clips_skipped', 0),
        'clips_enabled_at_start': bool(form.get('save_clips', True)), 'clips_off_reason': state.get('clips_off_reason'),
        'stimulus_started_taps': sum(1 for e in events if e.get('event', '').startswith('Stimulus started')),
        'output_sample_peak_max_dbfs': round(float(np.nanmax(peaks)), 1) if np.isfinite(peaks).any() else None,
        'limiter_ceiling_dbfs': -9.0, 'commit': commit(), 'form': form, 'notes_after_run': notes,
    }
    write_json(folder / 'summary.json', summary)
    lines = [f'Sukoon battery test {folder.name}', '']
    for k, v in summary.items():
        if k not in ('form', 'notes_after_run'):
            lines.append(f'{k}: {v}')
    lines += ['', 'start form:'] + [f'  {k}: {v}' for k, v in form.items()]
    lines += ['notes after the run:'] + [f'  {k}: {v}' for k, v in notes.items()]
    (folder / 'summary.txt').write_text('\n'.join(lines) + '\n')
    return summary


def graphs(folder):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    rows = read_rows(folder / 'battery.csv')
    if len(rows) < 2:
        return
    out = folder / 'graphs'
    out.mkdir(exist_ok=True)
    h = num(rows, 'elapsed_s') / 3600
    throttled = np.array([any(r.get(k) == '1' for k in ('under_voltage_now', 'freq_capped_now', 'throttled_now', 'temp_limit_now')) for r in rows])
    att = read_rows(folder / 'attenuation.csv')
    plots = [('temperature', 'Temperature (°C)', [(num(rows, 'temp_c'), 'CPU temperature')]),
             ('cpu_clock', 'ARM clock (MHz)', [(num(rows, 'arm_mhz'), 'ARM clock')]),
             ('processing_time', 'Processing time per 10 ms frame (ms)', [(num(rows, 'processing_ms_mean_10s'), 'mean of 10 s'),
                                                                        (num(rows, 'processing_ms_max_10s'), 'max of 10 s')]),
             ('underflows', 'Underflows (clicks), cumulative', [(np.nancumsum(num(rows, 'underflows_delta')), 'underflows')])]
    for name, ylabel, series in plots:
        fig, ax = plt.subplots(figsize=(7.2, 3.0))
        for (y, label), color in zip(series, ('#1659c7', '#c9650c')):
            ax.plot(h, y, color=color, lw=1.4, label=label)
        mark(ax, h, throttled)
        ax.set(xlabel='Hours since start', ylabel=ylabel)
        ax.grid(alpha=.3)
        ax.legend(loc='best', fontsize=8, frameon=False)
        ax.set_title(ylabel.split(' (')[0] + ' over the battery run', loc='left', fontsize=10)
        fig.tight_layout(); fig.savefig(out / f'{name}.png', dpi=150); plt.close(fig)
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    if att:
        ax.plot(num(att, 'elapsed_s') / 3600, num(att, 'attenuation_pct'), 'o-', color='#1659c7', ms=3, lw=1.2, label='3-minute average')
    mark(ax, h, throttled)
    ax.set(xlabel='Hours since start', ylabel='Average attenuation (%)')
    ax.grid(alpha=.3)
    ax.set_title('Average attenuation over the battery run' + ('' if att else ' (no 3-minute windows yet)'), loc='left', fontsize=10)
    fig.tight_layout(); fig.savefig(out / 'attenuation.png', dpi=150); plt.close(fig)


def mark(ax, h, throttled):
    """Throttling or under-voltage rows as red bands."""
    if throttled.any():
        for i in np.flatnonzero(throttled):
            ax.axvspan(h[max(i - 1, 0)], h[i], color='#c93a32', alpha=.18, lw=0)
        ax.plot([], [], color='#c93a32', alpha=.4, lw=6, label='throttled or low voltage')


def readme(folder):
    (folder / 'README.txt').write_text(f"""Sukoon battery test {folder.name}

summary.txt / summary.json  start and end (Pi clock and phone clock), duration, Sukoon on time, throttling,
                            temperature, CPU, underflows, over-budget frames, bangs, clips, highest output peak,
                            everything entered on the start form and after the run, commit.
battery.csv     one row every 10 s, each flushed to the SD card at once. elapsed_s and monotonic_s are exact;
                wall_time_pi is the Pi clock (no RTC, no NTP on the hotspot: it can be wrong); phone_time is the
                Pi clock plus the offset to the phone's clock taken when the test started.
                throttled_raw is vcgencmd get_throttled; the *_now and *_since_boot columns decode it.
                processing_ms_*_10s: time to process one 10 ms frame, mean and max over the last 10 s block.
                output_sample_peak_dbfs: largest output sample in the row's 10 s. speech_fraction: share of
                frames with the speech path open. in/out_level_dbfs: mean level over the 10 s.
attenuation.csv every 3 minutes: average attenuation (mic vs earbud signal), overall and per 1/3-octave band.
bangs.csv       one row per bang trigger: time, peak, reduction, per-band reduction over 80 ms, speech gate,
                RNNoise voice probability, pitch heard, test version, and the clip saved for it.
events.csv      Stimulus started taps (to line up with known bang times in the stimulus file), Sukoon turned
                back on, clip saving switched off, start and end.
clips/          about 1 s around each bang (0.4 s before, 0.6 s after): stereo WAV, left = microphone,
                right = earbud output (same time axis; the output carries the 20 ms processing delay).
                clips/index.csv lists them.
graphs/         temperature, ARM clock, processing time, underflows and attenuation over time; red bands mark
                rows with throttling or low voltage. They are drawn only while Sukoon is off (drawing them costs
                the audio a few clicks); if missing, download again after turning Sukoon off, or run
                tools/battery_log.py --graphs <this folder> on a PC.
If the run says interrupted, the Pi lost power or the logger was killed: the end time is the last row written.
""")


def finalize(folder):
    """Summary, graphs (offline, lowest priority), README and the zip."""
    st = json.loads((folder / 'state.json').read_text())
    if st.get('state') == 'stopped':
        st['state'] = 'finished'
        write_json(folder / 'state.json', st)
    summarize(folder)
    # Drawing the graphs (matplotlib, a few seconds) measurably adds underflows, so only while Sukoon is off.
    # Otherwise the dashboard draws them the next time Sukoon is off and rebuilds the zip.
    metrics = bl.dashboard('/metrics') or {}
    audio_on = metrics.get('status') in ('Running', 'Bypassed', 'Starting')
    if not audio_on:
        try:
            subprocess.run(['nice', '-n', '19', 'taskset', '-c', '1', sys.executable, str(Path(__file__).resolve().parent / 'battery_log.py'),
                            '--graphs', str(folder)], timeout=600, env={**os.environ, 'OPENBLAS_NUM_THREADS': '1'})
        except (OSError, subprocess.SubprocessError):
            pass
    readme(folder)
    make_zip(folder)
    st = json.loads((folder / 'state.json').read_text())
    st['graphs_pending'] = audio_on and not (folder / 'graphs' / 'temperature.png').exists()
    st['finalized'] = time.time()
    write_json(folder / 'state.json', st)


def make_zip(folder):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timed_test import gentle_zip
    files = [f for f in sorted(folder.rglob('*')) if f.is_file() and f.suffix in ('.csv', '.json', '.txt', '.png', '.wav')
             and f.name not in ('state.json',) and not f.name.endswith('.tmp')]
    zpath = folder / f'battery_{folder.name}.zip'
    tmp = zpath.with_suffix('.ztmp')
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as z:
        for f in files:
            with open(f, 'rb') as src, z.open(zipfile.ZipInfo.from_file(f, f'battery_{folder.name}/{f.relative_to(folder)}'), 'w') as dst:
                while True:
                    block = src.read(256 * 1024)
                    if not block:
                        break
                    dst.write(block)
                    time.sleep(0.02)
    tmp.replace(zpath)


def main(argv):
    mode, folder = argv[0], Path(argv[1]).resolve()
    if mode == '--run':
        url = argv[argv.index('--metrics') + 1] if '--metrics' in argv else 'http://127.0.0.1:5000'
        tap_path = argv[argv.index('--tap') + 1] if '--tap' in argv else '/dev/shm/sukoon_tap'
        bl.DASHBOARD = url
        run(folder, url, tap_path)
    elif mode == '--finalize':
        finalize(folder)
    elif mode == '--graphs':
        graphs(folder)
