# hwwatch

A tiny hardware watcher for Linux. It reads your CPU, GPU, NVMe, RAM and disk sensors and sends a push notification to your phone when something goes out of range. Built on Bazzite, but it should work on any systemd distro.

- **Lightweight:** one Python file, standard library only, no packages to install. Runs at lowest CPU priority with a 64 MB memory cap.
- **Immutable-distro friendly:** nothing is layered onto the OS. It runs as a systemd *user* service.
- **Phone alerts:** uses [ntfy](https://ntfy.sh) (free), plus optional desktop popups.
- **Crash detection:** optional heartbeat ping, so you get alerted if the PC hard-crashes or loses power.
- **Mining mode:** learns your idle GPU power and alerts when power falls back near it (miner stopped, crashed or stalled).

## What it watches

| Check | Default alert |
|---|---|
| GPU temperature | above 80 C |
| GPU junction temperature | above 100 C |
| GPU memory temperature | above 92 C |
| GPU power draw | above 230 W |
| GPU fan stopped (only while GPU is above 60 C) | below 1 rpm |
| CPU temperature | above 90 C |
| NVMe temperature | above 70 C |
| RAM used | above 90 % |
| Home disk free space | below 20 GB |

Every limit can be changed in the config file. Alerts must persist for a hold time (for example 30 s) before firing, repeat at most once per cooldown period, and send a "back to normal" message when they clear.

## Requirements

- Linux with systemd and Python 3.11 or newer
- **AMD GPU** (`amdgpu` driver) and an AMD (`k10temp`) or Intel (`coretemp`) CPU
- The ntfy app on your phone

NVIDIA GPUs are **not supported yet**. Sensors that don't exist on your machine show as `n/a` and are skipped.

## Install

```bash
git clone https://github.com/izzdzikri/hwwatch.git
cd hwwatch
python3 hwwatch.py --status      # check which sensors your system exposes
python3 hwwatch.py --install     # install and start the user service
```

Then edit `~/.config/hwwatch/config.toml`:

1. Set `ntfy_url` to `https://ntfy.sh/<a-long-random-topic-name>`.
2. Subscribe to the same topic in the ntfy app.
3. Run `python3 hwwatch.py --test` to confirm a notification arrives.

Use a hard-to-guess topic name. Anyone who knows it can read your alerts.

## Usage

```
hwwatch.py              run the watcher (what the service does)
hwwatch.py --status     print current readings and limits
hwwatch.py --test       send a test notification
hwwatch.py --init       write a default config file
hwwatch.py --calibrate-idle   measure and save idle GPU power (stop the miner first)
hwwatch.py --install    install and start the systemd user service
```

View logs:

```bash
journalctl --user -u hwwatch -f
```

After editing the config, restart the service:

```bash
systemctl --user restart hwwatch
```

## Configuration

`~/.config/hwwatch/config.toml`:

```toml
[general]
interval = 10          # seconds between checks
cooldown = 900         # seconds before the same alert can repeat
disabled = []          # e.g. ["ram", "disk"]

[notify]
ntfy_url = "https://ntfy.sh/your-random-topic"
desktop = true
startup_message = true
heartbeat_url = ""     # e.g. a healthchecks.io ping URL
heartbeat_every = 60

[mining]
enabled = true
margin_w = 15
hold = 60

[limits]
gpu_edge = 75
gpu_pwr = 260
gpu_junc = { limit = 95, hold = 20 }
```

Check ids: `gpu_edge`, `gpu_junc`, `gpu_mem`, `gpu_pwr`, `gpu_fan`, `cpu_temp`, `nvme`, `ram`, `disk`.

### Mining mode

Detects a miner that has stopped or crashed by watching GPU power instead of a specific process, so it works with any miner.

1. Stop the miner and close games or videos.
2. Run `python3 hwwatch.py --calibrate-idle` (add `--seconds 60` for a longer sample). It saves the median idle power to `~/.local/state/hwwatch/state.json`.
3. Set `enabled = true` under `[mining]` in the config and restart the service.

The alert level is idle power plus `margin_w` (default 15 W). If GPU power stays below it for `hold` seconds (default 60), you get a "Mining stopped or idle" notification, and a "Mining resumed" message when power recovers. Run `--status` to see the baseline and alert level.

Things to know:

- Re-calibrate if you change monitors, drivers or your desktop setup, since idle power can shift.
- A game or video raises GPU power too, so it can hide a stopped miner while it runs.
- The normal `gpu_pwr` limit (230 W by default) also applies. If your miner draws more than that, raise it under `[limits]`.
- If the whole PC powers off, the script dies with it. Use the heartbeat below to catch that.

### Catching hard crashes

A script can't report its own death. Create a check on [healthchecks.io](https://healthchecks.io) (or similar), put its ping URL in `heartbeat_url`, and set it to alert you when pings stop for a few minutes.

## Uninstall

```bash
systemctl --user disable --now hwwatch
rm ~/.config/systemd/user/hwwatch.service ~/.local/bin/hwwatch.py
rm -r ~/.config/hwwatch ~/.local/state/hwwatch
```

## Status

Early release, written for Bazzite with an AMD Radeon RX 6800 and a Ryzen CPU. Other hardware may need tweaks, so run `--status` first to see what your system exposes. Issues and pull requests are welcome.

## License

MIT, see [LICENSE](LICENSE).
