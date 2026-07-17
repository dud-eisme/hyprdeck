# Hyprdeck

## About

A little touch-control server for Hyprland — runs on an Arch box and talks to
Hyprland, PipeWire, playerctl, and brightnessctl, then serves a touch UI over
your LAN so an iPad (or any tablet/phone) can act as a physical deck for your
desktop: switch workspaces, focus windows, control volume/brightness/media,
and launch apps.

Vibecoded on a weekend because having a taskbar felt like too much. Expect rough edges.

## Usage

### Requirements

- Hyprland (with the Lua-based dispatch system, i.e. `hyprctl dispatch 'hl.dsp....'` syntax)
- `wpctl` (PipeWire), `brightnessctl`, `playerctl`
- Python 3.11+

### Setup

```bash
git clone <this repo> hyprdeck
cd hyprdeck
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Run manually

```bash
./venv/bin/python -u -m uvicorn server:app --host 0.0.0.0 --port 8765
```

Then open `http://<your-pc-lan-ip>:8765` on your tablet.

### Run on boot (systemd user service)

Copy the example unit and adjust the paths for your install location:

```bash
cp hyprdeck.service.example ~/.config/systemd/user/hyprdeck.service
# edit WorkingDirectory / ExecStart if you installed somewhere other than /opt/hyprdeck
systemctl --user daemon-reload
systemctl --user enable --now hyprdeck.service
```

Check it's alive:

```bash
systemctl --user status hyprdeck.service
journalctl --user -u hyprdeck.service -f
```

If your Hyprland session doesn't start `graphical-session.target` automatically,
add this to your Hyprland config:

```
exec-once = systemctl --user import-environment; systemctl --user start graphical-session.target
```

## ⚠️ Precaution

**None of the API endpoints require authentication.** Anyone who can reach
this port on your network can switch your workspaces, launch apps, change
volume/brightness, or trigger any other exposed endpoint — no login, no
token, nothing.

This is fine for a home LAN you trust, but:

- Do **not** port-forward this to the public internet.
- Do **not** run it on a network you don't fully trust (coffee shop wifi, shared dorm networks, etc.).
- If you want it reachable outside your LAN, put it behind a VPN (e.g. Tailscale/WireGuard) rather than exposing the port directly.
