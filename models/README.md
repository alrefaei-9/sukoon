# Models

Sukoon uses one trained neural network: **RNNoise**, the recurrent neural network noise suppressor by Xiph.Org (Jean-Marc Valin). It runs inside the audio engine on every 10 ms frame. It removes steady background noise and gives a per-frame voice probability, which the sound-event detector uses alongside its own pitch, level and onset measurements.

Sukoon's sound-event detection (speech, bang, alarm, background) is adaptive signal processing in `src/sukoon_engine.py` (`AdaptivePostProcessor`). It is not a separately trained model, so there is nothing else to download.

## How the model is obtained

The model is **not stored in this repository**. `system/install.sh` runs `src/build_rnnoise.py`, which:

1. downloads the RNNoise source at the pinned commit `70f1d256acd4b34a572f999a05c87bf00b67730d` from https://github.com/xiph/rnnoise;
2. downloads the matching trained model archive from the Xiph.Org servers and checks its SHA-256 (`0a8755f8e2d834eff6a54714ecc7d75f9932e845df35f8b59bc52a7cfe6e8b37`), plus the SHA-256 of the generated weights file;
3. compiles both with GCC for the Pi's CPU (`-O3 -march=native`) into `src/librnnoise.so`, and writes a build record `src/librnnoise.json` with the hashes.

The audio engine refuses to start if the library and its build record do not match the expected revision and model.

To rebuild by hand (internet and GCC needed, about 1–3 minutes on a Pi 4):

```bash
.venv/bin/python src/build_rnnoise.py
```

The build is deterministic: on the reference Raspberry Pi 4B with Raspberry Pi OS 64-bit (GCC 14.2), it produces a byte-identical library (SHA-256 `16d667db…`). The library is CPU-specific; rebuild it on each new device.

| | |
|---|---|
| Model | RNNoise (GRU-based, 48 kHz, 10 ms frames) |
| Size | about 15 MB compiled library, including the weights |
| Licence | BSD-3-Clause (see `RNNOISE_COPYING.txt`) |
