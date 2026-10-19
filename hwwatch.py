#!/usr/bin/env python3
"""hwwatch - tiny hardware watcher for Linux (built for Bazzite, stdlib only).

  hwwatch.py --init      write a default config to ~/.config/hwwatch/config.toml
  hwwatch.py --status    print current readings and exit
  hwwatch.py --test      send a test notification and exit
  hwwatch.py --calibrate-idle   measure + save idle GPU power (stop the miner first)
  hwwatch.py --install   install + start as a systemd user service
  hwwatch.py             run the watcher (what the service does)
"""
import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "hwwatch" / "config.toml"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
STATUS_FILE = RUNTIME / "hwwatch.json"
UNIT = HOME / ".config/systemd/user/hwwatch.service"
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "hwwatch" / "state.json"

DEFAULT_CONFIG = """\
[general]
interval = 10          # seconds between checks
cooldown = 900         # seconds before the same alert can repeat
disabled = []          # e.g. ["ram", "disk"]

[notify]
ntfy_url = "https://ntfy.sh/CHANGE-ME-to-something-random"
desktop = true         # also show a popup on this PC
startup_message = true
# Dead-man switch: ping this URL every heartbeat_every seconds. A service such
# as healthchecks.io alerts you when pings STOP, which catches hard crashes
# and power loss that this script can never report itself.
heartbeat_url = ""
heartbeat_every = 60

# Mining mode: learns your idle GPU power, then alerts when power falls back
# near it (miner crashed, stopped, or stalled). Run `hwwatch.py --calibrate-idle`
# with the miner STOPPED first. Alert level = idle power + margin_w.
[mining]
enabled = false
margin_w = 15          # watts above idle that still counts as "stopped"
hold = 60              # seconds GPU power must stay low before alerting
idle_w = 0             # 0 = use the calibrated value; set a number to override

# Override any limit by id. Format: id = limit  OR  id = { limit = X, hold = SECONDS }
[limits]
# gpu_edge = 80
# gpu_junc = { limit = 100, hold = 30 }
"""

# id, label, unit, mode, default limit, default hold (s)
CHECKS = [
    ("gpu_edge", "GPU temp",       "C",  "above", 80,  30),
    ("gpu_junc", "GPU junction",   "C",  "above", 100, 30),
    ("gpu_mem",  "GPU memory",     "C",  "above", 92,  30),
    ("gpu_pwr",  "GPU power",      "W",  "above", 230, 60),
    ("gpu_fan",  "GPU fan stopped", "rpm", "below", 1,   20),
    ("cpu_temp", "CPU temp",       "C",  "above", 90,  30),
    ("nvme",     "NVMe temp",      "C",  "above", 70,  60),
    ("ram",      "RAM used",       "%",  "above", 90,  60),
    ("disk",     "Disk free",      "GB", "below", 20,  0),
]
CLEAR_MARGIN = 0.05  # value must recover 5% past the limit before "back to normal"


# ---------- sensors ----------
def hwmons(name):
    out = []
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            if Path(d, "name").read_text().strip() == name:
                out.append(d)
        except OSError:
            pass
    return out


def read(dirs, fname, scale=1.0):
    """Highest value of a sysfs file across the given hwmon dirs, or None."""
    vals = []
    for d in dirs:
        try:
            vals.append(int(Path(d, fname).read_text()) / scale)
        except (OSError, ValueError):
            pass
    return max(vals) if vals else None


def ram_used_pct():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            m[k] = int(v.split()[0])
    return 100 * (1 - m["MemAvailable"] / m["MemTotal"])


class Sensors:
    def __init__(self):
        self.gpu = hwmons("amdgpu")
        self.cpu = hwmons("k10temp") or hwmons("coretemp")
        self.nvme = hwmons("nvme")

    def sample(self):
        gpu_temp = read(self.gpu, "temp1_input", 1000)
        gpu_pwr = read(self.gpu, "power1_average", 1e6)
        fan = read(self.gpu, "fan1_input")
        # RX 6800 fans stop at idle on purpose; only a stopped fan on a warm GPU is a problem.
        fan_val = fan if (gpu_temp is not None and gpu_temp > 60) else None
        return {
            "gpu_edge": gpu_temp,
            "gpu_junc": read(self.gpu, "temp2_input", 1000),
            "gpu_mem": read(self.gpu, "temp3_input", 1000),
            "gpu_pwr": gpu_pwr,
            "mining": gpu_pwr,
            "gpu_fan": fan_val,
            "cpu_temp": read(self.cpu, "temp1_input", 1000),
            "nvme": read(self.nvme, "temp1_input", 1000),
            "ram": ram_used_pct(),
            "disk": shutil.disk_usage(HOME).free / 1e9,
        }


# ---------- config ----------
def load_config():
    cfg = {"general": {}, "notify": {}, "limits": {}, "mining": {}}
    if CONFIG.exists():
        try:
            with open(CONFIG, "rb") as f:
                user = tomllib.load(f)
            for k in cfg:
                cfg[k].update(user.get(k, {}))
        except (tomllib.TOMLDecodeError, OSError) as e:
            print(f"config error, using defaults: {e}", flush=True)
    g, n = cfg["general"], cfg["notify"]
    g.setdefault("interval", 10)
    g.setdefault("cooldown", 900)
    g.setdefault("disabled", [])
    n.setdefault("ntfy_url", "")
    n.setdefault("desktop", True)
    n.setdefault("startup_message", True)
    n.setdefault("heartbeat_url", "")
    n.setdefault("heartbeat_every", 60)
    m = cfg["mining"]
    m.setdefault("enabled", False)
    m.setdefault("margin_w", 15)
    m.setdefault("hold", 60)
    m.setdefault("idle_w", 0)
    return cfg


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def idle_baseline(cfg):
    return cfg["mining"]["idle_w"] or load_state().get("idle_w") or 0


def effective_checks(cfg):
    checks = []
    for cid, label, unit, mode, limit, hold in CHECKS:
        if cid in cfg["general"]["disabled"]:
            continue
        o = cfg["limits"].get(cid)
        if isinstance(o, dict):
            limit, hold = o.get("limit", limit), o.get("hold", hold)
        elif isinstance(o, (int, float)):
            limit = o
        checks.append((cid, label, unit, mode, limit, hold))
    m = cfg["mining"]
    idle = idle_baseline(cfg)
    if m["enabled"] and idle:
        checks.append(("mining", "GPU power (mining)", "W", "below",
                       idle + m["margin_w"], m["hold"]))
    return checks


# alert title, recovery title (checks not listed use the generic wording)
TITLES = {"mining": ("Mining stopped or idle", "Mining resumed")}


# ---------- notifications ----------
def http(url, data=None, headers=None):
    try:
        req = urllib.request.Request(url, data=data, headers=headers or {})
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:
        print(f"http error: {e}", flush=True)
        return False


def notify(cfg, title, msg, prio="default", tags="warning"):
    n = cfg["notify"]
    print(f"[notify] {title}: {msg}", flush=True)
    if n["ntfy_url"] and "CHANGE-ME" not in n["ntfy_url"]:
        http(n["ntfy_url"], msg.encode(), {"Title": title, "Priority": prio, "Tags": tags})
    if n["desktop"]:
        try:
            subprocess.run(["notify-send", "-a", "hwwatch", title, msg],
                           check=False, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def snapshot(s):
    def f(k, fmt):
        return fmt.format(s[k]) if s.get(k) is not None else "n/a"
    return (f"GPU {f('gpu_edge', '{:.0f}')}C / junction {f('gpu_junc', '{:.0f}')}C / "
            f"{f('gpu_pwr', '{:.0f}')}W | CPU {f('cpu_temp', '{:.0f}')}C | "
            f"RAM {f('ram', '{:.0f}')}%")


# ---------- main loop ----------
def run():
    cfg = load_config()
    sensors = Sensors()
    checks = effective_checks(cfg)
    interval = cfg["general"]["interval"]
    cooldown = cfg["general"]["cooldown"]
    hb_url = cfg["notify"]["heartbeat_url"]
    hb_every = cfg["notify"]["heartbeat_every"]

    stop = False

    def handle(_sig, _frm):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    mining_note = ""
    if cfg["mining"]["enabled"]:
        if any(c[0] == "mining" for c in checks):
            mining_note = f"Mining mode on (idle baseline {idle_baseline(cfg):.0f}W). "
        else:
            mining_note = "Mining mode is on but NOT calibrated: run --calibrate-idle. "
            print(mining_note, flush=True)

    if cfg["notify"]["startup_message"]:
        s = sensors.sample()
        notify(cfg, "hwwatch started", mining_note + snapshot(s), "low", "white_check_mark")

    bad_since, last_sent, active = {}, {}, set()
    last_hb = 0.0
    while not stop:
        now = time.time()
        s = sensors.sample()
        for cid, label, unit, mode, limit, hold in checks:
            v = s.get(cid)
            if v is None:
                continue
            bad = v > limit if mode == "above" else v < limit
            if bad:
                bad_since.setdefault(cid, now)
                due = now - bad_since[cid] >= hold
                if due and now - last_sent.get(cid, 0) > cooldown:
                    word = "over" if mode == "above" else "under"
                    notify(cfg, TITLES.get(cid, (f"PC alert: {label}",))[0],
                           f"{label} {v:.1f}{unit} ({word} {limit}{unit}). {snapshot(s)}",
                           "high", "rotating_light")
                    last_sent[cid] = now
                    active.add(cid)
            else:
                bad_since.pop(cid, None)
                recovered = (v <= limit * (1 - CLEAR_MARGIN) if mode == "above"
                             else v >= limit * (1 + CLEAR_MARGIN))
                if cid in active and recovered:
                    active.discard(cid)
                    last_sent.pop(cid, None)
                    notify(cfg, TITLES.get(cid, (None, f"PC OK: {label}"))[1],
                           f"{label} back to normal ({v:.1f}{unit})",
                           "low", "white_check_mark")

        try:
            STATUS_FILE.write_text(json.dumps({"time": int(now), "readings": s,
                                               "active_alerts": sorted(active)}))
        except OSError:
            pass

        if hb_url and now - last_hb >= hb_every:
            if http(hb_url):
                last_hb = now

        time.sleep(interval)

    print("hwwatch stopped", flush=True)


# ---------- CLI helpers ----------
def cmd_status():
    cfg = load_config()
    s = Sensors().sample()
    print(f"{'check':<18}{'value':>10}  limit")
    for cid, label, unit, mode, limit, _ in effective_checks(cfg):
        v = s.get(cid)
        shown = "n/a" if v is None else f"{v:.1f}{unit}"
        sign = ">" if mode == "above" else "<"
        print(f"{label:<18}{shown:>10}  alert if {sign} {limit:g}{unit}")
    m = cfg["mining"]
    idle = idle_baseline(cfg)
    if not m["enabled"]:
        print("\nMining mode: off (set enabled = true under [mining])")
    elif idle:
        print(f"\nMining mode: on, idle baseline {idle:g}W, "
              f"alert if GPU power < {idle + m['margin_w']:g}W for {m['hold']}s")
    else:
        print("\nMining mode: on, but no idle baseline yet. "
              "Stop the miner, then run --calibrate-idle")


def cmd_calibrate_idle(seconds):
    sensors = Sensors()
    if not sensors.gpu or read(sensors.gpu, "power1_average", 1e6) is None:
        print("no GPU power sensor found (amdgpu power1_average)")
        return 1
    print(f"Measuring idle GPU power for {seconds}s. Make sure the miner is STOPPED "
          "and no games or videos are running...")
    vals = []
    end = time.time() + seconds
    while time.time() < end:
        v = read(sensors.gpu, "power1_average", 1e6)
        if v is not None:
            vals.append(v)
        time.sleep(2)
    vals.sort()
    idle = vals[len(vals) // 2]  # median ignores brief spikes
    state = load_state()
    state["idle_w"] = round(idle, 1)
    state["calibrated_at"] = time.strftime("%Y-%m-%d %H:%M")
    save_state(state)
    print(f"Idle GPU power: {idle:.1f}W (range {vals[0]:.1f}-{vals[-1]:.1f}W), saved to {STATE_FILE}")
    if idle > 60:
        print("That is high for idle. Is the miner or a game still running? Re-run when the GPU is quiet.")
    elif vals[-1] > idle * 1.5 + 10:
        print("Readings varied a lot. For a cleaner baseline, re-run when nothing is using the GPU.")
    print("Enable alerts with enabled = true under [mining], then restart: "
          "systemctl --user restart hwwatch")
    return 0


def cmd_init():
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        print(f"{CONFIG} already exists, leaving it alone")
        return
    CONFIG.write_text(DEFAULT_CONFIG)
    print(f"wrote {CONFIG}\nedit ntfy_url, then run: hwwatch.py --test")


def cmd_install():
    script = Path(__file__).resolve()
    dest = HOME / ".local/bin/hwwatch.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if script != dest:
        shutil.copy2(script, dest)
    dest.chmod(0o755)
    cmd_init()
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(f"""[Unit]
Description=Lightweight hardware watcher
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 {dest}
Restart=always
RestartSec=5
Nice=19
MemoryMax=64M

[Install]
WantedBy=default.target
""")
    for c in (["systemctl", "--user", "daemon-reload"],
              ["systemctl", "--user", "enable", "--now", "hwwatch"],
              ["loginctl", "enable-linger", os.environ.get("USER", "")]):
        subprocess.run(c, check=False)
    print("installed. logs: journalctl --user -u hwwatch -f")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--calibrate-idle", action="store_true")
    ap.add_argument("--seconds", type=int, default=30, help="calibration length (default 30)")
    a = ap.parse_args()
    if a.init:
        cmd_init()
    elif a.status:
        cmd_status()
    elif a.test:
        cfg = load_config()
        notify(cfg, "hwwatch test", snapshot(Sensors().sample()), "default", "tada")
    elif a.calibrate_idle:
        return cmd_calibrate_idle(a.seconds)
    elif a.install:
        cmd_install()
    else:
        run()


if __name__ == "__main__":
    sys.exit(main())
