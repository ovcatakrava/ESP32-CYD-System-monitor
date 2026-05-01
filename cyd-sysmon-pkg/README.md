# ESP32-CYD System Monitor

A real-time PC system stats display for the **ESP32-2432S028R (Cheap Yellow Display)**
with USB-C (CP2102 chip). Shows CPU%, GPU%, temps, RAM, network, and disk I/O.

---

## Quick Start

### 1. Get the firmware .bin

**Option A — GitHub Actions (easiest, no local tools needed)**
1. Fork this repo on GitHub
2. Push to `main` — Actions compiles and creates a Release automatically
3. Download `esp32-cyd-sysmon.bin` from the Releases page

**Option B — Local PlatformIO**
```bash
pip install platformio
cd firmware
pio run
# .bin is at: firmware/.pio/build/esp32dev/firmware.bin
```

### 2. Flash the firmware
Open `flasher/cyd-sysmon-flasher.html` in Chrome or Edge.
- Drop the `.bin` file onto the firmware zone (saved permanently in your browser)
- Connect your CYD via USB-C → click **Connect**
- Click **⚡ Flash Firmware**

### 3. Run the PC agent
```bash
pip install psutil pyserial

# Windows (best temps with LibreHardwareMonitor as Admin):
python agents/sysmon-windows.py

# macOS (temps via powermetrics or brew install osx-cpu-temp):
python agents/sysmon-mac.py

# Either / Linux:
python agents/sysmon-cross.py
```
The agent auto-detects the CYD's serial port and starts streaming immediately.

---

## What's displayed

| Metric       | Source                            |
|--------------|-----------------------------------|
| CPU %        | psutil (all platforms)            |
| CPU temp     | OHM/WMI (Win), powermetrics (Mac) |
| GPU %        | GPUtil / NVIDIA (Win)             |
| GPU temp     | OHM (Win), powermetrics (Mac)     |
| RAM %        | psutil                            |
| Net ↓↑ KB/s  | psutil                            |
| Disk R/W KB/s| psutil                            |

---

## Files

```
firmware/
  platformio.ini          PlatformIO build config (ESP32-WROOM, TFT_eSPI)
  src/main.cpp            Firmware source — ILI9341 dashboard, Serial JSON parsing
  .github/workflows/
    build.yml             Auto-compiles on push and publishes GitHub Release

agents/
  sysmon-windows.py       Windows agent (WMI temps + GPUtil GPU)
  sysmon-mac.py           macOS agent (powermetrics / osx-cpu-temp)
  sysmon-cross.py         Cross-platform (Windows + macOS + Linux auto-detect)

flasher/
  cyd-sysmon-flasher.html Auto-flash website — stores .bin in browser IndexedDB
```

---

## Board Pinout (ESP32-2432S028R)

| Signal    | GPIO |
|-----------|------|
| TFT MOSI  | 13   |
| TFT SCLK  | 14   |
| TFT CS    | 15   |
| TFT DC    | 2    |
| TFT RST   | 12   |
| Backlight | 21   |
| Touch CLK | 25   |
| Touch CS  | 33   |
| Touch DIN | 32   |
| Touch OUT | 39   |

---

## Serial Protocol

The firmware reads newline-terminated JSON at 115200 baud:

```json
{"os":"WIN","cpu":45.2,"ram":67.1,"cpu_t":72.0,"gpu":88.5,"gpu_t":65.0,"nd":1240.5,"nu":88.2,"dr":512.0,"dw":128.0}
```

| Key   | Meaning                  | Unit  |
|-------|--------------------------|-------|
| os    | Platform tag (WIN/MAC/LNX)| —    |
| cpu   | CPU utilization          | %     |
| ram   | RAM utilization          | %     |
| cpu_t | CPU temperature          | °C    |
| gpu   | GPU utilization          | %     |
| gpu_t | GPU temperature          | °C    |
| nd    | Network download         | KB/s  |
| nu    | Network upload           | KB/s  |
| dr    | Disk read                | KB/s  |
| dw    | Disk write               | KB/s  |
