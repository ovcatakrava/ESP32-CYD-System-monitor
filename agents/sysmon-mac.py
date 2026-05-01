#!/usr/bin/env python3
"""
ESP32-CYD System Monitor — macOS Agent
Sends CPU, GPU, RAM, network, disk stats to the CYD display over USB serial.

Requirements:
    pip install psutil pyserial

Temperature notes:
  - CPU temps are read via 'sudo powermetrics' (requires password once)
  - Or install osx-cpu-temp:  brew install osx-cpu-temp
  - Apple Silicon (M1/M2/M3): powermetrics gives die temps
  - Intel Mac: SMC sensors via powermetrics
"""

import serial
import serial.tools.list_ports
import psutil
import json
import time
import sys
import subprocess
import threading
import re

BAUD     = 115200
INTERVAL = 1.0
OS_TAG   = "MAC"

# ── Temperature reading ────────────────────────────────────────────────────────
_temp_lock  = threading.Lock()
_cpu_temp   = 0.0
_gpu_temp   = 0.0
_pm_running = False


def _try_osx_cpu_temp():
    """Try osx-cpu-temp binary (brew install osx-cpu-temp)."""
    try:
        out = subprocess.check_output(["osx-cpu-temp"], timeout=2).decode()
        m = re.search(r"([\d.]+)", out)
        if m:
            return float(m.group(1))
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        pass
    return 0.0


def _powermetrics_loop():
    """Background thread reading powermetrics every 2s."""
    global _cpu_temp, _gpu_temp, _pm_running
    cmd = [
        "sudo", "-n", "powermetrics",
        "--samplers", "thermal",
        "-i", "2000",
        "-n", "0",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        cpu_pat = re.compile(r"CPU die temperature:\s*([\d.]+)", re.IGNORECASE)
        gpu_pat = re.compile(r"GPU die temperature:\s*([\d.]+)", re.IGNORECASE)
        for line in proc.stdout:
            m = cpu_pat.search(line)
            if m:
                with _temp_lock:
                    _cpu_temp = float(m.group(1))
            m = gpu_pat.search(line)
            if m:
                with _temp_lock:
                    _gpu_temp = float(m.group(1))
    except Exception:
        pass


def start_temp_thread():
    global _pm_running
    if _pm_running:
        return
    # Check if sudo -n works (passwordless sudo available)
    r = subprocess.run(["sudo", "-n", "powermetrics", "--help"],
                       capture_output=True, timeout=3)
    if r.returncode == 0:
        t = threading.Thread(target=_powermetrics_loop, daemon=True)
        t.start()
        _pm_running = True
        print("[INFO] powermetrics temperature monitoring started.")
    else:
        # Try osx-cpu-temp fallback
        t = _try_osx_cpu_temp()
        if t > 0:
            print("[INFO] osx-cpu-temp found. Using for CPU temperature.")
        else:
            print("[WARN] No temperature source found.")
            print("       Option 1: brew install osx-cpu-temp")
            print("       Option 2: run this script with sudo for powermetrics access")


def get_temps():
    with _temp_lock:
        cpu_t = _cpu_temp
        gpu_t = _gpu_temp
    if cpu_t == 0:
        cpu_t = _try_osx_cpu_temp()
    return cpu_t, gpu_t


# ── Network & disk ─────────────────────────────────────────────────────────────
_net_last      = None
_net_last_time = None
_disk_last     = None
_disk_last_time= None

def get_net_kbps():
    global _net_last, _net_last_time
    now = time.time()
    cur = psutil.net_io_counters()
    if _net_last is None:
        _net_last = cur; _net_last_time = now; return 0.0, 0.0
    dt = now - _net_last_time
    if dt < 0.1: return 0.0, 0.0
    dl = (cur.bytes_recv - _net_last.bytes_recv) / dt / 1024
    ul = (cur.bytes_sent - _net_last.bytes_sent) / dt / 1024
    _net_last = cur; _net_last_time = now
    return max(dl, 0), max(ul, 0)


def get_disk_kbps():
    global _disk_last, _disk_last_time
    now = time.time()
    cur = psutil.disk_io_counters()
    if cur is None or _disk_last is None:
        _disk_last = cur; _disk_last_time = now; return 0.0, 0.0
    dt = now - _disk_last_time
    if dt < 0.1: return 0.0, 0.0
    dr = (cur.read_bytes  - _disk_last.read_bytes)  / dt / 1024
    dw = (cur.write_bytes - _disk_last.write_bytes) / dt / 1024
    _disk_last = cur; _disk_last_time = now
    return max(dr, 0), max(dw, 0)


# ── Serial port ────────────────────────────────────────────────────────────────
def find_cyd_port():
    for p in serial.tools.list_ports.comports():
        dev = p.device
        hw  = (p.hwid or "").upper()
        # CP2102 VID:PID = 10C4:EA60  (USB-C on CYD)
        if "10C4" in hw and "EA60" in hw:
            return dev
        if "USBSERIAL" in dev or "CU.WCHUSBSERIAL" in dev:
            return dev
    # Fallback: any /dev/cu.usbserial* or /dev/cu.SLAB*
    import glob
    for pattern in ["/dev/cu.usbserial*", "/dev/cu.SLAB_USB*", "/dev/cu.wchusbserial*"]:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
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
    print("\nAvailable ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p}")
    idx = input("Select port number: ").strip()
    try:
        return ports[int(idx)]
    except Exception:
        return ports[0]


# ── Main ───────────────────────────────────────────────────────────────────────
def build_packet():
    cpu_pct = psutil.cpu_percent(interval=None)
    ram_pct = psutil.virtual_memory().percent
    cpu_t, gpu_t = get_temps()
    nd, nu = get_net_kbps()
    dr, dw = get_disk_kbps()
    return {
        "os":   OS_TAG,
        "cpu":  round(cpu_pct, 1),
        "ram":  round(ram_pct, 1),
        "cpu_t":round(cpu_t, 1),
        "gpu":  0.0,   # GPU % not easily available on macOS without Metal APIs
        "gpu_t":round(gpu_t, 1),
        "nd":   round(nd, 1),
        "nu":   round(nu, 1),
        "dr":   round(dr, 1),
        "dw":   round(dw, 1),
    }


def main():
    start_temp_thread()
    port = pick_port()
    print(f"[INFO] Connecting to {port} at {BAUD} baud...")
    psutil.cpu_percent(interval=0.1)
    get_net_kbps(); get_disk_kbps()

    while True:
        try:
            with serial.Serial(port, BAUD, timeout=2) as ser:
                print(f"[OK]   Connected. Streaming to CYD display...\n")
                while True:
                    pkt = build_packet()
                    ser.write((json.dumps(pkt, separators=(',',':')) + '\n').encode())
                    print(f"\r  CPU {pkt['cpu']:5.1f}%  {pkt['cpu_t']:4.0f}°C  |  "
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
