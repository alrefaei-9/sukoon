"""Overnight battery test (dashboard version: --run DIR, see tools/battery_run.py): every --every seconds, log whether the Pi's supply voltage is OK while Sukoon runs.

The Pi 4 cannot read its 5 V input voltage. What it can report is the firmware's under-voltage flag, set when
the supply drops below about 4.63 V (vcgencmd get_throttled). That flag, the throttling flags, temperature,
clock and Sukoon's own status are written every period, so the last row tells how long the power bank lasted
and the first "LOW VOLTAGE" row tells when it started to sag. Every row is flushed to the SD card at once.

Start it so it keeps running after you disconnect (your user has systemd linger enabled):
  systemd-run --user --unit=sukoon-battery ~/sukoon/.venv/bin/python ~/sukoon/tools/battery_log.py --keep-on
Check on it:     systemctl --user status sukoon-battery      tail -f ~/sukoon/logs/battery_*.log
Stop it:         systemctl --user stop sukoon-battery
Next morning:    ~/sukoon/.venv/bin/python ~/sukoon/tools/battery_log.py --summary      (latest log)

--keep-on turns Sukoon back on through the dashboard if it is found off (only if the system check passed).
Clock note: the Pi has no clock battery, so the wall time may be wrong until it reaches the internet. The
"elapsed" and "uptime" columns are always right.
"""
import argparse, csv, json, os, subprocess, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / 'logs'
DASHBOARD = os.environ.get('SUKOON_DASHBOARD', 'http://127.0.0.1:5000')
FIELDS = ['wall_time', 'elapsed_h', 'uptime_h', 'verdict', 'under_voltage_now', 'under_voltage_since_boot',
          'throttled_now', 'freq_capped_now', 'throttled_raw', 'core_volts', 'temp_c', 'arm_mhz', 'cpu_pct',
          'sukoon', 'frames', 'output_underflows', 'over_budget_frames', 'disk_free_gb', 'note']


def vcgencmd(*args):
    try:
        return subprocess.run(['vcgencmd', *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def dashboard(path, body=None):
    try:
        req = urllib.request.Request(DASHBOARD + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='GET' if body is None else 'POST')
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def cpu_times():
    v = list(map(int, Path('/proc/stat').read_text().split('\n', 1)[0].split()[1:]))
    return sum(v), v[3] + v[4]


def sample(start, cpu0):
    raw = vcgencmd('get_throttled')
    try:
        bits = int(raw.split('=')[1], 16)
    except (IndexError, ValueError):
        bits = None
    flag = lambda m: '' if bits is None else int(bool(bits & m))
    if bits is None:
        verdict = 'UNKNOWN'
    elif bits & 0x1:
        verdict = 'LOW VOLTAGE NOW'
    elif bits & 0x10000:
        verdict = 'OK now, was low earlier'
    else:
        verdict = 'OK'
    total, idle = cpu_times()
    cpu = 100.0 * (1 - (idle - cpu0[1]) / max(total - cpu0[0], 1))
    try:
        temp = round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000.0, 1)
    except (OSError, ValueError):
        temp = ''
    try:
        mhz = round(int(vcgencmd('measure_clock', 'arm').split('=')[1]) / 1e6)
    except (IndexError, ValueError):
        mhz = ''
    volts = vcgencmd('measure_volts', 'core').replace('volt=', '').rstrip('V')
    metrics = dashboard('/metrics') or {}
    packet = metrics.get('packet') or {}
    live = packet.get('live') or {}
    power = (metrics.get('control') or {}).get('power', 'dashboard not answering' if not metrics else '')
    st = os.statvfs(str(ROOT))
    row = {'wall_time': time.strftime('%Y-%m-%d %H:%M:%S'), 'elapsed_h': round((time.monotonic() - start) / 3600, 3),
           'uptime_h': round(float(Path('/proc/uptime').read_text().split()[0]) / 3600, 3), 'verdict': verdict,
           'under_voltage_now': flag(0x1), 'under_voltage_since_boot': flag(0x10000), 'throttled_now': flag(0x4),
           'freq_capped_now': flag(0x2), 'throttled_raw': raw.replace('throttled=', ''), 'core_volts': volts,
           'temp_c': temp, 'arm_mhz': mhz, 'cpu_pct': round(cpu, 1), 'sukoon': power,
           'frames': live.get('frames', ''), 'output_underflows': live.get('output_underflows', ''),
           'over_budget_frames': live.get('over_budget_frames', ''),
           'disk_free_gb': round(st.f_bavail * st.f_frsize / 1e9, 2), 'note': ''}
    return row, (total, idle), metrics


def run(args):
    LOGS.mkdir(exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    csv_path, log_path = LOGS / f'battery_{stamp}.csv', LOGS / f'battery_{stamp}.log'
    start, cpu0 = time.monotonic(), cpu_times()
    last_verdict, last_frames, stalled = None, None, 0
    with open(csv_path, 'w', newline='') as fc, open(log_path, 'w') as fl:
        wr = csv.DictWriter(fc, fieldnames=FIELDS)
        wr.writeheader()

        def say(text):
            line = f'{time.strftime("%H:%M:%S")}  +{(time.monotonic() - start) / 3600:5.2f} h  {text}'
            print(line, flush=True)
            fl.write(line + '\n'); fl.flush(); os.fsync(fl.fileno())
        say(f'Battery log started, a row every {args.every:.0f} s -> {csv_path}')
        while True:
            row, cpu0, metrics = sample(start, cpu0)
            notes = []
            if args.keep_on and metrics and row['sukoon'] == 'off':
                ready = (metrics.get('control') or {}).get('ready')
                if ready and dashboard('/power', {'on': True}):
                    notes.append('Sukoon was off: turned it back on')
                else:
                    notes.append('Sukoon is off and the system check has not passed: not restarted')
            frames = row['frames']
            if row['sukoon'] == 'on' and frames != '' and frames == last_frames:
                stalled += 1
                if stalled == 2:
                    notes.append('Sukoon is on but its audio frames stopped advancing')
            else:
                stalled = 0
            last_frames = frames
            row['note'] = '; '.join(notes)
            wr.writerow(row); fc.flush(); os.fsync(fc.fileno())
            if row['verdict'] != last_verdict or notes or args.verbose:
                say(f"{row['verdict']:<24} Sukoon {row['sukoon']}, {row['temp_c']} °C, {row['arm_mhz']} MHz, "
                    f"underflows {row['output_underflows']}" + (f"  | {row['note']}" if row['note'] else ''))
                last_verdict = row['verdict']
            elif int(row['elapsed_h'] * 3600) % 3600 < args.every:
                say(f"hourly: {row['verdict']}, Sukoon {row['sukoon']}, {row['temp_c']} °C")
            time.sleep(args.every)


def summary(path):
    path = Path(path) if path else max(LOGS.glob('battery_*.csv'), key=lambda p: p.stat().st_mtime, default=None)
    if path is None:
        sys.exit('No battery log found in ' + str(LOGS))
    rows = list(csv.DictReader(open(path)))
    if not rows:
        sys.exit(f'{path} is empty')
    last = rows[-1]
    low = next((r for r in rows if r['under_voltage_now'] == '1'), None)
    on = [r for r in rows if r['sukoon'] == 'on']
    print(f'{path.name}: {len(rows)} rows, from {rows[0]["wall_time"]} (Pi clock)')
    print(f'Logged for {float(last["elapsed_h"]):.2f} h; last row: {last["verdict"]}, Sukoon {last["sukoon"]}')
    print(f'Sukoon on in {len(on)} of {len(rows)} rows ({100 * len(on) / len(rows):.0f}%)')
    print(f'First low voltage at +{float(low["elapsed_h"]):.2f} h' if low else 'Low voltage never reported')
    temps = [float(r['temp_c']) for r in rows if r['temp_c']]
    if temps:
        print(f'Temperature {min(temps):.1f} to {max(temps):.1f} °C')
    under = [int(r['output_underflows']) for r in on if r['output_underflows']]
    if under:
        print(f'Audio underflows at the end of the run: {under[-1]}')
    notes = [(r['elapsed_h'], r['note']) for r in rows if r['note']]
    for h, n in notes[:10]:
        print(f'  +{float(h):.2f} h  {n}')
    stopped_early = float(last['elapsed_h']) and last['verdict'] != 'LOW VOLTAGE NOW'
    print('If the Pi died, the last row is roughly when the power bank ran out.' if stopped_early else '')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('--run', '--finalize', '--graphs'):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import battery_run                       # dashboard battery test (logs/battery_runs/<stamp>/)
        battery_run.main(sys.argv[1:])
        sys.exit(0)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--every', type=float, default=60.0, help='seconds between rows (default 60)')
    p.add_argument('--keep-on', action='store_true', help='turn Sukoon back on if it is found off')
    p.add_argument('--verbose', action='store_true', help='print every row, not just changes')
    p.add_argument('--summary', nargs='?', const='', help='summarize a log (default: the latest) and exit')
    a = p.parse_args()
    if a.summary is not None:
        summary(a.summary)
    else:
        run(a)
