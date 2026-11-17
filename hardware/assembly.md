# Assembly notes

## 1. Headphones

1. Print and assemble the **[Ouroboros](https://github.com/CapraAudio/Ouroboros)** headphones (3.5 mm connector version) following Capra Audio's printing and assembly instructions:
   - This unit: printed parts in **PETG** and gaskets in **TPU 95A** (the design requires TPU 95A for the gasket), with at least 3 walls of 0.4 mm and 3 top/bottom layers.
   - Prepare each driver: disconnect the centre tweeter and remove its capacitor, then cover the rear vent with one layer of Micropore tape.
   - Fit the brass inserts, solder the drivers to the 3.5 mm connector, and fit the gasket, tuning foam, driver, grille, 10 µm mesh, retention plate and pad mount (M3×16 screws, tightened in a star pattern).
2. Fit the Yaxi pads and the headband.

The Ouroboros design is CC BY-NC-ND 4.0: build it for yourself, but do not sell it or redistribute modified design files.

## 2. Microphone

The NEEWER lavalier microphone runs on its own wire, separate from the headphone cable. Clip it to the headphone cable or to the wearer's clothing (collar or chest), facing outward, so it hears roughly what the wearer would hear. Plug it into the adapter's mic jack. The closed earcups keep the drivers from feeding back into it.

## 3. Electronics

1. Flash **Raspberry Pi OS (64-bit)** onto the 64 GB microSD card and run `system/install.sh` (see the main README).
2. With the Pi **powered off**, plug the ESSAGER (AB13X) adapter into any USB port, then the microphone into its mic jack and the headphones into its headphone jack.
3. Fit the Pi in its fan case (fan leads on the GPIO 5 V and GND pins) and stack it on top of the power bank. Connect them with the 15 cm right-angle USB-C cable. Keep the case vents and fan uncovered: the Pi ran at 45–50 °C in a 7-hour test.
4. Route the cables so they do not pull on the 3.5 mm plugs while the headset is worn.

## 4. First power-up check

1. Connect the power bank. After about 90 seconds you hear the welcome voice in the headphones, then the processed room sound.
2. Clap: it should be much quieter in the headphones than in the room.
3. On the phone, join the hotspot and open `http://sukoon.local:5000`. **Home › System check** should show the sound card and microphone ticked.
4. Optional: **System › Latency › Measure latency** with Sukoon turned off (expect about 70 ms).

> 📷 Photo placeholders for this page: `docs/images/headset/assembly_earcup.jpg`, `docs/images/headset/assembly_electronics.jpg`.
