"""Find Sukoon's USB sound cards by USB vendor:product id (or ALSA name), pick the active one, read and set its mic level.

Shared by dashboard/server.py, dashboard/audio_check.py, tools/passive_attenuation.py and tools/battery_log.py.
Card numbers (hw:N) change between boots and plug orders, so cards are identified by /proc/asound/cardN/usbid and
the PortAudio device is then named by the current number ('hw:N,0' is a substring of PortAudio's device name).

Selection (select()): known cards only (KNOWN, matched by USB id, else by ALSA card name), each must offer capture
and playback at 48000 Hz. Order: the override in dashboard/soundcard.json (optional), else the dashboard's saved
choice, else the fixed priority. The ALSA index is never used to choose; it is only how the chosen card is addressed.
Both of our cards report the same USB serial number, so the serial cannot tell them apart; USB id and name can.

  .venv/bin/python dashboard/soundcards.py          list the cards found, the one that would be active, mic levels
"""
import json
import re
import subprocess
from pathlib import Path

# Supported cards. priority: lower wins when several are plugged in and no choice was saved.
# The AB13X is the reference card (headset mic verified). names: ALSA card names (/proc/asound/cards)
# accepted when the USB id is unknown; the 1620:0b21 card has none, because 'USB Audio' is a generic name
# other cards use too.
KNOWN = {
    '0020:0b21': {'label': 'AB13X USB Audio', 'priority': 1, 'names': ('AB13X USB Audio',),
                  'note': 'Headset mic works. Cuts audio above about 7.5 to 8 kHz; mic is dark (-11 to -18 dB at '
                          '3 to 4 kHz); no jack sensing.'},
    '1620:0b21': {'label': 'USB Audio (1620:0b21)', 'priority': 2, 'names': (),
                  'note': 'Alternative card. Cuts audio above about 8 kHz. Mic quality not yet verified.'},
}
SAMPLE_RATE = 48000             # sukoon_engine.py runs at 48 kHz only (RNNoise)
CONFIG_PATH = Path(__file__).resolve().parent / 'soundcard.json'   # optional: {"override": "<usb id or ALSA name>"}
MIC_DEFAULT_PCT = 80            # today's level on both cards; never changed without asking the user
MIC_MIN_PCT = 50                # AB13X: 50% = -30 dB; lower puts quiet speech near the mic's own hiss
MIC_MAX_PCT = 100               # card maximum (0 dB); more gain cannot damage anything, it only clips sooner


def _stream_info(index):
    """{'playback': {'rates': [...], 'channels': [...]}, 'capture': {...}} from /proc/asound/cardN/stream0."""
    info = {'playback': {'rates': [], 'channels': []}, 'capture': {'rates': [], 'channels': []}}
    try:
        text = Path(f'/proc/asound/card{index}/stream0').read_text()
    except OSError:
        return info
    for part in re.split(r'\n(?=Playback:|Capture:)', text):
        kind = 'playback' if part.startswith('Playback:') else 'capture' if part.startswith('Capture:') else None
        if not kind:
            continue
        for ch in re.findall(r'Channels:\s*(\d+)', part):
            info[kind]['channels'].append(int(ch))
        for rates in re.findall(r'Rates:\s*([^\n]+)', part):
            for r in re.findall(r'\d+', rates.replace('continuous', '')):
                info[kind]['rates'].append(int(r))
        m = re.search(r'Rates:\s*(\d+)\s*-\s*(\d+)\s*\(continuous\)', part)
        if m and int(m.group(1)) <= SAMPLE_RATE <= int(m.group(2)):
            info[kind]['rates'].append(SAMPLE_RATE)
    for kind in info:
        for key in info[kind]:
            info[kind][key] = sorted(set(info[kind][key]))
    return info


def _port_path(index):
    """USB port path of the card, e.g. '1-1.4' (bus 1, hub port 1, port 4), or '' if not USB."""
    try:
        dev = Path(f'/sys/class/sound/card{index}/device').resolve()
    except OSError:
        return ''
    m = re.match(r'(\d+-[\d.]+)(?::|$)', dev.name)
    return m.group(1) if m else dev.parent.name


def _match_known(usbid, long_name):
    if usbid in KNOWN:
        return usbid, 'USB id'
    for key, info in KNOWN.items():
        if long_name in info.get('names', ()):
            return key, 'ALSA name'
    return None, ''


def list_cards():
    """USB audio cards present now: [{index, usbid, id, name, device, label, known, priority, port, streams,
    rate_ok, matched_by}], by priority."""
    cards = []
    names = {}
    try:
        for line in Path('/proc/asound/cards').read_text().splitlines():
            m = re.match(r'\s*(\d+)\s+\[(\S+)\s*\]:\s*(\S+)\s+-\s+(.*)', line)
            if m:
                names[int(m.group(1))] = (m.group(2), m.group(3), m.group(4).strip())
    except OSError:
        return []
    for index, (short, driver, long_name) in sorted(names.items()):
        if driver != 'USB-Audio':
            continue
        try:
            usbid = Path(f'/proc/asound/card{index}/usbid').read_text().strip().lower()
        except OSError:
            usbid = 'unknown'
        key, matched_by = _match_known(usbid, long_name)
        info = KNOWN.get(key, {})
        streams = _stream_info(index)
        rate_ok = all(SAMPLE_RATE in streams[k]['rates'] and streams[k]['channels'] for k in ('capture', 'playback'))
        cards.append({'index': index, 'usbid': usbid, 'id': short, 'name': long_name, 'device': f'hw:{index},0',
                      'label': info.get('label', long_name), 'known': bool(info), 'note': info.get('note', ''),
                      'priority': info.get('priority', 99), 'known_key': key, 'matched_by': matched_by,
                      'port': _port_path(index), 'streams': streams, 'rate_ok': rate_ok})
    cards.sort(key=lambda c: (c['priority'], c['index']))
    return cards


def config_override():
    """The 'override' value in dashboard/soundcard.json (USB id or ALSA card name), or None."""
    try:
        value = json.loads(CONFIG_PATH.read_text()).get('override')
    except (OSError, ValueError, AttributeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def usable(cards):
    """Known cards that do capture and playback at 48 kHz."""
    return [c for c in cards if c['known'] and c['rate_ok']]


def _matches(card, wanted):
    wanted = wanted.lower()
    return wanted in (card['usbid'], (card['known_key'] or '').lower(), card['name'].lower(), card['id'].lower())


def choose(cards, saved=None):
    """(card, reason) among usable cards: the config override, else the saved choice, else the highest priority.
    None (with the reason) if no known card can run at 48 kHz."""
    good = usable(cards)
    if not good:
        if not cards:
            return None, 'No USB sound card found.'
        found = '; '.join(f'{c["name"]} ({c["usbid"]}, port {c["port"] or "?"}): '
                          f'{"not a known card" if not c["known"] else "no capture and playback at 48 kHz"}'
                          for c in cards)
        return None, f'No known sound card can run Sukoon. Found: {found}.'
    notes = []
    override = config_override()
    if override:
        for c in good:
            if _matches(c, override):
                return c, f'Override in {CONFIG_PATH.name} ({override}).'
        notes.append(f'Override {override!r} in {CONFIG_PATH.name} is not plugged in or not usable, so it was ignored.')
    for c in good:
        if saved and c['usbid'] == saved:
            return c, ' '.join(['Saved choice.' if len(good) > 1 else 'Only card plugged in.', *notes])
    if len(good) == 1:
        reason = 'Only usable card plugged in.' if len(cards) > 1 else 'Only card plugged in.'
    else:
        others = ', '.join(c['label'] for c in good[1:])
        reason = f'{len(good)} cards plugged in; using {good[0]["label"]} by priority (also found: {others}).'
    skipped = [c for c in cards if c not in good]
    if skipped:
        notes.append('Skipped: ' + ', '.join(f'{c["name"]} ({c["usbid"]})' for c in skipped) + '.')
    return good[0], ' '.join([reason, *notes])


def throttled():
    """'throttled=0x0' from vcgencmd get_throttled, or the error."""
    try:
        r = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=3)
        return (r.stdout or r.stderr).strip() or f'vcgencmd exit {r.returncode}'
    except (OSError, subprocess.SubprocessError) as error:
        return f'vcgencmd failed: {error}'


def start_log_line(card, reason, cards):
    """One line for the journal and logs/card_select.log at every service start."""
    found = ', '.join(f'{c["name"]} {c["usbid"]} port {c["port"] or "?"} '
                      f'{"ok" if c in usable(cards) else "unusable"}' for c in cards) or 'none'
    if card:
        chosen = (f'chosen={card["label"]} usbid={card["usbid"]} alsa_name="{card["name"]}" alsa_id={card["id"]} '
                  f'device={card["device"]} usb_port={card["port"]} matched_by={card["matched_by"]}')
    else:
        chosen = 'chosen=NONE (audio will not start)'
    return f'Sukoon card select: {chosen} | {throttled()} | reason: {reason} | found: {found}'


def find(usbid):
    return next((c for c in list_cards() if c['usbid'] == usbid), None)


def _amixer(index, *args, timeout=3):
    return subprocess.run(['amixer', '-c', str(index), *args], capture_output=True, text=True, timeout=timeout)


def capture_control(index):
    """Name of the card's mic capture volume control ('Mic' on both cards), or None."""
    try:
        out = _amixer(index, 'scontents').stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = []
    for block in re.split(r"(?=Simple mixer control ')", out):
        m = re.match(r"Simple mixer control '([^']+)',(\d+)", block)
        if m and 'cvolume' in block and 'Capture' in block:
            found.append(m.group(1))
    for preferred in ('Mic', 'Capture'):
        if preferred in found:
            return preferred
    return found[0] if found else None


def get_mic_level(index):
    """{'pct', 'db', 'control'} or {'error'}."""
    control = capture_control(index)
    if not control:
        return {'error': 'This card has no mic level control.'}
    try:
        out = _amixer(index, 'sget', control).stdout
    except (OSError, subprocess.SubprocessError) as error:
        return {'error': f'amixer failed: {error}'}
    m = re.search(r'Capture \d+ \[(\d+)%\](?: \[(-?[\d.]+)dB\])?', out)
    if not m:
        return {'error': 'Could not read the mic level.'}
    return {'pct': int(m.group(1)), 'db': float(m.group(2)) if m.group(2) else None, 'control': control}


def set_mic_level(index, pct):
    """Clamp to the safe range, set it and switch capture on. Returns get_mic_level() after the change."""
    pct = int(round(min(max(float(pct), MIC_MIN_PCT), MIC_MAX_PCT)))
    control = capture_control(index)
    if not control:
        return {'error': 'This card has no mic level control.'}
    try:
        _amixer(index, '-q', 'sset', control, f'{pct}%', 'cap')
    except (OSError, subprocess.SubprocessError) as error:
        return {'error': f'amixer failed: {error}'}
    return get_mic_level(index)


if __name__ == '__main__':
    found = list_cards()
    active, why = choose(found)
    for c in found:
        print(json.dumps({**c, 'mic': get_mic_level(c['index'])}))
    print('active:', active and active['label'], '|', why)
    print(start_log_line(active, why, found))
