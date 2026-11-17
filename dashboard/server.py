"""Phone dashboard for the hear-through pipeline (separate process from the audio loop).

Listens for sukoon_engine.py's metrics on UDP 127.0.0.1:5005, serves http://0.0.0.0:5000, streams
live data with Server-Sent Events at 10 Hz (GET /metrics for polling), and forwards
validated settings to sukoon_engine.py on UDP 127.0.0.1:5006. Reads CPU load, temperature, clock
and power status itself.

It also runs the system check (USB sound card present, microphone signal alive, headphones
confirmed by the listener after a test tone), plays the ready chime when all three pass,
and turns the system on (welcome voice, then sukoon_engine.py) and off (SIGTERM to sukoon_engine.py).
Also: a test-version selector (sukoon_engine.py TEST_VERSIONS, Test 5 by default), a per-run level history
the page can scroll back through when the system is off, start at power-up, and the Tests tab tools.
Page choices that must survive a restart (test version, start at power-up) live in ui_state.json.
Run: .venv/bin/python dashboard/server.py
"""
import json
import math
import re
import os
import resource
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

import soundcards

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRESETS = HERE / 'presets.json'
SOUNDS = HERE / 'sounds'
PIPELINE_LOG = HERE / 'pipeline.log'
CARD_SELECT_LOG = Path(os.environ.get('SUKOON_CARD_LOG', ROOT / 'logs' / 'card_select.log'))   # one line per service start
CARD_WAIT_S = 20                # at service start: wait this long at most for the USB cards to finish appearing
CARD_STABLE_S = 3               # ... and until the set of cards has not changed for this long


def boot_id():
    """Short id of this boot (changes at every power-up), so log lines can be grouped by boot."""
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()[:8]
    except OSError:
        return '?'


UI_STATE = Path(os.environ.get('SUKOON_UI_STATE', HERE / 'ui_state.json'))   # env: test instance only
PYTHON = ROOT / '.venv' / 'bin' / 'python'
RT_PRIORITY = 70
START_TIMEOUT_S = 8.0
STOP_TIMEOUT_S = 15.0                 # sukoon_engine.py writes any recording after the stream closes
METRICS_ADDRESS = ('127.0.0.1', int(os.environ.get('SUKOON_METRICS_PORT', 5005)))   # env overrides: a second
SETTINGS_ADDRESS = ('127.0.0.1', int(os.environ.get('SUKOON_SETTINGS_PORT', 5006))) # test instance, not used
TAP_PATH = os.environ.get('SUKOON_TAP_PATH', '/dev/shm/sukoon_tap')   # sukoon_engine.py's frame ring for recorders
TESTS_DIR = ROOT / 'logs' / 'tests'
# Recorders run at nice 19 on core 1: core 0 takes every interrupt (USB audio, SD card), so keep work off it.
RECORDER_CPU = '1'
HISTORY_SHOWN = 10                    # test histories list the newest 10 runs
LOW_STORAGE_BYTES = 2 * 10 ** 9       # warn below 2 GB free (a 5-minute test takes about 60 MB)
HTTP_PORT = int(os.environ.get('SUKOON_HTTP_PORT', 5000))                             # in service
STOPPED_AFTER_S = 1.0
HISTORY_POINTS = 100                  # 10 s of 10 Hz level windows
RUN_HISTORY_POINTS = 18000            # 30 min, kept until the next run starts (review when off)
AUTO_ON_MIC_RETRY_S = 5.0
HANG_AFTER_S = 5.0                   # audio process alive but no new frames for this long: treated as hung
RECOVER_MAX = 3                      # automatic restarts allowed within RECOVER_WINDOW_S; after that it stays off
RECOVER_WINDOW_S = 600.0
LATENCY_LOG = ROOT / 'logs' / 'latency.log'      # one JSON line per latency measurement
TEST_VERSIONS = [
    {'id': 1, 'name': 'Test 1',
     'summary': 'Original speech rule. Any frame RNNoise rates 0.3 or more opens the ambient path for 1 s; '
                'speech boost up to +18 dB.'},
    {'id': 2, 'name': 'Test 2',
     'summary': 'Sustained voice only (40 ms, 14 dB above the room). No boost after 5 s without speech.'},
    {'id': 3, 'name': 'Test 3',
     'summary': 'Test 2 plus alarm guard: sirens and other long loud sounds stay turned down '
                '(Alarm reduction) instead of coming back after the bang mute.'},
    {'id': 4, 'name': 'Test 4',
     'summary': 'Test 3 with a safety floor: alarms are turned down but never below an audible level '
                '(default −26 dBFS), and faint alarms are raised up to +10 dB to reach it.'},
    {'id': 5, 'name': 'Test 5',
     'summary': 'Test 4 plus bangs caught during speech: the whole sound is turned down for just the bang '
                '(default 30 dB), so speech around it stays clear.'},
]
VERSION_IDS = {v['id'] for v in TEST_VERSIONS}
# Device buffers (measured in 60 s runs, FIFO 70): 'high' 2 underflows; 10 ms + 3 pre-filled frames 7 to 11.
LATENCY_MODES = {
    'standard': {'latency': 'high', 'prime': 0, 'name': 'Standard', 'detail': '40 ms buffers. Most stable.'},
    'low': {'latency': '0.01', 'prime': 3, 'name': 'Low', 'detail': '10 ms buffers, 3 frames pre-filled. Measured slower than Standard and clicks more.'},
}
DEFAULT_UI_STATE = {'test_version': 5, 'auto_on': True,   # start at power-up is always on
                    'latency_mode': 'standard', 'mic_levels': {}, 'card_choice': None}
CLIP_WINDOW_S = 10.0                  # clipped samples are also shown for the last 10 s
EVENT_LOG = 500                       # transients log; the stream carries the newest EVENTS_IN_STREAM, GET /events all
EVENTS_IN_STREAM = 100
# Same limits as sukoon_engine.py's DynamicAdaptiveMitigator.SETTING_RANGES (sukoon_engine.py clamps again).
SWITCHES = ('bypass', 'clip_guard', 'pop_guard_enabled', 'recording')
RANGES = {'TARGET_SPEECH_DBFS': (-30.0, -15.0), 'FLOOR_MIN_DB': (6.0, 30.0), 'FLOOR_MAX_DB': (12.0, 40.0),
          'POP_JUMP_DB': (12.0, 30.0), 'POP_HOLD_FRAMES': (15, 60), 'SUSTAIN_DUCK_DB': (6.0, 30.0),
          'SUSTAIN_FADE_S': (0.5, 5.0), 'SUSTAIN_STEADY_FRAMES': (10, 30),
          'ALARM_MIN_OUT_DBFS': (-40.0, -15.0), 'ALARM_MAX_BOOST_DB': (0.0, 20.0),
          'BANG_DUCK_DB': (10.0, 50.0), 'BANG_OVER_SPEECH_DB': (4.0, 20.0),
          'EQ_100': (-12.0, 12.0), 'EQ_400': (-12.0, 12.0), 'EQ_1000': (-12.0, 12.0), 'EQ_2500': (-12.0, 12.0),
          'EQ_5000': (-12.0, 12.0)}
PRESET_KEYS = ('clip_guard', 'pop_guard_enabled', *RANGES)

app = Flask(__name__, static_folder=None)
lock = threading.Lock()
state = {'packet': None, 'received': 0.0, 'frames_changed': 0.0, 'last_frames': -1,
         'pop_seen': None, 'clip_seen': None, 'alarm_seen': None, 'totals': {'bang': 0, 'clip': 0, 'alarm': 0},
         'system': {}, 'atten_seen': 0, 'atten': {'bang': deque(maxlen=200), 'alarm': deque(maxlen=200)},
         'ambient': None}
history = deque(maxlen=HISTORY_POINTS)
run_history = deque(maxlen=RUN_HISTORY_POINTS)
events = deque(maxlen=EVENT_LOG)


def load_ui_state():
    try:
        saved = json.loads(UI_STATE.read_text())
    except (OSError, ValueError):
        saved = {}
    ui = dict(DEFAULT_UI_STATE)
    if isinstance(saved, dict):
        if saved.get('test_version') in VERSION_IDS and not isinstance(saved.get('test_version'), bool):
            ui['test_version'] = saved['test_version']
        if saved.get('latency_mode') in LATENCY_MODES:
            ui['latency_mode'] = saved['latency_mode']
        if isinstance(saved.get('mic_levels'), dict):        # mic level per card, by USB id
            ui['mic_levels'] = {str(k): int(v) for k, v in saved['mic_levels'].items()
                                if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if isinstance(saved.get('card_choice'), str):
            ui['card_choice'] = saved['card_choice']
        if isinstance(saved.get('passive_roles'), dict):      # which card is the outer / inner mic
            ui['passive_roles'] = saved['passive_roles']
        if isinstance(saved.get('latency_result'), dict):     # last measurement, kept for test summaries
            ui['latency_result'] = saved['latency_result']
    return ui


def save_ui_state():
    tmp = UI_STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(ui_state, indent=2))
    tmp.replace(UI_STATE)


ui_state = load_ui_state()
# Mic level of the active card (read with amixer, never from the audio loop) and the clipped-samples counters.
mic_state = {'usbid': None, 'index': None, 'label': None, 'pct': None, 'db': None, 'error': None}
clips = {'baseline': 0, 'last': 0, 'points': deque(maxlen=400), 'run_frames': -1}


def apply_saved_mic_level(card):
    """At startup and whenever the active card changes: set that card's saved level (default 80%)."""
    if card is None:
        mic_state.update(usbid=None, index=None, label=None, pct=None, db=None, error='No sound card.')
        return
    pct = ui_state['mic_levels'].get(card['usbid'], soundcards.MIC_DEFAULT_PCT)
    result = soundcards.set_mic_level(card['index'], pct)
    mic_state.update(usbid=card['usbid'], index=card['index'], label=card['label'], pct=result.get('pct'),
                     db=result.get('db'), error=result.get('error'))


def set_mic_level(pct):
    if mic_state['index'] is None:
        return {'error': 'No sound card.'}
    result = soundcards.set_mic_level(mic_state['index'], pct)
    if 'error' not in result:
        ui_state['mic_levels'][mic_state['usbid']] = result['pct']
        save_ui_state()
    mic_state.update(pct=result.get('pct', mic_state['pct']), db=result.get('db', mic_state['db']),
                     error=result.get('error'))
    return result


def mic_view(now):
    """Mic level and clipped input samples: this session (since the run started or Reset) and the last 10 s."""
    pts = clips['points']
    recent = None
    if pts and now - pts[-1][0] < 2.0:
        old = next((c for t, c in pts if t >= now - CLIP_WINDOW_S), pts[-1][1])
        recent = pts[-1][1] - old
    return {**mic_state, 'min': soundcards.MIC_MIN_PCT, 'max': soundcards.MIC_MAX_PCT,
            'default': soundcards.MIC_DEFAULT_PCT,
            'clips_session': max(0, clips['last'] - clips['baseline']) if pts else None, 'clips_10s': recent}
settings_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


class SystemControl:
    """System check and power state. One worker thread does all audio-device work in turn.

    power: 'off' | 'starting' | 'on' | 'stopping'. The checks reset when the card disappears.
    The microphone check only proves the input carries a live signal; this card cannot tell
    whether a microphone is plugged in. The headphone check is the listener's confirmation.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = []
        self.wake = threading.Event()
        self.proc = None
        self.power = 'off'
        self.message = ''
        self.usb = {'ok': False, 'detail': 'Looking for the sound card'}
        self.mic = {'ok': None, 'detail': 'Not checked yet'}
        self.phones = {'ok': None, 'detail': 'Play the test, then confirm you heard it', 'tone_played': False}
        self.was_ready = False
        self.auto_on_pending = True                     # start at power-up: always, once per server start
        self.card = None                                # active card (soundcards dict), chosen by USB id
        self.card_reason = ''
        self.run_card = None                            # USB id of the card the running pipeline was started on
        self.card_missing = False
        self.cards = []
        self.startup_cards = None                       # cards scanned once at service start (no hot-plug)
        self.latency_result = ui_state.get('latency_result')
        self.last_mic_retry = 0.0
        self.recoveries = []                            # times of automatic restarts (watchdog)
        self.stale_passes = 0                           # control-loop passes in a row with no new audio frames

    # ----- state for the page -----
    def ready(self):
        return bool(self.usb['ok'] and self.mic['ok'] and self.phones['ok'])

    def view(self):
        with self.lock:
            card = self.card
            return {'card': ({k: card.get(k) for k in ('label', 'usbid', 'device', 'note', 'known', 'port')} if card else None),
                    'cards': list(self.cards), 'card_choice': ui_state.get('card_choice'),
                    'card_reason': self.card_reason, 'card_missing': self.card_missing, 'power': self.power, 'message': self.message, 'ready': self.ready(),
                    'can_start': bool(self.usb['ok'] and self.mic['ok']), 'busy': bool(self.jobs),
                    'usb': dict(self.usb), 'mic': dict(self.mic), 'headphones': dict(self.phones),
                    'latency_result': dict(self.latency_result) if self.latency_result else None,
                    'measuring': 'latency' in self.jobs}

    def submit(self, job):
        with self.lock:
            if job not in self.jobs:
                self.jobs.append(job)
        self.wake.set()

    # ----- helpers -----
    def scan_at_start(self):
        """Scan the cards once, at service start. Cards are only plugged or unplugged with the Pi off, so the result
        holds until the next start. Waits up to CARD_WAIT_S for USB enumeration, until the set is stable."""
        begin = time.time()
        seen, since = None, time.time()
        while True:
            cards = soundcards.list_cards()
            key = [(c['usbid'], c['port'], c['rate_ok']) for c in cards]
            if key != seen:
                seen, since = key, time.time()
            waited = time.time() - begin
            if (soundcards.usable(cards) and time.time() - since >= CARD_STABLE_S) or waited >= CARD_WAIT_S:
                break
            time.sleep(0.5)
        card, reason = soundcards.choose(cards, ui_state.get('card_choice'))
        line = soundcards.start_log_line(card, reason, cards) + f' | waited {waited:.1f} s'
        print(line, flush=True)                         # journal
        try:
            CARD_SELECT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(CARD_SELECT_LOG, 'a') as f:
                f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} boot_id={boot_id()} {line}\n')
        except OSError as error:
            print(f'Sukoon card select: could not write {CARD_SELECT_LOG}: {error}', flush=True)
        if card is None:
            print(f'Sukoon card select: ERROR {reason} Plug a known card in with the Pi off and reboot.', flush=True)
        self.startup_cards = cards

    def find_card(self):
        """(active card dict or None, reason, all cards), from the cards scanned at service start: the config
        override, else the saved choice, else the fixed priority (soundcards.choose)."""
        if self.startup_cards is None:
            return None, 'Looking for the sound card', []
        cards = self.startup_cards
        return (*soundcards.choose(cards, ui_state.get('card_choice')), cards)

    def helper(self, *args, timeout=10):
        device = self.card['device'] if self.card else 'USB Audio'
        try:
            out = subprocess.run([str(PYTHON), '-I', str(HERE / 'audio_check.py'), *args],
                                 capture_output=True, text=True, timeout=timeout,
                                 env={**os.environ, 'SUKOON_DEVICE': device}).stdout
            return json.loads(out.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
            return {'ok': False, 'detail': f'Check failed: {error}'}

    def pipeline_pid(self):
        """PID of a running sukoon_engine.py: ours, or one started by hand (from its metrics packet)."""
        if self.proc is not None and self.proc.poll() is None:
            return self.proc.pid
        with lock:
            packet = state['packet']
            fresh = packet is not None and time.time() - state['received'] < STOPPED_AFTER_S
        pid = packet.get('pid') if fresh else None
        try:
            if pid and b'sukoon_engine.py' in Path(f'/proc/{pid}/cmdline').read_bytes():
                return pid
        except OSError:
            pass
        return None

    # ----- jobs (worker thread only) -----
    def job_mic(self):
        with self.lock:
            self.mic = {'ok': None, 'detail': 'Checking…'}
        result = self.helper('mic')
        level = f" ({result['rms_dbfs']:.0f} dBFS)" if 'rms_dbfs' in result else ''
        with self.lock:
            self.mic = {'ok': bool(result.get('ok')), 'detail': result.get('detail', '') + level}

    def job_tone(self):
        result = self.helper('play', str(SOUNDS / 'tone.wav'))
        with self.lock:
            if result.get('ok'):
                self.phones.update(tone_played=True, detail='Did you hear the voice?')
            else:
                self.phones.update(ok=False, detail=result.get('detail', 'Could not play the test'))

    def job_latency(self):
        mode = LATENCY_MODES[ui_state['latency_mode']]
        result = self.helper('latency', mode['latency'], str(mode['prime']), timeout=20)
        result['mode'] = ui_state['latency_mode']
        result['time'] = time.time()
        with self.lock:
            self.latency_result = result
        try:                                            # every measurement is kept, not only the last one
            LATENCY_LOG.parent.mkdir(parents=True, exist_ok=True)
            card = self.card or {}
            with open(LATENCY_LOG, 'a') as f:
                f.write(json.dumps({'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'boot_id': boot_id(),
                                    'card': card.get('label'), 'usbid': card.get('usbid'), 'port': card.get('port'),
                                    'buffer': result['mode'], 'test_version': ui_state['test_version'],
                                    'ok': bool(result.get('ok')), 'mic_to_ear_ms': result.get('mic_to_ear_ms'),
                                    'range_ms': result.get('range_ms'), 'round_trip_ms': result.get('round_trip_ms'),
                                    'sweeps_used': result.get('sweeps_used'), 'lags_ms': result.get('lags_ms'),
                                    'detail': result.get('detail')}) + '\n')
        except OSError as error:
            print(f'Latency log: could not write {LATENCY_LOG}: {error}', flush=True)
        if result.get('ok'):
            ui_state['latency_result'] = result
            save_ui_state()

    def job_restart(self):
        """Apply a new buffer setting while on: stop and start again, without the welcome voice."""
        if not self.pipeline_pid():
            return
        with self.lock:
            self.power, self.message = 'stopping', 'Applying the new buffer setting…'
        self.stop_process()
        self.job_on(voice=False)

    def job_on(self, voice=True):
        if self.pipeline_pid():
            with self.lock:
                self.power = 'on'
            return
        if self.card is None:
            with self.lock:
                self.power, self.message = 'off', 'No usable sound card. Power the Pi off, plug a known card in, and power on (cards are detected at start only).'
            return
        with self.lock:
            self.power, self.message = 'starting', 'Starting…'
            self.card_missing = False
        if voice:
            self.helper('play', str(SOUNDS / 'welcome.wav'), timeout=15)
        command = [str(PYTHON), '-I', str(ROOT / 'src' / 'sukoon_engine.py')]
        if resource.getrlimit(resource.RLIMIT_RTPRIO)[0] >= RT_PRIORITY:
            command = ['chrt', '-f', str(RT_PRIORITY), *command]
        version = ui_state['test_version']
        buffers = LATENCY_MODES[ui_state['latency_mode']]
        log = open(PIPELINE_LOG, 'a')
        log.write(f'\n=== {time.strftime("%Y-%m-%d %H:%M:%S")} start (test version {version}, buffers '
                  f'{ui_state["latency_mode"]}, card {self.card["label"]} {self.card["usbid"]} {self.card["device"]} port {self.card.get("port", "?")}, '
                  f'{soundcards.throttled()}): '
                  f'{" ".join(command)}\n')
        log.flush()
        self.run_card = self.card['usbid']
        self.proc = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                     env={**os.environ, 'SUKOON_NO_RECORD': '1',
                                          'SUKOON_TEST_VERSION': str(version),
                                          'SUKOON_DEVICE': self.card['device'],
                                          'SUKOON_TAP': TAP_PATH,
                                          'SUKOON_LATENCY': buffers['latency'],
                                          'SUKOON_PRIME': str(buffers['prime']),
                                          'SUKOON_METRICS_PORT': str(METRICS_ADDRESS[1]),
                                          'SUKOON_SETTINGS_PORT': str(SETTINGS_ADDRESS[1])})
        log.close()
        begin = time.time()
        while time.time() - begin < START_TIMEOUT_S:
            if self.proc.poll() is not None:
                break
            with lock:
                running = state['packet'] is not None and time.time() - state['frames_changed'] < STOPPED_AFTER_S \
                    and state['packet'].get('pid') == self.proc.pid
            if running:
                with self.lock:
                    self.power, self.message = 'on', ''
                return
            time.sleep(0.1)
        self.stop_process()
        with self.lock:
            self.power, self.message = 'off', 'The audio pipeline did not start. See dashboard/pipeline.log.'

    def stop_process(self, force=False):
        pid = self.pipeline_pid()
        if pid:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)   # force: a hung process cannot answer SIGTERM
            begin = time.time()
            while time.time() - begin < STOP_TIMEOUT_S and Path(f'/proc/{pid}').exists():
                if self.proc is not None and self.proc.pid == pid and self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            if Path(f'/proc/{pid}').exists() and (self.proc is None or self.proc.poll() is None):
                os.kill(pid, signal.SIGKILL)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None

    def job_recover(self):
        """Watchdog: the audio stopped or hung without being asked to. Start it again, a few times at most."""
        now = time.time()
        self.recoveries = [t for t in self.recoveries if now - t < RECOVER_WINDOW_S and t <= now]
        with open(PIPELINE_LOG, 'a') as log:
            log.write(f'\n=== {time.strftime("%Y-%m-%d %H:%M:%S")} watchdog: audio stopped unexpectedly '
                      f'({len(self.recoveries)} automatic restarts in the last {RECOVER_WINDOW_S / 60:.0f} min)\n')
        self.stop_process(force=True)                   # also ends a hung process
        if self.run_card and not soundcards.find(self.run_card):
            with self.lock:
                self.power, self.card_missing = 'off', True
                self.message = 'Sound card missing. The audio was stopped. Power off, plug the card in, power on.'
            return
        if len(self.recoveries) >= RECOVER_MAX:
            with self.lock:
                self.power = 'off'
                self.message = (f'The audio stopped {RECOVER_MAX + 1} times in {RECOVER_WINDOW_S / 60:.0f} minutes and was '
                                'left off. Turn it on again; if it keeps stopping, see dashboard/pipeline.log.')
            return
        self.recoveries.append(now)
        self.job_on(voice=False)
        with self.lock:
            if self.power == 'on':
                self.message = (f'The audio stopped unexpectedly and was restarted automatically at '
                                f'{time.strftime("%H:%M:%S")} ({len(self.recoveries)} of {RECOVER_MAX}).')

    def job_off(self):
        with self.lock:
            self.power, self.message = 'stopping', 'Turning off…'
        self.stop_process()
        with self.lock:
            self.power, self.message = 'off', ''

    # ----- loop -----
    def run(self):
        seen_card = None
        self.scan_at_start()
        while True:
            self.wake.wait(1.0)
            self.wake.clear()
            active, reason, cards = self.find_card()
            card = active and f"{active['label']} ({active['device']})"
            running = self.pipeline_pid() is not None
            if running and self.run_card and not soundcards.find(self.run_card):
                # the card the audio runs on is gone: stop cleanly instead of leaving a stuck or crashing loop
                with self.lock:
                    self.power, self.message = 'stopping', 'Sound card missing: stopping…'
                self.stop_process()
                with self.lock:
                    self.power, self.card_missing = 'off', True
                    self.message = 'Sound card missing. The audio was stopped. Plug the card back in (a reboot may be needed).'
                running = False
            if card != seen_card:
                apply_saved_mic_level(active)          # that card's saved mic level (default 80%)
            with self.lock:
                self.card, self.card_reason = active, reason
                self.cards = [{'label': c['label'], 'usbid': c['usbid'], 'device': c['device'], 'name': c['name'],
                               'port': c.get('port', '')} for c in cards]
            with self.lock:
                if card != seen_card:                  # plugged, unplugged or swapped: start over
                    self.mic = {'ok': None, 'detail': 'Not checked yet'}
                    self.phones = {'ok': None, 'detail': 'Play the test, then confirm you heard it',
                                   'tone_played': False}
                    if card and not running and 'mic' not in self.jobs:
                        self.jobs.append('mic')
                seen_card = card
                self.usb = {'ok': bool(card), 'detail': (card + ('' if reason == 'Only card plugged in.' else '. ' + reason))
                            if card else 'Not found. Plug the USB sound card into the Pi.'}
                if self.card_missing and self.power == 'off' and self.run_card and soundcards.find(self.run_card):
                    self.card_missing = False
                    self.message = 'The sound card is back. Turn on again; if it stays silent, reboot the Pi.'
                with lock:
                    stale = time.time() - state['frames_changed'] > HANG_AFTER_S
                self.stale_passes = self.stale_passes + 1 if (self.power == 'on' and running and stale) else 0
                hung = self.stale_passes >= 2           # two passes in a row, so one clock jump cannot trigger it
                if self.power == 'on' and (not running or hung) and not self.jobs:
                    if card:                            # unexpected stop or hang with the card present: recover
                        self.power, self.message = 'starting', 'The audio stopped unexpectedly. Restarting…'
                        self.jobs.append('recover')
                        self.stale_passes = 0
                    else:
                        self.power, self.message = 'off', 'The audio pipeline stopped. The sound card was unplugged.'
                if self.power == 'off' and running:
                    self.power = 'on'                  # started by hand
                job = self.jobs[0] if self.jobs else None
            if job:
                if job in ('mic', 'tone', 'latency') and (running or not card):
                    pass                               # device busy or absent: skip
                else:
                    getattr(self, 'job_' + job)()
                with self.lock:
                    self.jobs.remove(job)
                self.wake.set()
            ready = self.ready()
            if ready and not self.was_ready and self.power == 'off' and not self.jobs:
                self.helper('play', str(SOUNDS / 'ready.wav'))
            self.was_ready = ready
            self.auto_start(card, running)

    def auto_start(self, card, running):
        """Start at power-up: once the card and microphone pass, turn on without the headphone check."""
        if not self.auto_on_pending:
            return
        if running:
            self.auto_on_pending = False
            return
        with self.lock:
            idle = self.power == 'off' and not self.jobs
            mic_ok, mic_failed = self.mic['ok'], self.mic['ok'] is False
        if not (card and idle):
            return
        if mic_ok:
            self.auto_on_pending = False
            with self.lock:
                self.message = 'Started automatically at power-up.'
            self.submit('on')
        elif mic_failed and time.time() - self.last_mic_retry > AUTO_ON_MIC_RETRY_S:
            self.last_mic_retry = time.time()
            self.submit('mic')


control = SystemControl()


def validate(settings):
    """Keep only known settings with valid types, clamped to range; FLOOR_MIN_DB <= FLOOR_MAX_DB."""
    if not isinstance(settings, dict):
        return {}
    clean = {}
    if settings.get('reset_defaults') is True:
        clean['reset_defaults'] = True
    level = settings.get('mic_level')               # applied by the server (amixer), not sent to sukoon_engine.py
    if not isinstance(level, bool) and isinstance(level, (int, float)) and math.isfinite(level):
        clean['mic_level'] = int(round(min(max(level, soundcards.MIC_MIN_PCT), soundcards.MIC_MAX_PCT)))
    for key in SWITCHES:
        if isinstance(settings.get(key), bool):
            clean[key] = settings[key]
    for key, (low, high) in RANGES.items():
        value = settings.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        value = min(max(value, low), high)
        clean[key] = int(round(value)) if isinstance(low, int) else round(float(value), 1)
    if 'FLOOR_MIN_DB' in clean and 'FLOOR_MAX_DB' in clean and clean['FLOOR_MIN_DB'] > clean['FLOOR_MAX_DB']:
        clean['FLOOR_MAX_DB'] = clean['FLOOR_MIN_DB']
    return clean


def listen_metrics():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(METRICS_ADDRESS)
    while True:
        try:
            packet = json.loads(rx.recv(65536))
        except (OSError, ValueError):
            continue
        now = time.time()
        with lock:
            frames = packet.get('live', {}).get('frames', 0)
            if frames < state['last_frames']:              # sukoon_engine.py restarted: new counters
                state['pop_seen'] = state['clip_seen'] = state['alarm_seen'] = None
                state['atten_seen'] = 0
                history.clear()
                run_history.clear()
            if frames != state['last_frames']:
                state['frames_changed'] = now
            rail = packet.get('live', {}).get('input_rail_samples')
            if rail is not None:
                if frames < clips['run_frames'] or rail < clips['last']:      # new run: the session starts over
                    clips['baseline'] = 0
                    clips['points'].clear()
                clips['run_frames'] = frames
                clips['last'] = rail
                clips['points'].append((now, rail))
            state['last_frames'] = frames
            for kind, key, seen in (('bang', 'pop_triggers', 'pop_seen'), ('clip', 'clip_guard_triggers', 'clip_seen'),
                                    ('alarm', 'alarm_events', 'alarm_seen')):
                count = packet.get(key) or 0
                if state[seen] is not None and count > state[seen]:
                    state['totals'][kind] += count - state[seen]
                    events.appendleft({'time': now, 'kind': kind, 'count': count - state[seen]})
                state[seen] = count
            attenuation = packet.get('attenuation') or {}
            for record in attenuation.get('events', []):
                if record['seq'] <= state['atten_seen']:
                    continue
                state['atten_seen'] = record['seq']
                if record['kind'] not in ('bang', 'alarm'):
                    continue                               # Test 3 hold on an ordinary sound: not reduced, not logged
                state['atten'][record['kind']].append(record['reduction_db'])
                for event in events:                       # newest first: the row this measurement belongs to
                    if event['kind'] == record['kind'] and 'reduction_db' not in event:
                        event['reduction_db'] = record['reduction_db']
                        break
            if attenuation.get('ambient'):
                state['ambient'] = attenuation['ambient']
            window = packet.get('window', {})
            point = [round(now, 2), window.get('in_peak_dbfs', -120), window.get('out_peak_dbfs', -120),
                     int(bool(packet.get('pop_hold'))), int(bool(packet.get('voice_hold')))]
            history.append(point)
            run_history.append(point)
            version = (packet.get('state') or {}).get('test_version')
            if version is not None and version != state.get('version_seen'):
                if state.get('version_seen') is not None:
                    events.appendleft({'time': now, 'kind': 'version', 'count': version})
                state['version_seen'] = version
            spectrum = packet.pop('spectrum', None)        # kept on its own: still shown after the run stops
            if spectrum is not None:
                state['spectrum'] = spectrum
            state['packet'] = packet
            state['received'] = now


def read_cpu():
    values = list(map(int, Path('/proc/stat').read_text().split('\n', 1)[0].split()[1:]))
    return sum(values), values[3] + values[4]


def vcgencmd(*args):
    try:
        return subprocess.run(['vcgencmd', *args], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def decode_throttled(text):
    try:
        bits = int(text.split('=')[1], 16)
    except (IndexError, ValueError):
        return {'label': 'Unknown', 'level': 'unknown', 'raw': text}
    if bits & 0x1:
        label, level = 'Low voltage now', 'bad'
    elif bits & 0x6:
        label, level = 'Throttled', 'bad'
    elif bits & 0x10000:
        label, level = 'Low voltage since boot', 'warn'
    elif bits & 0x60000:
        label, level = 'Throttled since boot', 'warn'
    else:
        label, level = 'Power OK', 'ok'
    flags = {'under_voltage_now': 0x1, 'freq_capped_now': 0x2, 'throttled_now': 0x4, 'soft_temp_limit_now': 0x8,
             'under_voltage_since_boot': 0x10000, 'freq_capped_since_boot': 0x20000,
             'throttled_since_boot': 0x40000, 'soft_temp_limit_since_boot': 0x80000}
    return {'label': label, 'level': level, 'raw': f'0x{bits:x}',
            'flags': [name for name, mask in flags.items() if bits & mask]}


RECORD_BYTES_PER_S = 2 * 48000 * 2          # input + output, 16-bit mono WAV
RECORD_RAM_BYTES_PER_S = 2 * 48000 * 4      # float32 blocks held in memory until the run stops
RECORD_MAX_S = 600                           # sukoon_engine.py keeps the first 10 minutes of a run


def read_storage():
    """Disk space, saved recordings and memory. Cheap: one statvfs, one directory glob, /proc/meminfo."""
    st = os.statvfs(str(ROOT))
    total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    files = list(ROOT.glob('live_*_input.wav')) + list(ROOT.glob('live_*_output.wav'))
    rec_bytes = sum(f.stat().st_size for f in files)
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0]) * 1024
    return {'disk_total': total, 'disk_free': free, 'disk_used': total - free,
            'recordings_bytes': rec_bytes, 'recordings_runs': len({f.name.rsplit('_', 1)[0] for f in files}),
            'record_minutes_left': round(free / RECORD_BYTES_PER_S / 60),
            'run_disk_bytes': RECORD_BYTES_PER_S * RECORD_MAX_S, 'run_ram_bytes': RECORD_RAM_BYTES_PER_S * RECORD_MAX_S,
            'ram_total': mem.get('MemTotal', 0), 'ram_available': mem.get('MemAvailable', 0)}


def sample_system():
    total0, idle0 = read_cpu()
    storage, tick = {}, 0
    while True:
        time.sleep(1.0)
        total1, idle1 = read_cpu()
        load = 100.0 * (1 - (idle1 - idle0) / max(total1 - total0, 1))
        total0, idle0 = total1, idle1
        try:
            temp = int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000.0
        except (OSError, ValueError):
            temp = None
        clock = vcgencmd('measure_clock', 'arm')
        try:
            mhz = int(clock.split('=')[1]) / 1e6
        except (IndexError, ValueError):
            mhz = int(Path('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq').read_text()) / 1000.0
        governor = Path('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor').read_text().strip()
        uptime = float(Path('/proc/uptime').read_text().split()[0])
        system = {'cpu_pct': round(load, 1), 'load_avg': Path('/proc/loadavg').read_text().split()[:3],
                  'temp_c': temp, 'clock_mhz': round(mhz), 'governor': governor, 'pi_uptime_s': round(uptime),
                  'power': decode_throttled(vcgencmd('get_throttled'))}
        if tick % 10 == 0:
            try:
                storage = read_storage()
            except (OSError, ValueError):
                pass
        tick += 1
        system['storage'] = storage
        with lock:
            state['system'] = system


# Anything that is saving audio to storage registers here (timed test, passive test, battery clips); the page shows
# a Recording badge while any is active. Readers only need {'active': bool}; 'kind' and 'label' say what.
recorders = {}


def recording_status():
    with lock:
        active = {k: v for k, v in recorders.items() if v}
    if not active:
        return {'active': False}
    kind = next(iter(active))
    return {'active': True, 'kind': kind, 'label': active[kind], 'all': sorted(active)}


def set_recording(kind, label=None):
    """label None clears it."""
    with lock:
        recorders[kind] = label


def summarize(values):
    values = list(values)
    if not values:
        return None
    ordered = sorted(values)
    return {'last': values[-1], 'median': ordered[len(ordered) // 2], 'min': ordered[0], 'n': len(values)}


def snapshot(full_history=False):
    """Everything the page needs, assembled under the lock."""
    now = time.time()
    with lock:
        packet = state['packet']
        running = packet is not None and now - state['frames_changed'] < STOPPED_AFTER_S
        status = 'Off' if not running else ('Bypassed' if packet['state']['bypass'] else 'Running')
        live = packet['live'] if packet else {}
        latency = None
        if live:
            latency = round(live['algorithm_delay_ms'] + live['frame_ms'] + live['device_input_latency_ms']
                            + live['device_output_latency_ms'], 1)
        data = {'now': now, 'status': status, 'packet': packet if running else None, 'system': state['system'],
                'estimated_latency_ms': latency,
                'run_seconds': round(live.get('frames', 0) * live.get('frame_ms', 10) / 1000.0, 1) if live else 0,
                'events': list(events)[:EVENTS_IN_STREAM], 'totals': dict(state['totals']),
                'history': list(history) if full_history else (list(history)[-1:] if history else []),
                'run_history_points': len(run_history),
                'attenuation': {kind: summarize(values) for kind, values in state['atten'].items()}}
        data['attenuation']['ambient'] = state['ambient'] if running else None
        data['spectrum'] = state.get('spectrum')
        data['spectrum_live'] = running
    data['control'] = control.view()
    data['control'].update(test_version=ui_state['test_version'], auto_on=ui_state['auto_on'],
                           latency_mode=ui_state['latency_mode'])
    data['battery'] = battery_view()
    data['recording'] = recording_status()
    data['timed_test'] = timed_view() if timed['dir'] else None
    with lock:
        data['mic'] = mic_view(now)
    data['test_versions'] = TEST_VERSIONS
    data['latency_modes'] = [{'id': k, 'name': v['name'], 'detail': v['detail']} for k, v in LATENCY_MODES.items()]
    if data['control']['power'] in ('starting', 'stopping'):
        data['status'] = data['control']['power'].capitalize()
    return data


@app.get('/')
def index():
    return send_from_directory(HERE / 'static', 'index.html')


RECORDINGS_DIR = HERE / 'recordings'


@app.get('/recordings')
def recordings_list():
    """WAV files dropped into dashboard/recordings/, for playback on the page."""
    files = sorted(RECORDINGS_DIR.glob('*.wav')) if RECORDINGS_DIR.is_dir() else []
    out = []
    for f in files:
        secs = None
        try:
            import wave
            with wave.open(str(f)) as w:
                secs = round(w.getnframes() / w.getframerate(), 1)
        except Exception:
            pass
        out.append({'name': f.name, 'bytes': f.stat().st_size, 'seconds': secs})
    return jsonify({'recordings': out, 'playing': player_now()})


player = {'proc': None, 'name': None}       # headset playback of one recording (aplay), Sukoon off only
player_lock = threading.Lock()


def live_inject():
    """What Sukoon is feeding into its input right now (from its metrics packet), or None."""
    with lock:
        packet = state['packet']
        fresh = packet is not None and time.time() - state['received'] < STOPPED_AFTER_S
    return packet.get('inject') if fresh else None


def player_now():
    """{'name', 'via', ...} for what is playing on the headset, or None."""
    inj = live_inject()
    if inj:
        return {**inj, 'via': 'sukoon'}
    with player_lock:
        p = player['proc']
        return {'name': player['name'], 'via': 'raw'} if p is not None and p.poll() is None else None


def player_stop():
    with player_lock:
        p, player['proc'], player['name'] = player['proc'], None, None
    if p is not None and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()


@app.post('/recordings/stop')
def recordings_stop():
    player_stop()
    if control.pipeline_pid():
        settings_socket.sendto(json.dumps({'inject_stop': True}).encode(), SETTINGS_ADDRESS)
    return jsonify({'ok': True})


@app.post('/recordings/<path:name>/play')
def recordings_play(name):
    """Play one recording on the headset.

    Sukoon on: the recording is fed into Sukoon's input (mixed with the live mic, or in its place)
    and heard after processing, so its bangs are handled like real ones. Sukoon off: played as is.
    """
    f = RECORDINGS_DIR / name
    if '/' in name or f.suffix.lower() != '.wav' or not f.is_file():
        return jsonify({'ok': False, 'error': 'No such recording.'}), 404
    body = request.get_json(silent=True) or {}
    view = control.view()
    if control.pipeline_pid():
        gain = body.get('gain_db', 0)
        gain = 0 if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) else gain
        packet = {'inject_play': name, 'inject_gain_db': min(max(gain, -30), 10),
                  'inject_keep_mic': body.get('keep_mic', True) is not False}
        settings_socket.sendto(json.dumps(packet).encode(), SETTINGS_ADDRESS)
        return jsonify({'ok': True, 'via': 'sukoon'})
    if view['power'] != 'off':
        return jsonify({'ok': False, 'error': 'Sukoon is starting or stopping. Try again in a moment.'}), 409
    if not view['card']:
        return jsonify({'ok': False, 'error': 'No usable sound card. Power off, plug a known card in, power on.'}), 409
    if view['busy']:
        return jsonify({'ok': False, 'error': 'The sound card is busy with a check. Try again in a moment.'}), 409
    player_stop()
    device = view['card']['device']
    device = 'plug' + device if device.startswith('hw:') else 'default'
    try:
        p = subprocess.Popen(['aplay', '-q', '-D', device, str(f)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        return jsonify({'ok': False, 'error': f'Could not play: {error}'}), 500
    with player_lock:
        player['proc'], player['name'] = p, name
    return jsonify({'ok': True, 'via': 'raw'})


@app.get('/recordings/<path:name>')
def recordings_file(name):
    """One recording, with range support so the player can seek."""
    return send_from_directory(RECORDINGS_DIR, name, mimetype='audio/wav')


@app.get('/fonts/<path:name>')
def fonts(name):
    """Bundled Inter font files for the page (read-only, cached a week)."""
    return send_from_directory(HERE / 'static' / 'fonts', name, max_age=7 * 24 * 3600)


@app.get('/metrics')
def metrics():
    return jsonify(snapshot(full_history=True))


@app.get('/stream')
def stream():
    def generate():
        yield 'retry: 1000\n\n'
        yield f'event: init\ndata: {json.dumps(snapshot(full_history=True))}\n\n'
        while True:
            time.sleep(0.1)
            yield f'data: {json.dumps(snapshot())}\n\n'
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.post('/settings')
def settings():
    clean = validate(request.get_json(silent=True))
    if not clean:
        return jsonify({'ok': False, 'error': 'No valid settings in request.'}), 400
    reply = {'ok': True}
    if clean.get('reset_defaults'):
        clean.setdefault('mic_level', soundcards.MIC_DEFAULT_PCT)
    if 'mic_level' in clean:
        result = set_mic_level(clean.pop('mic_level'))
        if 'error' in result:
            return jsonify({'ok': False, 'error': result['error']}), 409
        reply['mic_level'] = result['pct']
    if clean:
        settings_socket.sendto(json.dumps(clean).encode(), SETTINGS_ADDRESS)
    reply['sent'] = clean
    return jsonify(reply)


@app.post('/mic_clips/reset')
def reset_mic_clips():
    """Start the session's clipped-samples count from zero."""
    with lock:
        clips['baseline'] = clips['last']
    return jsonify({'ok': True})


@app.get('/events')
def get_events():
    """The whole transients log (up to EVENT_LOG entries), newest first."""
    with lock:
        return jsonify({'events': list(events)})


@app.get('/history')
def get_history():
    """The whole current or last run, [t, in_peak, out_peak, bang, speech] at 10 Hz, for review."""
    with lock:
        points = list(run_history)
    return jsonify({'points': points})


@app.post('/test_version')
def set_test_version():
    body = request.get_json(silent=True) or {}
    version = body.get('version')
    if isinstance(version, bool) or version not in VERSION_IDS:
        return jsonify({'ok': False, 'error': f'version must be one of {sorted(VERSION_IDS)}.'}), 400
    ui_state['test_version'] = version
    save_ui_state()
    if control.pipeline_pid():                         # running: switch live; otherwise used at the next start
        settings_socket.sendto(json.dumps({'test_version': version}).encode(), SETTINGS_ADDRESS)
    return jsonify({'ok': True, 'test_version': version})


@app.post('/latency_mode')
def set_latency_mode():
    body = request.get_json(silent=True) or {}
    mode = body.get('mode')
    if mode not in LATENCY_MODES:
        return jsonify({'ok': False, 'error': f'mode must be one of {sorted(LATENCY_MODES)}.'}), 400
    changed = mode != ui_state['latency_mode']
    ui_state['latency_mode'] = mode
    save_ui_state()
    if changed and control.pipeline_pid():
        control.submit('restart')                      # buffers are fixed when the stream opens
    return jsonify({'ok': True, 'latency_mode': mode, 'restarting': changed and bool(control.pipeline_pid())})


@app.post('/auto_on')
def set_auto_on():
    """Kept so old pages and scripts don't break. Start at power-up is now permanent; the request changes nothing."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get('on'), bool):
        return jsonify({'ok': False, 'error': 'Send {"on": true} or {"on": false}.'}), 400
    return jsonify({'ok': True, 'auto_on': True, 'note': 'Start at power-up is always on.'})


@app.get('/cards')
def get_cards():
    """USB sound cards plugged in now, the active one and why."""
    view = control.view()
    return jsonify({'cards': soundcards.list_cards(), 'active': view['card'], 'reason': view['card_reason'],
                    'choice': ui_state.get('card_choice')})


@app.post('/card')
def set_card():
    """{"usbid": "0020:0b21"} saves which card to use when both are plugged in (null clears it). Only while Off."""
    body = request.get_json(silent=True) or {}
    usbid = body.get('usbid')
    if usbid is not None and not (isinstance(usbid, str) and re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{4}', usbid)):
        return jsonify({'ok': False, 'error': 'Send {"usbid": "vvvv:pppp"} or {"usbid": null}.'}), 400
    if control.view()['power'] != 'off':
        return jsonify({'ok': False, 'error': 'Turn Sukoon off to change the sound card.'}), 409
    ui_state['card_choice'] = usbid
    save_ui_state()
    control.wake.set()
    return jsonify({'ok': True, 'choice': usbid})


@app.post('/power')
def power():
    body = request.get_json(silent=True) or {}
    on = body.get('on')
    if not isinstance(on, bool):
        return jsonify({'ok': False, 'error': 'Send {"on": true} or {"on": false}.'}), 400
    view = control.view()
    battery_keep_on = body.get('source') == 'battery' and battery_active()[0] is not None
    if on and view['power'] == 'off' and not view['can_start'] and not battery_keep_on:   # same rule as the automatic start
        return jsonify({'ok': False, 'error': 'The sound card and the microphone check must pass first (see System).'}), 409
    if on:
        player_stop()                                  # the headset recording gives the card back
    control.submit('on' if on else 'off')
    return jsonify({'ok': True})


@app.post('/check/<name>')
def check(name):
    if name not in ('mic', 'tone', 'heard', 'not_heard', 'latency'):
        return jsonify({'ok': False, 'error': 'Unknown check.'}), 404
    if control.view()['power'] != 'off':
        return jsonify({'ok': False, 'error': 'Turn the system off to run checks.'}), 409
    if name in ('heard', 'not_heard'):
        with control.lock:
            if not control.phones.get('tone_played'):
                return jsonify({'ok': False, 'error': 'Play the test tone first.'}), 409
            heard = name == 'heard'
            control.phones.update(ok=heard, detail='Confirmed by listener' if heard else
                                  'Not heard. Check the headphone plug and volume, then play the tone again.')
        control.wake.set()
    else:
        control.submit(name)
    return jsonify({'ok': True})


# ----- timed test (tools/timed_test.py in its own low-priority process; it reads the audio tap) -----
timed = {'proc': None, 'dir': None, 'error': None}
STAMP_RE = re.compile(r'^\d{8}_\d{6}$')


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def dir_size(path):
    return sum(f.stat().st_size for f in Path(path).rglob('*') if f.is_file())


def disk_free():
    st = os.statvfs(str(ROOT))
    return st.f_bavail * st.f_frsize


def timed_view():
    """Progress of the running or last timed test, for the snapshot (small)."""
    proc, folder = timed['proc'], timed['dir']
    running = proc is not None and proc.poll() is None
    status = read_json(folder / 'status.json') if folder else None
    if proc is not None and not running and status and status.get('state') in ('starting', 'running', 'finishing'):
        status = {**status, 'state': 'error', 'error': 'The test stopped unexpectedly. See dashboard/pipeline.log.'}
    return {'running': running, 'id': folder.name if folder else None, **(status or {})}


def watch_timed():
    timed['proc'].wait()
    set_recording('timed', None)


def runs_list(base, zip_name):
    runs = []
    if base.exists():
        for d in sorted((x for x in base.iterdir() if x.is_dir() and STAMP_RE.match(x.name)), reverse=True)[:HISTORY_SHOWN]:
            summary = read_json(d / 'summary.json') or {}
            status = read_json(d / 'status.json') or read_json(d / 'state.json') or {}
            runs.append({'id': d.name, 'bytes': dir_size(d), 'zip': (d / zip_name(d.name)).exists(),
                         'state': status.get('state'), 'summary': {k: summary.get(k) for k in (
                             'start_time', 'duration_recorded_s', 'duration_requested_s', 'test_version', 'bangs',
                             'output_sample_peak_max_dbfs', 'stopped_by', 'error', 'duration_h', 'end_reason',
                             'clips_saved', 'mean_insertion_loss_db', 'valid_bands', 'interrupted')}})
    return runs


def run_dir(base, run_id):
    if not STAMP_RE.match(run_id or ''):
        return None
    d = base / run_id
    return d if d.is_dir() else None


def send_download(path, name, mimetype):
    """Attachment download that iPhone Safari saves to Files (Content-Disposition: attachment)."""
    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=name, max_age=0)


def sukoon_running():
    return bool(control.pipeline_pid()) and snapshot()['status'] in ('Running', 'Bypassed')


@app.post('/test/start')
def timed_start():
    body = request.get_json(silent=True) or {}
    minutes = body.get('minutes')
    if isinstance(minutes, bool) or minutes not in (1, 2, 3, 4, 5):
        return jsonify({'ok': False, 'error': 'Choose 1, 2, 3, 4 or 5 minutes.'}), 400
    if timed['proc'] is not None and timed['proc'].poll() is None:
        return jsonify({'ok': False, 'error': 'A timed test is already running.'}), 409
    if not sukoon_running():
        return jsonify({'ok': False, 'error': 'Sukoon is off. Turn it on first: the test records the current '
                                              'test version while it runs.'}), 409
    if disk_free() < 200 * 10 ** 6:
        return jsonify({'ok': False, 'error': 'Storage is almost full. Delete old tests first.'}), 409
    folder = TESTS_DIR / time.strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True, exist_ok=True)
    log = open(PIPELINE_LOG, 'a')
    log.write(f'\n=== {time.strftime("%Y-%m-%d %H:%M:%S")} timed test {folder.name}, {minutes} min\n')
    log.flush()
    timed['proc'] = subprocess.Popen(['nice', '-n', '19', 'taskset', '-c', RECORDER_CPU, str(PYTHON), str(ROOT / 'tools' / 'timed_test.py'),
                                      '--minutes', str(minutes), '--out', str(folder),
                                      '--metrics', f'http://127.0.0.1:{HTTP_PORT}', '--tap', TAP_PATH],
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                     env={**os.environ, 'OPENBLAS_NUM_THREADS': '1'})
    log.close()
    timed['dir'] = folder
    set_recording('timed', f'Timed test, {minutes} min')
    threading.Thread(target=watch_timed, daemon=True).start()
    return jsonify({'ok': True, 'id': folder.name})


@app.post('/test/stop')
def timed_stop():
    proc = timed['proc']
    if proc is None or proc.poll() is not None:
        return jsonify({'ok': False, 'error': 'No timed test is running.'}), 409
    proc.send_signal(signal.SIGTERM)          # the runner finishes the files and the zip
    return jsonify({'ok': True})


@app.get('/tests')
def timed_list():
    free = disk_free()
    return jsonify({'runs': runs_list(TESTS_DIR, lambda i: f'test_{i}.zip'),
                    'state': timed_view() if timed['dir'] else None,
                    'disk_free': free, 'low_storage': free < LOW_STORAGE_BYTES})


@app.get('/tests/<run_id>/download')
def timed_download(run_id):
    d = run_dir(TESTS_DIR, run_id)
    z = d / f'test_{run_id}.zip' if d else None
    if not z or not z.exists():
        return jsonify({'ok': False, 'error': 'No zip for that test.'}), 404
    return send_download(z, z.name, 'application/zip')


@app.get('/tests/<run_id>/file/<kind>')
def timed_file(run_id, kind):
    d = run_dir(TESTS_DIR, run_id)
    f = d / f'test_{run_id}_{kind}.wav' if d and kind in ('input', 'output') else None
    if not f or not f.exists():
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    return send_download(f, f.name, 'audio/wav')


@app.delete('/tests/<run_id>')
def timed_delete(run_id):
    d = run_dir(TESTS_DIR, run_id)
    if not d:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    if timed['dir'] == d and timed['proc'] is not None and timed['proc'].poll() is None:
        return jsonify({'ok': False, 'error': 'That test is still running.'}), 409
    shutil.rmtree(d)
    return jsonify({'ok': True})


# ----- battery test (tools/battery_log.py --run as a systemd user service; see tools/battery_run.py) -----
BATTERY_DIR = ROOT / 'logs' / 'battery_runs'
BATTERY_UNIT = os.environ.get('SUKOON_BATTERY_UNIT', 'sukoon-battery')
battery_cache = {'t': 0.0, 'view': None}
FORM_TEXT = ('bank', 'bank_capacity', 'room_temp', 'enclosure', 'ambient', 'stimulus_file', 'speaker_volume',
             'spl_at_mic', 'distance', 'note')


def user_systemd_env():
    """The service runs without a login session; point systemctl --user at the service user's manager (linger is on)."""
    uid = os.getuid()
    return {**os.environ, 'XDG_RUNTIME_DIR': f'/run/user/{uid}',
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{uid}/bus'}


def systemctl_user(*args, timeout=10):
    try:
        return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=timeout,
                              env=user_systemd_env())
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(args, 1, '', str(error))


def this_boot():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return ''


def battery_alive(st):
    """The logger process of a run's state.json is still running (same boot, pid alive, is battery_log.py)."""
    if not st or st.get('boot_id') != this_boot():
        return False
    try:
        return b'battery_log.py' in Path(f"/proc/{st.get('pid')}/cmdline").read_bytes()
    except OSError:
        return False


def finalize_battery(folder):
    subprocess.Popen(['nice', '-n', '19', 'taskset', '-c', RECORDER_CPU, str(PYTHON), str(ROOT / 'tools' / 'battery_log.py'),
                      '--finalize', str(folder)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env={**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'SUKOON_DASHBOARD': f'http://127.0.0.1:{HTTP_PORT}'},
                     start_new_session=True)


def battery_graphs_when_off():
    """Graphs left for later (Sukoon was on when a run ended): draw them once Sukoon is off."""
    while True:
        time.sleep(30)
        if not BATTERY_DIR.exists() or control.pipeline_pid() or control.view()['power'] != 'off':
            continue
        for d in BATTERY_DIR.iterdir():
            st = read_json(d / 'state.json') or {}
            if st.get('graphs_pending') and st.get('state') in ('finished', 'interrupted'):
                st['graphs_pending'] = False
                (d / 'state.json').write_text(json.dumps(st, indent=1))
                finalize_battery(d)
                break


def mark_interrupted_runs():
    """A run left 'running' by a power loss or a killed logger: mark it interrupted, end = last row; never resume."""
    if not BATTERY_DIR.exists():
        return
    for d in BATTERY_DIR.iterdir():
        st = read_json(d / 'state.json')
        if st and st.get('state') == 'running' and not battery_alive(st):
            st.update(state='interrupted', end_reason='interrupted: power lost or logger stopped; '
                      'the end time is the last row written', marked_at=time.time())
            tmp = d / 'state.json.tmp'
            tmp.write_text(json.dumps(st, indent=1))
            tmp.replace(d / 'state.json')
            finalize_battery(d)


def battery_active():
    """(folder, state) of the running battery test, or (None, None)."""
    if not BATTERY_DIR.exists():
        return None, None
    for d in sorted(BATTERY_DIR.iterdir(), reverse=True):
        st = read_json(d / 'state.json')
        if st and st.get('state') == 'running' and battery_alive(st):
            return d, st
    return None, None


def battery_view():
    """Small state for the snapshot, refreshed at most once a second."""
    now = time.time()
    if now - battery_cache['t'] < 1.0:
        return battery_cache['view']
    d, st = battery_active()
    view = None
    if d:
        form = read_json(d / 'form.json') or {}
        view = {'running': True, 'id': d.name, 'elapsed_s': st.get('last_elapsed_s', 0), 'rows': st.get('rows', 0),
                'clips_saved': st.get('clips_saved', 0), 'clips_skipped': st.get('clips_skipped', 0),
                'clips_enabled': st.get('clips_enabled'), 'clips_off_reason': st.get('clips_off_reason'),
                'last_temp_c': st.get('last_temp_c'), 'last_throttled': st.get('last_throttled'),
                'sukoon': st.get('sukoon'), 'bangs': st.get('bangs', 0), 'start_mono': st.get('start_mono'),
                'max_hours': form.get('max_hours')}
    battery_cache.update(t=now, view=view)
    set_recording('battery', 'Battery test clips' if view and view.get('clips_enabled') else None)
    return view


def clean_text(value, limit=200):
    return str(value).strip()[:limit] if value is not None else ''


@app.post('/battery/start')
def battery_start():
    body = request.get_json(silent=True) or {}
    if battery_active()[0]:
        return jsonify({'ok': False, 'error': 'A battery test is already running.'}), 409
    view = control.view()
    if not view['card'] or view['card_missing']:
        return jsonify({'ok': False, 'error': 'Sukoon has a sound card problem (no card or card missing). Fix that first.'}), 409
    raw = (state.get('system') or {}).get('power', {}).get('raw', '')
    try:
        if int(raw, 16) & 0x1:
            return jsonify({'ok': False, 'error': f'The Pi reports low voltage right now (throttled {raw}). Use a '
                                                  'better supply or cable; the test would not mean anything.'}), 409
    except ValueError:
        pass
    form = {k: clean_text(body.get(k)) for k in FORM_TEXT}
    for key, low, high in (('start_charge_pct', 0, 100), ('meter_start_wh', 0, 1000), ('max_hours', 0, 48)):
        v = body.get(key)
        if v in (None, ''):
            form[key] = None
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': f'{key} must be a number.'}), 400
        if not low <= v <= high:
            return jsonify({'ok': False, 'error': f'{key} must be between {low} and {high}.'}), 400
        form[key] = v
    if not form['bank']:
        return jsonify({'ok': False, 'error': 'Enter the power bank model or capacity.'}), 400
    form['save_clips'] = body.get('save_clips') is not False
    phone_ms = body.get('phone_time_ms')
    now = time.time()
    plausible = isinstance(phone_ms, (int, float)) and not isinstance(phone_ms, bool) and phone_ms > 1.6e12
    form['phone_offset_s'] = round(phone_ms / 1000.0 - now, 3) if plausible else 0.0     # phone clock minus Pi clock
    form['start_time_pi'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
    form['start_time_phone'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now + form['phone_offset_s']))
    pk = state.get('packet') or {}
    form['volume_target_dbfs'] = (pk.get('settings') or {}).get('TARGET_SPEECH_DBFS', body.get('volume'))
    form['test_version'] = ui_state['test_version']
    form['card'] = view['card']
    form['cards_detected'] = view['cards']           # every USB card plugged in at the start, by USB id
    form['mic_level_pct'] = mic_state.get('pct')
    folder = BATTERY_DIR / time.strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'form.json').write_text(json.dumps(form, indent=1))
    if not control.pipeline_pid():
        control.submit('on')                       # the battery test keeps Sukoon on (as --keep-on does)
    systemctl_user('reset-failed', BATTERY_UNIT)
    try:
        result = subprocess.run(['systemd-run', '--user', f'--unit={BATTERY_UNIT}', '--collect', '--quiet',
                                 '-p', 'Nice=10', '-p', f'CPUAffinity={RECORDER_CPU}', '-p', 'TimeoutStopSec=600',
                                 '--setenv=OPENBLAS_NUM_THREADS=1', f'--working-directory={ROOT}',
                                 str(PYTHON), str(ROOT / 'tools' / 'battery_log.py'), '--run', str(folder),
                                 '--metrics', f'http://127.0.0.1:{HTTP_PORT}', '--tap', TAP_PATH],
                                capture_output=True, text=True, timeout=15, env=user_systemd_env())
    except (OSError, subprocess.SubprocessError) as error:
        result = subprocess.CompletedProcess([], 1, '', str(error))
    if result.returncode != 0:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify({'ok': False, 'error': f'Could not start the battery service: {result.stderr.strip()[:300]}'}), 500
    battery_cache['t'] = 0.0
    return jsonify({'ok': True, 'id': folder.name, 'phone_offset_s': form['phone_offset_s']})


@app.post('/battery/stop')
def battery_stop():
    d, _ = battery_active()
    if not d:
        return jsonify({'ok': False, 'error': 'No battery test is running.'}), 409
    systemctl_user('stop', '--no-block', BATTERY_UNIT)    # the logger writes the summary, graphs and zip
    battery_cache['t'] = 0.0
    return jsonify({'ok': True})


@app.post('/battery/stimulus')
def battery_stimulus():
    """Stimulus started: a timestamped event to line known bang times in the stimulus file up with triggers."""
    d, st = battery_active()
    if not d:
        return jsonify({'ok': False, 'error': 'No battery test is running.'}), 409
    body = request.get_json(silent=True) or {}
    form = read_json(d / 'form.json') or {}
    now = time.time()
    elapsed = round(time.monotonic() - float(st['start_mono']), 2)
    phone_ms = body.get('phone_time_ms')
    phone = now + (form.get('phone_offset_s') or 0.0)
    with open(d / 'events.csv', 'a', newline='') as f:
        f.write(f"{elapsed},{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))},"
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(phone))},Stimulus started"
                + (f" (phone {phone_ms})" if isinstance(phone_ms, (int, float)) else '') + '\n')
        f.flush()
        os.fsync(f.fileno())
    return jsonify({'ok': True, 'elapsed_s': elapsed})


@app.post('/battery/<run_id>/notes')
def battery_notes(run_id):
    d = run_dir(BATTERY_DIR, run_id)
    if not d:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    st = read_json(d / 'state.json') or {}
    if st.get('state') == 'running':
        return jsonify({'ok': False, 'error': 'The test is still running.'}), 409
    body = request.get_json(silent=True) or {}
    notes = read_json(d / 'notes.json') or {}
    v = body.get('meter_end_wh')
    if v not in (None, ''):
        try:
            notes['meter_end_wh'] = float(v)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'End meter reading must be a number (Wh).'}), 400
    if 'end_notes' in body:
        notes['end_notes'] = clean_text(body.get('end_notes'), 1000)
    notes['edited'] = time.strftime('%Y-%m-%d %H:%M:%S')
    (d / 'notes.json').write_text(json.dumps(notes, indent=1))
    zf = d / f'battery_{d.name}.zip'
    if zf.exists():
        zf.unlink()                                # rebuilt with the new summary
    finalize_battery(d)
    return jsonify({'ok': True, 'notes': notes})


@app.get('/battery')
def battery_list():
    mark_interrupted_runs()
    runs = runs_list(BATTERY_DIR, lambda i: f'battery_{i}.zip')
    for r in runs:
        st = read_json(BATTERY_DIR / r['id'] / 'state.json') or {}
        r['state'] = st.get('state')
        r['detail'] = {k: st.get(k) for k in ('start_time', 'last_wall', 'last_elapsed_s', 'rows', 'clips_saved',
                                               'clips_skipped', 'end_reason', 'bangs')}
        r['notes'] = read_json(BATTERY_DIR / r['id'] / 'notes.json') or {}
    free = disk_free()
    battery_cache['t'] = 0.0
    return jsonify({'runs': runs, 'state': battery_view(), 'disk_free': free, 'low_storage': free < LOW_STORAGE_BYTES})


@app.get('/battery/<run_id>/download')
def battery_download(run_id):
    d = run_dir(BATTERY_DIR, run_id)
    z = d / f'battery_{run_id}.zip' if d else None
    if not z or not z.exists():
        return jsonify({'ok': False, 'error': 'The zip is not ready yet. Try again in a minute.'}), 404
    return send_download(z, z.name, 'application/zip')


@app.delete('/battery/<run_id>')
def battery_delete(run_id):
    d = run_dir(BATTERY_DIR, run_id)
    if not d:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    st = read_json(d / 'state.json') or {}
    if st.get('state') == 'running' and battery_alive(st):
        return jsonify({'ok': False, 'error': 'That test is still running.'}), 409
    shutil.rmtree(d)
    return jsonify({'ok': True})


# ----- passive attenuation test (tools/passive_attenuation.py --step ..., one short process per step) -----
PASSIVE_DIR = ROOT / 'report' / 'passive'
PI_SPEAKER = 'bcm2835'                     # the Pi's 3.5 mm jack, for a speaker (the command-line --play option)
passive = {'dir': None, 'busy': False}
passive_lock = threading.Lock()


def passive_session():
    d = passive['dir']
    return (d, read_json(d / 'session.json')) if d and (d / 'session.json').exists() else (None, None)


def passive_step(*args, timeout=60):
    try:
        out = subprocess.run(['nice', '-n', '5', str(PYTHON), str(ROOT / 'tools' / 'passive_attenuation.py'), '--step', *args],
                             cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, 'OPENBLAS_NUM_THREADS': '1'}).stdout
        return json.loads(out.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        return {'ok': False, 'error': f'The step failed: {error}'}


def passive_card_check(dry_run=False):
    """None if the cards can be used, else the reason."""
    if control.pipeline_pid() or control.view()['power'] != 'off':
        return 'Turn Sukoon off first: the test needs both sound cards free.'
    if battery_active()[0]:
        return 'A battery test is running. Stop it first.'
    cards = soundcards.list_cards()
    if dry_run:
        return None if cards else 'No sound card is plugged in.'
    if len(cards) < 2:
        return f'Both sound cards must be plugged in (found {len(cards)}). Plug in the second card, wait 10 s, try again.'
    return None


def passive_public(d, data):
    if not d:
        return None
    takes = data.get('takes', {})
    return {'id': d.name, 'roles': data.get('roles'), 'playback': data.get('playback'), 'dry_run': data.get('dry_run'),
            'done': [k for k in takes if k != 'claps_result'], 'finished': bool(data.get('finished')),
            'levels': {k: v.get('level_db') for k, v in takes.items() if isinstance(v, dict) and 'level_db' in v}}


@app.get('/passive')
def passive_get():
    d, data = passive_session()
    runs = []
    if PASSIVE_DIR.exists():
        for x in sorted((p for p in PASSIVE_DIR.iterdir() if p.is_dir() and STAMP_RE.match(p.name)), reverse=True)[:HISTORY_SHOWN]:
            s = read_json(x / 'summary.json') or {}
            runs.append({'id': x.name, 'bytes': dir_size(x), 'zip': (x / f'passive_{x.name}.zip').exists(),
                         'summary': {k: s.get(k) for k in ('valid_bands', 'bands', 'mean_insertion_loss_db')},
                         'legacy': not (x / 'session.json').exists()})
    cards = soundcards.list_cards()
    return jsonify({'session': passive_public(d, data), 'busy': passive['busy'], 'runs': runs,
                    'cards': [{'usbid': c['usbid'], 'label': c['label'], 'device': c['device']} for c in cards],
                    'saved_roles': ui_state.get('passive_roles'), 'problem': passive_card_check(),
                    'safety': {'start_db': -30, 'max_db': 0, 'step_db': 3}})


@app.post('/passive/begin')
def passive_begin():
    body = request.get_json(silent=True) or {}
    dry = body.get('dry_run') is True
    problem = passive_card_check(dry)
    if problem:
        return jsonify({'ok': False, 'error': problem}), 409
    playback = body.get('playback') if body.get('playback') in ('phone', 'pi') else 'phone'
    cards = soundcards.list_cards()
    saved = ui_state.get('passive_roles') or {}
    ids = [c['usbid'] for c in cards]
    card = {c['usbid']: c for c in cards}
    if dry:          # one card, its two input channels: checks the whole flow and the files, not a real result
        roles = {'outer': {'usbid': ids[0], 'label': card[ids[0]]['label'], 'channel': 0},
                 'inner': {'usbid': ids[0], 'label': card[ids[0]]['label'], 'channel': 1}}
    else:
        outer = saved.get('outer') if saved.get('outer') in ids else ids[0]
        inner = saved.get('inner') if saved.get('inner') in ids and saved.get('inner') != outer else next(i for i in ids if i != outer)
        roles = {'outer': {'usbid': outer, 'label': card[outer]['label'], 'channel': 0},
                 'inner': {'usbid': inner, 'label': card[inner]['label'], 'channel': 0}}
    mic_levels = {}
    for c in cards:                                   # each card at its saved level (default 80%)
        r = soundcards.set_mic_level(c['index'], ui_state['mic_levels'].get(c['usbid'], soundcards.MIC_DEFAULT_PCT))
        mic_levels[c['usbid']] = r.get('pct')
    d = PASSIVE_DIR / time.strftime('%Y%m%d_%H%M%S')
    d.mkdir(parents=True, exist_ok=True)
    data = {'roles': roles, 'playback': playback, 'dry_run': dry,
            'cards_detected': [{'label': c['label'], 'usbid': c['usbid'], 'name': c['name']} for c in cards], 'notes': clean_text(body.get('notes'), 500),
            'takes': {}, 'start_time': time.strftime('%Y-%m-%d %H:%M:%S'), 'mic_levels': mic_levels}
    (d / 'session.json').write_text(json.dumps(data, indent=1))
    if (ROOT / 'pink_noise.wav').exists():
        shutil.copy(ROOT / 'pink_noise.wav', d / 'pink_noise.wav')
    passive['dir'] = d
    return jsonify({'ok': True, 'session': passive_public(d, data)})


@app.post('/passive/roles')
def passive_roles():
    body = request.get_json(silent=True) or {}
    d, data = passive_session()
    if not d:
        return jsonify({'ok': False, 'error': 'Start the passive test first.'}), 409
    if data.get('dry_run'):
        return jsonify({'ok': False, 'error': 'The dry run uses one card; roles are fixed.'}), 409
    cards = {c['usbid']: c for c in soundcards.list_cards()}
    result = {}
    if body.get('detect') is True:
        if passive['busy']:
            return jsonify({'ok': False, 'error': 'A step is already running.'}), 409
        passive['busy'] = True
        set_recording('passive', 'Passive test: finding the outer mic')
        try:
            result = passive_step('roles', '--seconds', '4', timeout=30)
        finally:
            passive['busy'] = False
            set_recording('passive', None)
        if not result.get('ok'):
            return jsonify({'ok': False, 'error': result.get('error') or 'Role detection failed.', 'scores': result.get('scores')}), 409
        outer = result['outer']
        inner = next((u for u in cards if u != outer), None)
    else:
        outer, inner = body.get('outer'), body.get('inner')
    if outer not in cards or inner not in cards or outer == inner:
        return jsonify({'ok': False, 'error': 'Choose two different plugged-in cards.'}), 400
    data['roles'] = {'outer': {'usbid': outer, 'label': cards[outer]['label'], 'channel': 0},
                     'inner': {'usbid': inner, 'label': cards[inner]['label'], 'channel': 0}}
    (d / 'session.json').write_text(json.dumps(data, indent=1))
    ui_state['passive_roles'] = {'outer': outer, 'inner': inner}
    save_ui_state()
    return jsonify({'ok': True, 'session': passive_public(d, data), 'scores': result.get('scores')})


@app.post('/passive/record')
def passive_record():
    body = request.get_json(silent=True) or {}
    name = body.get('name')
    if name not in ('open_silence', 'probe', 'open', 'worn_silence', 'worn', 'claps'):
        return jsonify({'ok': False, 'error': 'Unknown step.'}), 400
    d, data = passive_session()
    if not d:
        return jsonify({'ok': False, 'error': 'Start the passive test first.'}), 409
    problem = passive_card_check(data.get('dry_run'))
    if problem:
        return jsonify({'ok': False, 'error': problem}), 409
    with passive_lock:
        if passive['busy']:
            return jsonify({'ok': False, 'error': 'A step is already running.'}), 409
        passive['busy'] = True
    args = ['record', '--session', str(d), '--name', name]
    level = body.get('level_db')
    if isinstance(level, (int, float)) and not isinstance(level, bool):
        args += ['--level', str(max(-60.0, min(0.0, float(level))))]     # hard cap: the file's own level
        if data.get('playback') == 'pi':
            args += ['--play', PI_SPEAKER]
    set_recording('passive', 'Passive test')
    try:
        result = passive_step(*args, timeout=45)
    finally:
        passive['busy'] = False
        set_recording('passive', None)
    return jsonify(result), (200 if result.get('ok') or result.get('clipped') or 'loud_enough' in result else 409)


@app.post('/passive/finish')
def passive_finish():
    body = request.get_json(silent=True) or {}
    d, data = passive_session()
    if not d:
        return jsonify({'ok': False, 'error': 'Start the passive test first.'}), 409
    args = ['finish', '--session', str(d)] + ([] if body.get('save_recordings', True) is not False else ['--no-recordings'])
    result = passive_step(*args, timeout=120)
    if result.get('ok'):
        passive['dir'] = None
    return jsonify(result), (200 if result.get('ok') else 409)


@app.post('/passive/cancel')
def passive_cancel():
    d, data = passive_session()
    passive['dir'] = None
    if d and not (d / 'summary.json').exists():
        shutil.rmtree(d, ignore_errors=True)            # nothing usable was produced
    return jsonify({'ok': True})


@app.get('/passive/pink_noise.wav')
def passive_pink():
    """The same pink noise file the command-line test writes, for the phone to play."""
    return send_file(ROOT / 'pink_noise.wav', mimetype='audio/wav', max_age=0)


@app.get('/passive/<run_id>/download')
def passive_download(run_id):
    d = run_dir(PASSIVE_DIR, run_id)
    z = d / f'passive_{run_id}.zip' if d else None
    if d and not (z and z.exists()):                    # older command-line runs: zip them on request
        files = [f for f in sorted(d.iterdir()) if f.is_file() and f.suffix in ('.csv', '.png', '.json', '.txt', '.wav')]
        if files:
            import zipfile
            with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, f'passive_{run_id}/{f.name}')
    if not z or not z.exists():
        return jsonify({'ok': False, 'error': 'No files for that run.'}), 404
    return send_download(z, z.name, 'application/zip')


@app.delete('/passive/<run_id>')
def passive_delete(run_id):
    d = run_dir(PASSIVE_DIR, run_id)
    if not d:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    if passive['dir'] == d:
        passive['dir'] = None
    shutil.rmtree(d)
    return jsonify({'ok': True})


def load_presets():
    try:
        presets = json.loads(PRESETS.read_text())
        return presets if isinstance(presets, dict) else {}
    except (OSError, ValueError):
        return {}


@app.get('/presets')
def get_presets():
    return jsonify(load_presets())


@app.post('/presets')
def save_preset():
    body = request.get_json(silent=True) or {}
    name = str(body.get('name', '')).strip()[:40]
    values = validate(body.get('settings'))
    values = {k: v for k, v in values.items() if k in PRESET_KEYS}
    if not name or not values:
        return jsonify({'ok': False, 'error': 'A preset needs a name and settings.'}), 400
    presets = load_presets()
    presets[name] = values
    tmp = PRESETS.with_suffix('.tmp')
    tmp.write_text(json.dumps(presets, indent=2))
    tmp.replace(PRESETS)
    return jsonify({'ok': True, 'presets': presets})


@app.delete('/presets/<name>')
def delete_preset(name):
    presets = load_presets()
    presets.pop(name, None)
    tmp = PRESETS.with_suffix('.tmp')
    tmp.write_text(json.dumps(presets, indent=2))
    tmp.replace(PRESETS)
    return jsonify({'ok': True, 'presets': presets})


if __name__ == '__main__':
    threading.Thread(target=listen_metrics, daemon=True).start()
    threading.Thread(target=sample_system, daemon=True).start()
    threading.Thread(target=control.run, daemon=True).start()
    threading.Thread(target=mark_interrupted_runs, daemon=True).start()   # power lost during a battery test
    threading.Thread(target=battery_graphs_when_off, daemon=True).start()
    app.run(host='0.0.0.0', port=HTTP_PORT, threaded=True)
