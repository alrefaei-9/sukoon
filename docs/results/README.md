# Measured results

All measurements are on the Raspberry Pi 4B with the AB13X USB audio adapter, Test 5, Standard buffer (40 ms device buffers) and the mic level at 80%.

## Transient attenuation (bang tests A and B)

**Method.** One real bang recorded with the headset microphone (no clipped samples) was repeated 30 times, one every 8 s, over a recorded room-noise bed (−50.6 dBFS RMS). The 5-minute stimulus was fed into the live pipeline input in place of the microphone, through the dashboard's recording playback, so the input could not clip. Everything after the input was the live system: RNNoise, Test 5, the limiter and the sound card output. Input and output were captured with the timed test and scored per bang. Peak reduction is the input peak minus the output peak in a ±150 ms window.

| Test | Bang peak | Detected | Median reduction | Lowest | Highest | False triggers |
|---|---|---|---|---|---|---|
| A | −2.8 dBFS | 30 / 30 | **40.4 dB** | 35.4 dB | 60.7 dB | 1 |
| B | −12 dBFS | 30 / 30 | **25.4 dB** | 22.1 dB | 54.8 dB | 1 |

The single extra trigger in each run occurs where the stimulus file ends and the live microphone returns; it comes from the test method.

**Limits.** The bang does not pass through the air, the microphone or the adapter's input stage, and the output is measured at the adapter's digital output, not at the eardrum. Passive isolation of the earcups is not included and comes on top of these figures.

| File | Content |
|---|---|
| `bang_test_A_-3dBFS_summary.csv`, `bang_test_B_-12dBFS_summary.csv` | Detection, reduction and timing summary |
| `bang_test_A_-3dBFS_per_bang.csv`, `bang_test_B_-12dBFS_per_bang.csv` | One row per bang, with per-band reduction |
| `bang_test_A_per_bang.png` | Peak reduction per bang (test A) |
| `bang_test_A_bands.png` | Reduction per 1/3-octave band (test A) |

## Latency

**Method.** With Sukoon off, the dashboard plays ten soft sweeps into the headphones, records what leaks back into the microphone, and adds the 20 ms algorithm delay to the measured round trip. The median of the usable sweeps is reported.

**Result:** about **72 ms** microphone-to-ear typical, 64–83 ms across measurements. File: `latency_measurements.jsonl` (one JSON line per measurement).

## Stability

`soak_10min.csv`: a 10-minute run logged once per second (frames, underflows, processing time, levels, CPU, temperature, throttling). In a separate 7 h 27 min overnight run on the 10,000 mAh power bank: 0 output underflows, 5.5 ms mean processing per 10 ms frame (worst 9.8 ms), 45–50 °C, no under-voltage.
