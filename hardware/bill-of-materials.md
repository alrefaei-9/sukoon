# Bill of materials

Items marked **TO CONFIRM** are not yet verified against the built unit.

## Electronics

| # | Item | Qty | Specification | Role |
|---|---|---|---|---|
| 1 | Raspberry Pi 4 Model B | 1 | 4 GB RAM, Rev 1.4, quad-core Cortex-A72 at 1.8 GHz | Runs the audio engine, the SafeSound dashboard and the Wi-Fi hotspot |
| 2 | microSD card | 1 | 64 GB (Raspberry Pi OS 64-bit, Debian 13) | Operating system, software, test recordings |
| 3 | USB audio adapter | 1 | AB13X USB Audio (USB ID `0020:0b21`), 48 kHz capture and playback, separate 3.5 mm mic and headphone jacks | ADC for the microphone, DAC for the headphones |
| 4 | Microphone | 1 | NEEWER lavalier lapel microphone: omnidirectional condenser, wired, clip-on, 3.5 mm plug into the adapter's mic jack | Picks up the surroundings |
| 5 | USB power bank | 1 | Generic, 10,000 mAh, 5 V USB output (3 A recommended) | Portable power (7 h 27 min measured) |
| 6 | USB-C cable | 1 | Short and thick (low voltage drop). **Length TO CONFIRM** | Power bank to Pi |

Alternative audio adapter recognised by the software: USB Audio `1620:0b21` (same 48 kHz requirement).

## Headphones: Ouroboros (3.5 mm version)

The earcups are 3D-printed from the open-source **[Ouroboros](https://github.com/CapraAudio/Ouroboros)** design by Capra Audio (CC BY-NC-ND 4.0). Print and assemble them from the original repository; the design files are not redistributed here. The parts below follow the Ouroboros bill of materials. This unit was printed in PETG, with the gaskets in TPU 95A.

| # | Item | Qty | Specification |
|---|---|---|---|
| 7 | Speaker drivers | 2 | 60 mm, 32 Ω ring-radiator coaxial units (as in the Ouroboros BOM). The centre tweeter is disconnected and used as a passive radiator |
| 8 | Ear pads | 2 | Yaxi pads (leather) |
| 9 | Connector | 1 per cup (or as designed) | 3.5 mm connector version |
| 10 | Wire | — | 22 AWG |
| 11 | Baffle mesh | — | 10 µm nylon mesh |
| 12 | Rear tuning tape | — | 3M Micropore tape |
| 13 | Rear damping | — | Tuning foam (Geekria) |
| 14 | Screws | 10 + 5 | M3×12 mm and M3×16 mm flat-head socket cap |
| 15 | Heat-set inserts | ~12 | M3 brass heat-set inserts |
| 16 | Gasket | 2 | Printed in TPU 95A |
| 17 | Printed parts | 1 set | Rear housings, driver mounts, grilles, baffle retention plates, pad mounts and headband (Capra headband design), printed in **PETG**; gaskets in **TPU 95A** |
| 18 | Headphone cable | 1 | 3.5 mm stereo, 1.25 m, headphones to adapter headphone jack |

## Headset integration (DIY 3D-printed)

| # | Item | Qty | Notes |
|---|---|---|---|
| 19 | Microphone mount | — | The lavalier's own clip: it attaches to the headphone cable or the wearer's clothing (separate wire from the headphone cable) |
| 20 | Pi / power bank enclosure or carrier | 1 | **TO CONFIRM** (worn on the headset, belt or pocket) |

## Software (installed by `system/install.sh`)

Raspberry Pi OS (64-bit), Python 3.13, PortAudio, ALSA utilities, GCC, NetworkManager hotspot, Avahi (mDNS), RNNoise (built from pinned source), Python packages in `requirements.txt`.
