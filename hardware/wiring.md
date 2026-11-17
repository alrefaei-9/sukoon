# Wiring and connections

Sukoon uses no GPIO or soldered connections on the Pi. Everything connects through standard USB and 3.5 mm plugs.

```mermaid
flowchart LR
    PB["🔋 10,000 mAh power bank"] -- "USB-C, 5 V" --> PI["Raspberry Pi 4B"]
    MIC["🎤 NEEWER lavalier mic (clip-on)"] -- "3.5 mm" --> MJ["mic jack"]
    subgraph AD["AB13X USB audio adapter"]
        MJ
        HJ["headphone jack"]
    end
    AD -- "USB-A (any port)" --> PI
    HJ -- "3.5 mm stereo, 1.25 m" --> HP["🎧 Ouroboros headphones"]
    PI -. "Wi-Fi hotspot (wlan0)" .-> PH["📱 iPhone / iPad"]
```

```
 ┌──────────────┐  USB-C 5 V   ┌──────────────────────┐   USB-A   ┌───────────────────────┐
 │  Power bank  │─────────────▶│  Raspberry Pi 4B     │◀──────────│  AB13X USB audio      │
 │  10,000 mAh  │              │  (microSD 64 GB)     │           │  adapter              │
 └──────────────┘              │                      │           │   mic jack ◀──────────┼── 3.5 mm ── Microphone
                               │  wlan0 hotspot ))) ──┼── Wi-Fi ─▶│   headphone jack ─────┼── 3.5 mm ──▶ Headphones
                               └──────────────────────┘  iPhone   └───────────────────────┘
```

| From | To | Cable / link | Notes |
|---|---|---|---|
| Power bank | Pi USB-C power input | USB-C | Use a short, thick cable. Under-voltage shows on the dashboard (System › Raspberry Pi) |
| USB audio adapter | Any Pi USB port | Adapter's own USB-A plug | Plug in or out **only with the Pi powered off**: the adapter is detected once at power-up, by USB ID |
| Lavalier microphone | Adapter mic jack | Its own 3.5 mm wire | Clipped to the headphone cable or clothing. Mic level is set from the dashboard (default 80%) |
| Headphones | Adapter headphone jack | 3.5 mm stereo, 1.25 m | Output limited to −9 dBFS |
| iPhone / iPad | Pi | Wi-Fi (hotspot from `config.env`) | Dashboard at `http://sukoon.local:5000` |

Signal path in software: microphone → adapter ADC (48 kHz) → `src/sukoon_engine.py` (RNNoise → sound-event detection → adaptive filter → limiter) → adapter DAC → headphones.

> 📷 Add a photo of the real connections as `docs/images/headset/wiring.jpg`.
