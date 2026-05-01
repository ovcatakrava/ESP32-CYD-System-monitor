#!/usr/bin/env python3
"""
ESP32-CYD System Monitor — Windows Agent
Sends CPU, GPU, RAM, network, disk stats to the CYD display over USB serial.

Requirements:
    pip install psutil pyserial wmi GPUtil

GPU temperature requires an NVIDIA GPU. For AMD, install:
    pip install pyadl
"""

import serial
import serial.tools.list_ports
import psutil
import json
import time
import sys
import os

# ── Optional Windows-specific imports ──────────────────────────────────────────
try:
    import wmi
    _WMI = wmi.WMI(namespace="root\\OpenHardwareMonitor")
    _USE_OHM = True
    print("[INFO] OpenHardwareMonitor WMI found — full temperature support active.")
    print("       (Requires OHM/LibreHardwareMonitor running as Administrator)")
except Exception:
    _USE_OHM = False
    print("[WARN] OpenHardwareMonitor not found. Temps will show 0.")
    print("       For temps: run LibreHardwareMonitor as Admin, then restart this script.")

try:
    import GPUtil
    _GPUTIL = True
except ImportError:
    _GPUTIL = False

BAUD = 115200
INTERVAL = 1.0   # seconds between updates
OS_TAG = "WIN"


# ── Serial port auto-detection ─────────────────────────────────────────────────
def find_cyd_port():
    """Try to find the CP2102 (CYD) serial port automatically."""
    candidates = []
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").upper()
        hw   = (p.hwid or "").upper()
        # CP2102 VID:PID = 10C4:EA60
        if "10C4" in hw and "EA60" in hw:
            return p.device
        if "CP210" in desc or "SILICON" in desc or "UART" in desc:
            candidates.append(p.device)
        if "USB" in desc:
            candidates.append(p.device)
    if candidates:
        return candidates[0]
    return None


def pick_port():
    port = find_cyd_port()
    if port:
        print(f"[INFO] Auto-detected CYD on {port}")
        return port

    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        print("[ERROR] No serial ports found. Is the CYD plugged in?")
        sys.exit(1)

    print("\nAvailable serial ports:")
    for i, p in enumerate(ports):
        info = serial.tools.list_ports.comports()[i]
        print(f"  [{i}] {p}  — {info.description}")
    idx = input("Select port number: ").strip()
    try:
        return ports[int(idx)]
    except Exception:
        return ports[0]


# ── Sensor reading ─────────────────────────────────────────────────────────────
_cpu_temps_cache = {}

def get_temps_ohm():
    """Read temps from OpenHardwareMonitor WMI interface."""
    cpu_t = 0.0
    gpu_t = 0.0
    if not _USE_OHM:
        return cpu_t, gpu_t
    try:
        sensors = _WMI.Sensor()
        for s in sensors:
            if s.SensorType == "Temperature":
                name = (s.Name or "").lower()
                if "cpu" in name and "package" in name:
                    cpu_t = float(s.Value)
                elif "gpu" in name and "core" in name:
                    gpu_t = float(s.Value)
        # Fallback: first CPU / GPU temp found
        if cpu_t == 0 or gpu_t == 0:
            for s in sensors:
                if s.SensorType == "Temperature":
                    name = (s.Name or "").lower()
                    if cpu_t == 0 and "cpu" in name:
                        cpu_t = float(s.Value)
                    if gpu_t == 0 and "gpu" in name:
                        gpu_t = float(s.Value)
    except Exception:
        pass
    return cpu_t, gpu_t


_net_last = None
_net_last_time = None
_disk_last = None
_disk_last_time = None

def get_net_kbps():
    global _net_last, _net_last_time
    now = time.time()
    cur = psutil.net_io_counters()
    if _net_last is None:
        _net_last = cur
        _net_last_time = now
        return 0.0, 0.0
    dt = now - _net_last_time
    if dt < 0.1:
        return 0.0, 0.0
    dl = (cur.bytes_recv - _net_last.bytes_recv) / dt / 1024
    ul = (cur.bytes_sent - _net_last.bytes_sent) / dt / 1024
    _net_last = cur
    _net_last_time = now
    return max(dl, 0), max(ul, 0)


def get_disk_kbps():
    global _disk_last, _disk_last_time
    now = time.time()
    cur = psutil.disk_io_counters()
    if cur is None or _disk_last is None:
        _disk_last = cur
        _disk_last_time = now
        return 0.0, 0.0
    dt = now - _disk_last_time
    if dt < 0.1:
        return 0.0, 0.0
    dr = (cur.read_bytes  - _disk_last.read_bytes)  / dt / 1024
    dw = (cur.write_bytes - _disk_last.write_bytes) / dt / 1024
    _disk_last = cur
    _disk_last_time = now
    return max(dr, 0), max(dw, 0)


def get_gpu():
    if _GPUTIL:
        try:
            gpus = GPUtil.getGPUs()
            if gpus:
                g = gpus[0]
                return round(g.load * 100, 1), round(g.temperature, 1)
        except Exception:
            pass
    return 0.0, 0.0


def build_packet():
    cpu_pct  = psutil.cpu_percent(interval=None)
    ram_pct  = psutil.virtual_memory().percent
    cpu_t, gpu_t_ohm = get_temps_ohm()
    gpu_pct, gpu_t_gputil = get_gpu()
    gpu_t = gpu_t_ohm if gpu_t_ohm > 0 else gpu_t_gputil
    nd, nu   = get_net_kbps()
    dr, dw   = get_disk_kbps()

    return {
        "os":  OS_TAG,
        "cpu": round(cpu_pct, 1),
        "ram": round(ram_pct, 1),
        "cpu_t": round(cpu_t, 1),
        "gpu": round(gpu_pct, 1),
        "gpu_t": round(gpu_t, 1),
        "nd": round(nd, 1),
        "nu": round(nu, 1),
        "dr": round(dr, 1),
        "dw": round(dw, 1),
    }


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    port = pick_port()
    print(f"[INFO] Connecting to {port} at {BAUD} baud...")

    # Warm up psutil counters
    psutil.cpu_percent(interval=0.1)
    get_net_kbps()
    get_disk_kbps()

    while True:
        try:
            with serial.Serial(port, BAUD, timeout=2) as ser:
                print(f"[OK]   Connected. Streaming to CYD display...\n")
                while True:
                    pkt = build_packet()
                    line = json.dumps(pkt, separators=(',', ':')) + '\n'
                    ser.write(line.encode())
                    print(f"\r  CPU {pkt['cpu']:5.1f}%  {pkt['cpu_t']:4.0f}°C  |  "
                          f"GPU {pkt['gpu']:5.1f}%  {pkt['gpu_t']:4.0f}°C  |  "
                          f"RAM {pkt['ram']:5.1f}%  |  "
                          f"↓{pkt['nd']:6.0f}  ↑{pkt['nu']:6.0f} KB/s",
                          end='', flush=True)
                    time.sleep(INTERVAL)
        except serial.SerialException as e:
            print(f"\n[WARN] Serial error: {e}. Retrying in 3s...")
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n[INFO] Stopped.")
            break


if __name__ == "__main__":
    main()
