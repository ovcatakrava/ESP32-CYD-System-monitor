#!/usr/bin/env python3
"""
ESP32-CYD System Monitor — Cross-Platform Agent (Windows + macOS + Linux)
Automatically detects OS and uses the best available data sources.

Requirements:
    pip install psutil pyserial

Optional (for better data):
    Windows: pip install wmi GPUtil    (temps + NVIDIA GPU)
    macOS:   brew install osx-cpu-temp  (CPU temps)
    Linux:   pip install psutil         (reads /sys/class/thermal)
"""

import serial
import serial.tools.list_ports
import psutil
import json
import time
import sys
import platform
import threading
import subprocess
import re
import glob

BAUD     = 115200
INTERVAL = 1.0
SYSTEM   = platform.system()   # 'Windows', 'Darwin', 'Linux'
OS_TAGS  = {"Windows": "WIN", "Darwin": "MAC", "Linux": "LNX"}
OS_TAG   = OS_TAGS.get(SYSTEM, "???")

print(f"[INFO] Detected OS: {SYSTEM} ({OS_TAG})")

# ══════════════════════════════════════════════════════════════════════════════
# Temperature backend — auto-selected by OS
# ══════════════════════════════════════════════════════════════════════════════
_temp_lock = threading.Lock()
_cpu_temp  = 0.0
_gpu_temp  = 0.0


# ── Windows temps ──────────────────────────────────────────────────────────────
_wmi_inst = None
_gputil_ok = False

def _init_windows_temps():
    global _wmi_inst, _gputil_ok
    try:
        import wmi
        _wmi_inst = wmi.WMI(namespace="root\\OpenHardwareMonitor")
        print("[INFO] OpenHardwareMonitor WMI connected — full temps available.")
    except Exception:
        print("[WARN] OpenHardwareMonitor not found. Temps = 0.")
        print("       Run LibreHardwareMonitor as Admin for temperature support.")
    try:
        import GPUtil
        _gputil_ok = True
        print("[INFO] GPUtil found — NVIDIA GPU stats available.")
    except ImportError:
        pass


def _get_temps_windows():
    cpu_t = gpu_t = 0.0
    if _wmi_inst:
        try:
            for s in _wmi_inst.Sensor():
                if s.SensorType == "Temperature":
                    n = (s.Name or "").lower()
                    if cpu_t == 0 and "cpu" in n and ("package" in n or "die" in n or "temp" in n):
                        cpu_t = float(s.Value)
                    if gpu_t == 0 and "gpu" in n and "core" in n:
                        gpu_t = float(s.Value)
        except Exception:
            pass
    if _gputil_ok and gpu_t == 0:
        try:
            import GPUtil
            gpus = GPUtil.getGPUs()
            if gpus:
                gpu_t = gpus[0].temperature
        except Exception:
            pass
    return cpu_t, gpu_t


def _get_gpu_windows():
    if _gputil_ok:
        try:
            import GPUtil
            gpus = GPUtil.getGPUs()
            if gpus:
                return round(gpus[0].load * 100, 1), round(gpus[0].temperature, 1)
        except Exception:
            pass
    return 0.0, 0.0


# ── macOS temps ────────────────────────────────────────────────────────────────
_pm_started = False

def _try_osx_cpu_temp_bin():
    try:
        out = subprocess.check_output(["osx-cpu-temp"], timeout=2).decode()
        m = re.search(r"([\d.]+)", out)
        return float(m.group(1)) if m else 0.0
    except Exception:
        return 0.0


def _powermetrics_loop():
    cmd = ["sudo", "-n", "powermetrics", "--samplers", "thermal", "-i", "2000", "-n", "0"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        cpu_re = re.compile(r"CPU die temperature:\s*([\d.]+)", re.I)
        gpu_re = re.compile(r"GPU die temperature:\s*([\d.]+)", re.I)
        for line in proc.stdout:
            m = cpu_re.search(line)
            if m:
                with _temp_lock: globals()['_cpu_temp'] = float(m.group(1))
            m = gpu_re.search(line)
            if m:
                with _temp_lock: globals()['_gpu_temp'] = float(m.group(1))
    except Exception:
        pass


def _init_mac_temps():
    global _pm_started
    r = subprocess.run(["sudo", "-n", "powermetrics", "--help"],
                       capture_output=True, timeout=3)
    if r.returncode == 0:
        threading.Thread(target=_powermetrics_loop, daemon=True).start()
        _pm_started = True
        print("[INFO] powermetrics started in background.")
    elif _try_osx_cpu_temp_bin() > 0:
        print("[INFO] osx-cpu-temp available.")
    else:
        print("[WARN] No macOS temp source. brew install osx-cpu-temp for temps.")


def _get_temps_mac():
    with _temp_lock:
        cpu_t = _cpu_temp
        gpu_t = _gpu_temp
    if cpu_t == 0:
        cpu_t = _try_osx_cpu_temp_bin()
    return cpu_t, gpu_t


# ── Linux temps ────────────────────────────────────────────────────────────────
def _get_temps_linux():
    cpu_t = gpu_t = 0.0
    # psutil reads /sys/class/thermal on most Linux distros
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            if key in temps:
                entries = temps[key]
                cpu_t = entries[0].current
                break
        for key in ("amdgpu", "nvidia", "radeon", "nouveau"):
            if key in temps:
                gpu_t = temps[key][0].current
                break
    except Exception:
        pass
    return cpu_t, gpu_t


# ── Unified interface ──────────────────────────────────────────────────────────
def get_temps():
    if SYSTEM == "Windows":
        return _get_temps_windows()
    elif SYSTEM == "Darwin":
        return _get_temps_mac()
    else:
        return _get_temps_linux()


def get_gpu_pct():
    if SYSTEM == "Windows":
        pct, _ = _get_gpu_windows()
        return pct
    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Network / disk rate helpers
# ══════════════════════════════════════════════════════════════════════════════
_net_prev  = None; _net_t  = None
_disk_prev = None; _disk_t = None

def get_net_kbps():
    global _net_prev, _net_t
    now = time.time(); cur = psutil.net_io_counters()
    if _net_prev is None: _net_prev = cur; _net_t = now; return 0.0, 0.0
    dt = now - _net_t
    if dt < 0.1: return 0.0, 0.0
    dl = (cur.bytes_recv - _net_prev.bytes_recv) / dt / 1024
    ul = (cur.bytes_sent - _net_prev.bytes_sent) / dt / 1024
    _net_prev = cur; _net_t = now
    return max(dl, 0), max(ul, 0)


def get_disk_kbps():
    global _disk_prev, _disk_t
    now = time.time(); cur = psutil.disk_io_counters()
    if cur is None or _disk_prev is None:
        _disk_prev = cur; _disk_t = now; return 0.0, 0.0
    dt = now - _disk_t
    if dt < 0.1: return 0.0, 0.0
    dr = (cur.read_bytes  - _disk_prev.read_bytes)  / dt / 1024
    dw = (cur.write_bytes - _disk_prev.write_bytes) / dt / 1024
    _disk_prev = cur; _disk_t = now
    return max(dr, 0), max(dw, 0)


# ══════════════════════════════════════════════════════════════════════════════
# Serial port auto-detection
# ══════════════════════════════════════════════════════════════════════════════
def find_cyd_port():
    for p in serial.tools.list_ports.comports():
        hw = (p.hwid or "").upper()
        if "10C4" in hw and "EA60" in hw:   # CP2102 VID:PID
            return p.device
    if SYSTEM == "Darwin":
        for pat in ["/dev/cu.usbserial*", "/dev/cu.SLAB_USB*"]:
            m = glob.glob(pat)
            if m: return m[0]
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").upper()
        if any(k in desc for k in ("CP210", "SILICON", "UART", "USB SERIAL")):
            return p.device
    return None


def pick_port():
    port = find_cyd_port()
    if port:
        print(f"[INFO] Auto-detected CYD on {port}")
        return port
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        print("[ERROR] No serial ports found. Is the CYD plugged in via USB-C?")
        sys.exit(1)
    print("\nAvailable ports:")
    for i, p in enumerate(ports):
        info = serial.tools.list_ports.comports()[i]
        print(f"  [{i}] {p}  —  {info.description}")
    try:
        return ports[int(input("Select port: ").strip())]
    except Exception:
        return ports[0]


# ══════════════════════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════════════════════════════
def build_packet():
    cpu_pct = psutil.cpu_percent(interval=None)
    ram_pct = psutil.virtual_memory().percent
    cpu_t, gpu_t = get_temps()
    gpu_pct = get_gpu_pct()
    nd, nu  = get_net_kbps()
    dr, dw  = get_disk_kbps()
    return {
        "os":   OS_TAG,
        "cpu":  round(cpu_pct, 1),
        "ram":  round(ram_pct, 1),
        "cpu_t":round(cpu_t,   1),
        "gpu":  round(gpu_pct, 1),
        "gpu_t":round(gpu_t,   1),
        "nd":   round(nd,  1),
        "nu":   round(nu,  1),
        "dr":   round(dr,  1),
        "dw":   round(dw,  1),
    }


def main():
    # OS-specific init
    if SYSTEM == "Windows":
        _init_windows_temps()
    elif SYSTEM == "Darwin":
        _init_mac_temps()
    # Linux: psutil handles it automatically

    port = pick_port()
    print(f"[INFO] Connecting to {port} at {BAUD} baud...")

    # Warm-up counters (first call is always 0)
    psutil.cpu_percent(interval=0.3)
    get_net_kbps(); get_disk_kbps()

    while True:
        try:
            with serial.Serial(port, BAUD, timeout=2) as ser:
                print(f"[OK]   Streaming system stats to CYD...\n")
                while True:
                    pkt = build_packet()
                    ser.write((json.dumps(pkt, separators=(',', ':')) + '\n').encode())
                    print(f"\r  [{OS_TAG}]  CPU {pkt['cpu']:5.1f}%  {pkt['cpu_t']:4.0f}°C  |  "
                          f"GPU {pkt['gpu']:5.1f}%  {pkt['gpu_t']:4.0f}°C  |  "
                          f"RAM {pkt['ram']:5.1f}%  |  "
                          f"↓{pkt['nd']:6.0f}  ↑{pkt['nu']:6.0f} KB/s",
                          end='', flush=True)
                    time.sleep(INTERVAL)
        except serial.SerialException as e:
            print(f"\n[WARN] Serial disconnected: {e}. Retrying...")
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n[INFO] Stopped by user.")
            break


if __name__ == "__main__":
    main()
