"""Guided passive attenuation test, one step per call (run by the dashboard; see passive_attenuation.py).

Uses passive_attenuation.py's own pink noise, recording, 1/3-octave band levels and plots, so the method and the
numbers are the same as the command-line test. Session state lives in DIR/session.json:
  roles      {'outer': {usbid, label, channel}, 'inner': {...}}
  takes      {name: {'outer': bands, 'inner': bands, 'clip_pct': {...}, 'rms_dbfs': {...}, 'level_db': ...}}
Files are named as the command-line test names them (open_mic0.wav = outer, open_mic1.wav = inner, ...), so
report/passive/<time>/ stays compatible; insertion_loss.csv, summary.json, README.txt and a zip are added.
"""
import csv, json, subprocess, sys, time, zipfile
from pathlib import Path

import numpy as np
import scipy.io.wavfile as wav

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'dashboard'))
import passive_attenuation as pa
import soundcards

ROOT = pa.ROOT
SECONDS = {'open_silence': 3.5, 'worn_silence': 3.5, 'probe': 2.5, 'open': 8.5, 'worn': 8.5, 'claps': 15.5}
CLIP_LIMIT_PCT = 0.01             # more than 0.01% of samples at full scale: the take is clipped
TARGET_MARGIN_DB = 12.0           # ramp until the noise is this far above the silence take ...
TARGET_SHARE = 0.75               # ... in this share of the bands from 100 Hz to 8 kHz (the cards cut above 8 kHz)
MAX_LEVEL_DB = 0.0                # hard cap: the pink noise file's own level (-12 dBFS RMS), never louder
START_LEVEL_DB = -30.0
VALID_MARGIN_DB = pa.MIN_SNR_DB   # 10 dB, as the command-line test
RAMP_BANDS = np.array(pa.BANDS) <= 8000


def say(obj):
    print(json.dumps(obj))
    sys.exit(0)


def load(session):
    return json.loads((session / 'session.json').read_text())


def save(session, data):
    tmp = session / 'session.json.tmp'
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(session / 'session.json')


def device_of(role):
    card = soundcards.find(role['usbid'])
    if card is None:
        say({'ok': False, 'error': f"{role['label']} ({role['usbid']}) is not plugged in."})
    return card['device']


def step_roles(seconds):
    """Record every USB card at once; the card with the strongest tap (peak over its median) is the outer mic."""
    cards = soundcards.list_cards()
    if len(cards) < 2:
        say({'ok': False, 'error': 'Two USB sound cards are needed for role detection.', 'cards': cards})
    takes = pa.record([(c['device'], 0) for c in cards], seconds)
    scores = []
    for c, x in zip(cards, takes):
        hop = 480
        env = np.abs(x[:len(x) // hop * hop]).reshape(-1, hop).max(axis=1) + 1e-9
        scores.append(float(20 * np.log10(env.max() / np.median(env))))
    k = int(np.argmax(scores))
    other = sorted(scores)[-2] if len(scores) > 1 else -99
    clear = scores[k] - other >= 6.0 and scores[k] >= 15.0
    say({'ok': clear, 'outer': cards[k]['usbid'], 'scores': {c['usbid']: round(v, 1) for c, v in zip(cards, scores)},
         'error': None if clear else 'No clear tap found. Tap the outer mic a few times firmly during the 4 seconds, '
                                     'or choose the roles by hand.'})


def step_record(session, name, play, level_db):
    data = load(session)
    roles = data['roles']
    mics = [(device_of(roles['outer']), roles['outer'].get('channel', 0)),
            (device_of(roles['inner']), roles['inner'].get('channel', 0))]
    seconds = SECONDS[name]
    stimulus = None
    if play and name in ('probe', 'open', 'worn'):
        level_db = min(MAX_LEVEL_DB, float(level_db))
        stimulus = pa.pink_noise(seconds + 2.0) * 10 ** (level_db / 20)
    outer, inner = pa.record(mics, seconds, stimulus, play if stimulus is not None else None)
    clip = {'outer': round(float(np.mean(np.abs(outer) > 0.99) * 100), 3),
            'inner': round(float(np.mean(np.abs(inner) > 0.99) * 100), 3)}
    rms = {'outer': round(float(20 * np.log10(np.sqrt(np.mean(outer ** 2)) + 1e-12)), 1),
           'inner': round(float(20 * np.log10(np.sqrt(np.mean(inner ** 2)) + 1e-12)), 1)}
    pcm = lambda x: (np.clip(x, -1, 1) * 32767).astype(np.int16)
    if name == 'claps':
        wav.write(session / 'claps_outer.wav', pa.FS, pcm(outer)); wav.write(session / 'claps_inner.wav', pa.FS, pcm(inner))
    else:
        cond, suffix = (name.split('_')[0], '_silence') if name.endswith('_silence') else (name, '')
        wav.write(session / f'{cond}_mic0{suffix}.wav', pa.FS, pcm(outer))
        wav.write(session / f'{cond}_mic1{suffix}.wav', pa.FS, pcm(inner))
    take = {'outer': pa.band_levels(outer).round(2).tolist(), 'inner': pa.band_levels(inner).round(2).tolist(),
            'clip_pct': clip, 'rms_dbfs': rms, 'level_db': level_db, 'time': time.time(), 'played_by_pi': stimulus is not None}
    result = {'ok': True, 'name': name, 'clip_pct': clip, 'rms_dbfs': rms}
    clipped = max(clip.values()) > CLIP_LIMIT_PCT
    if clipped:
        result.update(ok=False, clipped=True, error=('A clap clipped the microphone. Clap more softly or further away '
                                                     'and record the claps again.') if name == 'claps' else
                      'The microphone clipped (hit its limit). Turn the speaker down (or lower the mic level) and '
                      'record this step again.')
    if name == 'claps':
        result['claps'] = clap_peaks(outer, inner, data)
    silence = data['takes'].get('worn_silence' if name == 'worn' else 'open_silence')
    if silence and name in ('probe', 'open', 'worn'):
        m = {r: (np.array(take[r]) - np.array(silence[r])) for r in ('outer', 'inner')}
        share = {r: round(float(np.mean(m[r][RAMP_BANDS] >= TARGET_MARGIN_DB)), 2) for r in m}
        result.update(margin_db={r: m[r].round(1).tolist() for r in m}, share_over_target=share,
                      loud_enough=min(share.values()) >= TARGET_SHARE,
                      valid_bands=int(np.sum((m['outer'] >= VALID_MARGIN_DB) & (m['inner'] >= VALID_MARGIN_DB))))
    if not clipped or name == 'probe':
        data['takes'][name] = take
        if name == 'claps':
            data['takes']['claps_result'] = result['claps']
        save(session, data)
    say(result)


def clap_peaks(outer, inner, data):
    hop = pa.FS // 100
    env = lambda s: 20 * np.log10(np.abs(s[:len(s) // hop * hop]).reshape(-1, hop).max(axis=1) + 1e-9)
    eo, ei = env(outer), env(inner)
    peaks, k, floor = [], 0, np.median(eo)
    while k < len(eo):
        if eo[k] > floor + 20:
            seg = slice(max(k - 2, 0), k + 10)
            peaks.append([round(k / 100, 2), round(float(eo[seg].max()), 1), round(float(ei[seg].max()), 1)])
            k += 50
        else:
            k += 1
    o, i = data['takes'].get('open'), None
    cal = float(np.mean(np.array(o['outer']) - np.array(o['inner']))) if o else 0.0
    return {'peaks': peaks, 'cal_db': round(cal, 1), 'reduction_db': [round(a - b - cal, 1) for _, a, b in peaks]}


def commit():
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        return 'unknown'


def step_finish(session, keep_recordings):
    data = load(session)
    t = data['takes']
    missing = [n for n in ('open_silence', 'open', 'worn_silence', 'worn') if n not in t]
    if missing:
        say({'ok': False, 'error': 'Steps not done yet: ' + ', '.join(missing)})
    g = lambda n, r: np.array(t[n][r])
    o_open, i_open, o_worn, i_worn = g('open', 'outer'), g('open', 'inner'), g('worn', 'outer'), g('worn', 'inner')
    oq_open, iq_open, oq_worn, iq_worn = (g('open_silence', 'outer'), g('open_silence', 'inner'),
                                          g('worn_silence', 'outer'), g('worn_silence', 'inner'))
    margins = {'outer_open': o_open - oq_open, 'inner_open': i_open - iq_open,
               'outer_worn': o_worn - oq_worn, 'inner_worn': i_worn - iq_worn}
    valid = np.all([m >= VALID_MARGIN_DB for m in margins.values()], axis=0)
    loss = (o_worn - i_worn) - (o_open - i_open)                     # same formula as the command-line test
    # files the command-line test writes (compatible)
    levels = {'Outer mic (worn)': o_worn, 'Inner mic (worn)': i_worn, 'Inner mic silence': iq_worn}
    with open(session / 'attenuation.csv', 'w', newline='') as fh:
        wr = csv.writer(fh)
        wr.writerow(['band_hz', 'passive_attenuation_db', 'reliable'] + [f'{k} (dB)' for k in levels])
        for j, fc in enumerate(pa.BANDS):
            wr.writerow([fc, round(float(loss[j]), 1), bool(valid[j])] + [round(float(v[j]), 1) for v in levels.values()])
    claps = t.get('claps_result')
    peaks = [(p[0], p[1], p[2]) for p in claps['peaks']] if claps else []
    pa.plot(session, loss, valid, levels, 'two microphones, insertion loss', peaks, claps['cal_db'] if claps else 0.0)
    with open(session / 'insertion_loss.csv', 'w', newline='') as fh:
        wr = csv.writer(fh)
        wr.writerow(['band_hz', 'outer_silence_open_db', 'inner_silence_open_db', 'outer_open_db', 'inner_open_db',
                     'outer_silence_worn_db', 'inner_silence_worn_db', 'outer_worn_db', 'inner_worn_db',
                     'min_margin_db', 'valid', 'insertion_loss_db'])
        for j, fc in enumerate(pa.BANDS):
            mm = min(float(m[j]) for m in margins.values())
            wr.writerow([fc] + [round(float(v[j]), 1) for v in (oq_open, iq_open, o_open, i_open, oq_worn, iq_worn,
                                                                  o_worn, i_worn)]
                        + [round(mm, 1), int(bool(valid[j])), round(float(loss[j]), 1) if valid[j] else ''])
    good = loss[valid]
    speech = valid & (np.array(pa.BANDS) >= 300) & (np.array(pa.BANDS) <= 4000)
    summary = {'test': 'passive', 'run': session.name, 'start_time': data.get('start_time'),
               'cards_and_roles': data['roles'], 'cards_detected': data.get('cards_detected'), 'mic_levels_pct': data.get('mic_levels'),
               'playback': data.get('playback'), 'levels_used_db_re_file': {n: t[n].get('level_db') for n in ('open', 'worn')},
               'pink_noise_rms_dbfs_at_0_db': -12.0, 'valid_bands': int(valid.sum()), 'bands': len(pa.BANDS),
               'mean_insertion_loss_db': round(float(good.mean()), 1) if len(good) else None,
               'speech_range_300_4000_db': round(float(loss[speech].mean()), 1) if speech.any() else None,
               'claps': claps, 'commit': commit(), 'notes': data.get('notes', ''), 'recordings_in_zip': keep_recordings,
               'valid_rule': f'a band is valid when the pink noise is at least {VALID_MARGIN_DB:.0f} dB above the silence '
                             'take on both mics in both conditions'}
    (session / 'summary.json').write_text(json.dumps(summary, indent=1))
    (session / 'README.txt').write_text(f"""Sukoon passive attenuation test {session.name}

How much the earbuds block on their own (Sukoon off). Two microphones: OPEN = both side by side at the ear,
no earbuds (calibrates the mics against each other); WORN = inner mic under the earbud, outer just outside.
Insertion loss per band = (outer - inner) worn - (outer - inner) open, in dB. Positive = quieter inside.
insertion_loss.csv  per 1/3-octave band: silence, open and worn levels for both mics (dB re full scale), the
                    smallest margin of noise over silence, valid (1/0) and the insertion loss (blank if invalid).
attenuation.csv     the same result in the command-line test's format.
frequency_response.png  insertion loss; bands below the noise floor are drawn as open grey circles.
levels.png, claps.png   band levels; clap peak reduction if claps were recorded.
summary.json        cards and roles, mic levels, playback levels used, valid bands, averages, commit, notes.
*.wav               the recordings (mic0 = outer, mic1 = inner), so the result can be checked.
Pink noise level: 0 dB = the file's own level (-12 dBFS RMS); the ramp started at {START_LEVEL_DB:.0f} dB and never went above it.
""")
    for f in session.glob('probe_mic*.wav'):
        f.unlink()
    names = [f for f in sorted(session.iterdir()) if f.is_file() and f.suffix in ('.csv', '.png', '.json', '.txt', '.wav')
             and f.name not in ('session.json',) and (keep_recordings or f.suffix != '.wav')]
    zpath = session / f'passive_{session.name}.zip'
    with zipfile.ZipFile(zpath.with_suffix('.ztmp'), 'w', zipfile.ZIP_DEFLATED) as z:
        for f in names:
            z.write(f, f'passive_{session.name}/{f.name}')
    zpath.with_suffix('.ztmp').replace(zpath)
    data['finished'] = time.time()
    save(session, data)
    say({'ok': True, 'valid': valid.tolist(), 'loss_db': loss.round(1).tolist(), 'bands': pa.BANDS,
         'summary': summary, 'zip': zpath.name})


def main(argv):
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--step', required=True, choices=('roles', 'record', 'finish'))
    p.add_argument('--session'); p.add_argument('--name', choices=tuple(SECONDS))
    p.add_argument('--play'); p.add_argument('--level', type=float, default=START_LEVEL_DB)
    p.add_argument('--seconds', type=float, default=4.0)
    p.add_argument('--no-recordings', action='store_true')
    a = p.parse_args(argv)
    try:
        if a.step == 'roles':
            step_roles(a.seconds)
        session = Path(a.session).resolve()
        if a.step == 'record':
            step_record(session, a.name, a.play, a.level)
        else:
            step_finish(session, not a.no_recordings)
    except SystemExit:
        raise
    except Exception as error:                   # a device error is a message for the page, not a traceback
        say({'ok': False, 'error': f'{type(error).__name__}: {error}'})
