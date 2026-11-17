# SafeSound dashboard reference

The SafeSound dashboard is a single page, `dashboard/static/index.html`, served by `dashboard/server.py` (Flask) on port 5000. There is no build step and no external file: fonts are bundled, so it works offline on the headset's own Wi-Fi hotspot. Open it at `http://sukoon.local:5000` (or `http://10.42.0.1:5000`) from an iPhone, iPad or laptop.

## Layout

App bar (brand, connection badge, Recording badge, colour-mode button) and four tabs. Phones get a bottom tab bar; from 760 px wide the tabs sit in the app bar.

| Tab | Sections, in order |
|---|---|
| Home | Protection (state, run time, Turn on/off, test version chip, sound card and USB port, **System check**: sound card, microphone signal, optional headphone check) · Bangs and alarms · Levels (In and Out, 10 s chart, review of the last run while off) · Frequency (Live / Last bang / 3 min, average attenuation) · Transients log (every bang and alarm with time and reduction) · Test version (5 by default) |
| Sound | Speech and room · Bangs · Alarms · Tone · Microphone · Compare · Presets · Reset. Then, under "More control": Equalizer (5 bands) and Expert (raw settings) |
| Tests | Timed test (1 to 5 minutes) · Saved tests · Recordings (play a WAV on the headset or into Sukoon's input). Under "Long measurements": Advanced (battery test, passive attenuation test) |
| System | Sound card (active card, USB port, USB id, cards detected) · Raspberry Pi (power, CPU, temperature, clock, uptime) · Latency · Processing · Attenuation · Storage |

Rules the page follows:

- Sound settings can change only while Sukoon is on (they live in the audio process). The mic level works while off too.
- Checks and the latency measurement run only while Sukoon is off (they need the sound card).
- Turn on needs the sound card and the microphone check, the same rule as the automatic start at power-up. The headphone check is optional.
- When the Pi cannot be reached, a red banner shows, the last values stay on screen and controls are disabled. The page reconnects by itself.
- The Recording badge shows while audio is being saved (timed test, passive recording, battery clips).
- Colour mode: automatic (follows the device), light or dark, saved in the browser.

## Test versions

| Version | Processing |
|---|---|
| 1 | Simple speech rule: any frame RNNoise rates 0.3 or more opens the ambient path for 1 s; speech boost up to +18 dB |
| 2 | Sustained voice only (40 ms, 14 dB above the room); no boost after 5 s without speech |
| 3 | Test 2 plus the alarm guard: sirens and other long loud sounds stay turned down |
| 4 | Test 3 plus a safety floor: alarms are never below an audible level (−26 dBFS); faint alarms are raised up to +10 dB |
| **5** | **Test 4 plus bangs caught during speech: the whole sound is turned down for just the bang (30 dB), so speech around it stays clear. Default and recommended** |

## Controls (Sound tab)

| Control | Setting key | Range · default | What it does |
|---|---|---|---|
| Speech volume | `TARGET_SPEECH_DBFS` | −30 to −15 dBFS · −23 | How loud speech is in the ears |
| Room sound during speech | `FLOOR_MIN_DB` and `FLOOR_MAX_DB` together | keeps the gap between the two | How much of the room is mixed in while someone talks |
| Bang sensitivity | `POP_JUMP_DB` and `BANG_OVER_SPEECH_DB` together | Low 26/18 · Normal 20/15 (defaults) · High 15/10 | How easily a sudden sound counts as a bang. Shows "Custom" when the raw values under Expert match none of the three |
| How much bangs are turned down | `BANG_DUCK_DB` | 10 to 50 dB · 30 | Reduction of a bang that arrives during speech (Test 5) |
| Alarm reduction | `SUSTAIN_DUCK_DB` | 6 to 30 dB · 20 | Sirens are played quieter, never removed |
| Tone | `EQ_100` … `EQ_5000` | Flat · Clearer · Extra clear | Equalizer presets |
| Mic level | `mic_level` (applied by the server with amixer) | 50 to 100% · 80 | Sound card capture gain, saved per card |
| Compare: hear the raw mic | `bypass` | off | Unprocessed microphone with the same delay. No protection while on; a warning chip shows on Home |
| Presets, Reset | | | Save, apply, delete named settings; reset everything to defaults (two taps, with Undo) |
| Equalizer | `EQ_100`, `EQ_400`, `EQ_1000`, `EQ_2500`, `EQ_5000` | −12 to +12 dB · 0 | Five bands |
| Expert | `pop_guard_enabled`, `FLOOR_MIN_DB`, `FLOOR_MAX_DB`, `POP_JUMP_DB`, `BANG_OVER_SPEECH_DB`, `POP_HOLD_FRAMES`, `SUSTAIN_FADE_S`, `SUSTAIN_STEADY_FRAMES` | see the page | Raw values. Turning Bang guard off needs two taps and shows a warning chip |

## Style

- Tokens are CSS variables on `:root` (dark) and `:root[data-theme="light"]`. One accent (teal) for actions, selection and the OUT series; orange for incoming sharp sound (IN series, bangs); green, red and amber only for status.
- Content sits on solid cards with hairline borders; only the app bar and the phone tab bar are translucent.
- Type: Inter (bundled, SIL OFL). The Arabic wordmark سكون uses a four-letter subset of Noto Kufi Arabic SemiBold (bundled, SIL OFL). The ring in the logo is the sukūn mark, the sign for a resting consonant.
- Touch targets are at least 44 px on touch screens. Reduced motion, reduced transparency and higher contrast settings are respected.

## Backend endpoints the page uses
| Request | Body or format | Notes |
|---|---|---|
| GET `/stream` | SSE: `event: init` with the full snapshot, then `data:` snapshots every 100 ms | 3 errors in a row mean Disconnected; if the stream closes the page falls back to polling |
| GET `/metrics` | Full snapshot JSON | Fallback polling every 250 ms |
| GET `/events` | `{"events": [...]}` newest first, up to 500 | The stream's `events[]` carries only the newest 100 |
| GET `/history` | `{"points": [[t, in_peak, out_peak, bang 0/1, speech 0/1], …]}` at 10 Hz | Review of the last run while Off |
| GET `/presets` | `{name: {settings…}}` | |
| POST `/presets` | `{"name": str ≤40, "settings": {...packet.settings, clip_guard, pop_guard_enabled}}` | Also used by preset undo |
| DELETE `/presets/<name>` | – | |
| POST `/settings` | Any of: `{"bypass"|"clip_guard"|"pop_guard_enabled"|"recording": bool}`, `{KEY: number}` for the slider keys above, `{"mic_level": 50..100}`, `{"reset_defaults": true}` | Server clamps to range and forwards over UDP 127.0.0.1:5006 to src/sukoon_engine.py; `mic_level` is applied by the server (amixer) and not forwarded |
| POST `/mic_clips/reset` | – | Session clipped-samples count starts from 0 |
| POST `/power` | `{"on": bool}` | 409 "Finish the system check first." if not ready |
| POST `/test_version` | `{"version": 1..5}` | Saved in ui_state.json; also sent live as `{"test_version": n}` over UDP while running |
| POST `/latency_mode` | `{"mode": "standard"|"low"}` | Restarts the audio if on. Standard (40 ms buffers) is the default and the measured setting |
| POST `/auto_on` | `{"on": bool}` | Kept for compatibility; always answers `auto_on: true`, changes nothing |
| GET `/cards` | `{cards[], active, reason, choice}` | Cards by USB id |
| POST `/card` | `{"usbid": "vvvv:pppp"|null}` | Saved card choice; 409 unless Off |
| POST `/check/<mic|tone|heard|not_heard|latency>` | No body | 409 unless Off; heard and not_heard need the tone played first |
| POST `/test/start` | `{"minutes": 1..5}` | Timed test (tools/timed_test.py, nice 19 on core 1). 409 if Sukoon is off |
| POST `/test/stop` | – | Stops early; files and zip are still written |
| GET `/tests` | `{runs[], state, disk_free, low_storage}` | Newest 10 in logs/tests/ |
| GET `/tests/<id>/download`, `/tests/<id>/file/<input|output>` | zip / WAV as attachment | |
| DELETE `/tests/<id>` | – | Page asks for a second tap |
| POST `/battery/start` | form fields (bank, start_charge_pct, meter_start_wh, room_temp, enclosure, ambient, stimulus_file, speaker_volume, spl_at_mic, distance, note, max_hours, save_clips, phone_time_ms) | Starts systemd user unit `sukoon-battery` (tools/battery_log.py --run). Refused on a card problem or low voltage now |
| POST `/battery/stop`, `/battery/stimulus` | – / `{"phone_time_ms"}` | Stop; log a Stimulus started event |
| POST `/battery/<id>/notes` | `{"meter_end_wh", "end_notes"}` | After the run; summary and zip are rebuilt |
| GET `/battery`, `/battery/<id>/download`, DELETE `/battery/<id>` | | Also marks runs left running by a power loss as interrupted |
| GET `/passive` | `{session, runs[], cards[], saved_roles, problem, safety}` | Guided passive test state |
| POST `/passive/begin` | `{"playback": "phone"|"pi", "notes", "dry_run"}` | 409 unless Sukoon is Off and both cards are plugged in |
| POST `/passive/roles` | `{"detect": true}` or `{"outer": usbid, "inner": usbid}` | Saved as `passive_roles` |
| POST `/passive/record` | `{"name": "open_silence"|"probe"|"open"|"worn_silence"|"worn"|"claps", "level_db"}` | One recording step; level capped at 0 dB re the pink noise file |
| POST `/passive/finish`, `/passive/cancel` | `{"save_recordings": bool}` | |
| GET `/passive/pink_noise.wav`, `/passive/<id>/download`, DELETE `/passive/<id>` | | |
| GET `/fonts/<name>` | woff2, cached 7 days | |

Snapshot fields the page reads:

- **Top level:** `status`, `now`, `run_seconds`, `estimated_latency_ms`, `events[]`, `totals`, `history`, `run_history_points`, `attenuation{bang, alarm, ambient}`, `spectrum`, `spectrum_live`, `system{cpu_pct, temp_c, clock_mhz, governor, pi_uptime_s, power, storage}`, `control{card{label, usbid, device, note, port}, cards[], card_choice, card_reason, card_missing, power, message, ready, can_start, busy, usb, mic, headphones, latency_result, measuring, test_version, auto_on, latency_mode}`, `recording{active, kind, label}`, `mic{label, usbid, pct, db, min, max, default, clips_session, clips_10s, error}`, `timed_test{running, id, state, elapsed_s, total_s, bangs, out_peak_dbfs}`, `battery{running, id, elapsed_s, rows, clips_saved, clips_skipped, clips_enabled, last_temp_c, last_throttled, sukoon}`, `test_versions[]`, `latency_modes[]`.
- **Packet:** `state`, `settings`, `defaults`, `live`, `adaptive`, `window`, `vad`, `voice_hold`, `pop_triggers`, `alarm_events`, `alarm_active`.

Metrics reach the server from src/sukoon_engine.py over UDP 127.0.0.1:5005.

