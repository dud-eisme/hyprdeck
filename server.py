"""
Hyprland Deck server
---------------------
Runs on your Arch box, talks to Hyprland/PipeWire/playerctl/brightnessctl,
and serves a touch UI (served as static files) that your iPad connects to
over your LAN. The iPad never talks to the WM directly - this process is
the only thing that needs to run on your PC.

Run:
    python3 server.py
or via the included systemd unit (see README.md).
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Set
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI()

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# shell helpers
# ---------------------------------------------------------------------------


def run(cmd: list[str]) -> str:
    """Run a command, return stdout (empty string on any failure)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        return result.stdout.strip()
    except Exception:
        return ""


def hyprctl_json(args: list[str]):
    out = run(["hyprctl", "-j", *args])
    try:
        return json.loads(out)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# websocket fan-out
# ---------------------------------------------------------------------------


class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

# ---------------------------------------------------------------------------
# state readers
# ---------------------------------------------------------------------------


def parse_volume(raw: str):
    # e.g. "Volume: 0.45 [MUTED]" or "Volume: 0.45"
    muted = "MUTED" in raw
    level = 0
    for p in raw.split():
        try:
            level = round(float(p) * 100)
            break
        except ValueError:
            continue
    return {"level": level, "muted": muted}


def parse_brightness():
    cur = run(["brightnessctl", "g"])
    mx = run(["brightnessctl", "m"])
    try:
        return round(int(cur) / int(mx) * 100)
    except Exception:
        return None


def get_media_state():
    status = run(["playerctl", "-p", "spotify_player", "status"])
    if not status:
        return None
    title = run(["playerctl", "-p", "spotify_player", "metadata", "title"])
    artist = run(["playerctl", "-p", "spotify_player", "metadata", "artist"])
    art_url = run(["playerctl", "-p", "spotify_player", "metadata", "mpris:artUrl"])
    return {"status": status, "title": title, "artist": artist, "art_url": art_url}


def get_clients():
    """All open windows, raw from hyprctl."""
    return hyprctl_json(["clients"]) or []


def get_monitors():
    return hyprctl_json(["monitors"]) or []


def get_windows_by_workspace():
    """
    Group open windows by workspace id, with position/size normalized to
    0..1 relative to the monitor that workspace lives on - this is what
    the frontend needs to draw the little "windows inside the workspace
    tile" preview, same idea as the end-4 (ii) overview grid.
    """
    clients = get_clients()
    monitors = get_monitors()

    # monitor resolution for whichever workspace is currently showing on it
    mon_by_ws: dict[int, dict] = {}
    for m in monitors:
        active_ws = m.get("activeWorkspace", {})
        ws_id = active_ws.get("id")
        if ws_id is not None:
            mon_by_ws[ws_id] = {
                "width": m.get("width", 1920) / m.get("scale", 1) or 1920,
                "height": m.get("height", 1080) / m.get("scale", 1) or 1080,
                "x": m.get("x", 0),
                "y": m.get("y", 0),
            }
    # sensible fallback for workspaces that aren't the active one on any
    # monitor (we don't know their monitor's exact geometry, so fall back
    # to the first monitor's resolution - still gives a reasonable preview)
    fallback = mon_by_ws.get(
        next(iter(mon_by_ws), None),
        {"width": 1920, "height": 1080, "x": 0, "y": 0},
    )

    windows_by_ws: dict[int, list] = {}
    for c in clients:
        ws_id = c.get("workspace", {}).get("id")
        if ws_id is None or ws_id < 0:
            continue  # skip special workspaces
        mon = mon_by_ws.get(ws_id, fallback)
        x, y = c.get("at", [0, 0])
        w, h = c.get("size", [0, 0])
        # window coords from hyprctl are absolute (monitor-space), so
        # subtract the monitor origin before normalizing
        rel_x = x - mon["x"]
        rel_y = y - mon["y"]
        windows_by_ws.setdefault(ws_id, []).append(
            {
                "address": c.get("address"),
                "class": c.get("class") or c.get("initialClass") or "",
                "title": c.get("title", ""),
                "focused": bool(c.get("focusHistoryID") == 0),
                "floating": bool(c.get("floating")),
                "fullscreen": bool(c.get("fullscreen")),
                "x": round(rel_x / mon["width"], 4) if mon["width"] else 0,
                "y": round(rel_y / mon["height"], 4) if mon["height"] else 0,
                "w": round(w / mon["width"], 4) if mon["width"] else 0,
                "h": round(h / mon["height"], 4) if mon["height"] else 0,
            }
        )
    return windows_by_ws


# ---------------------------------------------------------------------------
# system vitals + hotspot status (for the "suggestions" panel)
# ---------------------------------------------------------------------------

_last_cpu = None


def get_cpu_percent():
    """
    CPU usage % since the last call, computed from /proc/stat deltas.
    Returns 0 on the very first call (no prior sample to diff against yet).
    """
    global _last_cpu
    try:
        with open("/proc/stat") as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + parts[4]
        total = sum(parts)
        if _last_cpu is None:
            _last_cpu = (idle, total)
            return 0
        d_idle = idle - _last_cpu[0]
        d_total = total - _last_cpu[1]
        _last_cpu = (idle, total)
        return round((1 - d_idle / d_total) * 100) if d_total else 0
    except Exception:
        return None


def get_mem_percent():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 1)
        avail = info.get("MemAvailable", 0)
        return round((total - avail) / total * 100)
    except Exception:
        return None


def get_gpu_stats():
    # Requires nvidia-smi. If you're not on Nvidia, this just returns None
    # and the frontend hides the GPU row - safe to leave in either way.
    out = run(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    if not out:
        return None
    try:
        util, temp = [p.strip() for p in out.split(",")]
        return {"util": int(util), "temp": int(temp)}
    except Exception:
        return None


def get_system_stats():
    return {"cpu": get_cpu_percent(), "mem": get_mem_percent(), "gpu": get_gpu_stats()}


def get_hotspot_status():
    # NOTE: adjust "Hotspot" (nmcli connection name) and "wlo1" (interface)
    # below if yours are named differently.
    state = run(["nmcli", "-t", "-f", "GENERAL.STATE", "connection", "show", "Hotspot"])
    active = "activated" in state.lower()
    ssid = run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "Hotspot"])
    client_count = 0
    if active:
        raw = run(["iw", "dev", "wlo1", "station", "dump"])
        client_count = sum(1 for line in raw.splitlines() if line.startswith("Station"))
    return {"active": active, "ssid": ssid, "client_count": client_count}


def get_full_state():
    workspaces = hyprctl_json(["workspaces"]) or []
    active = hyprctl_json(["activeworkspace"]) or {}
    volume = parse_volume(run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]))
    windows_by_ws = get_windows_by_workspace()
    return {
        "workspaces": sorted(
            [
                {
                    "id": w["id"],
                    "name": w.get("name", str(w["id"])),
                    "windows": windows_by_ws.get(w["id"], []),
                }
                for w in workspaces
            ],
            key=lambda w: w["id"],
        ),
        "active_workspace": active.get("id"),
        "volume": volume["level"],
        "muted": volume["muted"],
        "brightness": parse_brightness(),
        "media": get_media_state(),
        "system": get_system_stats(),
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/api/state")
async def get_state():
    return JSONResponse(get_full_state())


class WorkspaceReq(BaseModel):
    id: int


@app.post("/api/workspace")
async def set_workspace(req: WorkspaceReq):
    run(["hyprctl", "dispatch", f"hl.dsp.focus({{ workspace = {req.id} }})"])
    return {"ok": True}


class WindowFocusReq(BaseModel):
    address: str


@app.post("/api/window/focus")
async def focus_window(req: WindowFocusReq):
    # address comes back from /api/state as e.g. "0x55b2..." - hyprctl wants
    # it prefixed like this for focuswindow
    run(["hyprctl", "dispatch", "focuswindow", f"address:{req.address}"])
    return {"ok": True}


@app.post("/api/media/{action}")
async def media_action(action: str):
    if action not in {"play-pause", "next", "previous"}:
        return JSONResponse({"error": "invalid action"}, status_code=400)
    run(["playerctl", "-p", "spotify_player", action])
    return {"ok": True}


class VolumeReq(BaseModel):
    level: int  # 0-100


@app.post("/api/volume")
async def set_volume(req: VolumeReq):
    level = max(0, min(100, req.level))
    run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"])
    return {"ok": True}


@app.post("/api/volume/mute")
async def toggle_mute():
    run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])
    return {"ok": True}


class BrightnessReq(BaseModel):
    level: int  # 0-100


@app.post("/api/brightness")
async def set_brightness(req: BrightnessReq):
    level = max(1, min(100, req.level))
    run(["brightnessctl", "set", f"{level}%"])
    return {"ok": True}


LAUNCH_APPS = {
    "wireshark": ["wireshark"],
    "ghidra": ["ghidra"],
    "audacity": ["audacity"],
    "virt-manager": ["virt-manager"],
    "filemanager": ["dolphin"],  # swap the binary if you use a different file manager
}


@app.post("/api/launch/{name}")
async def launch_app(name: str):
    cmd = LAUNCH_APPS.get(name)
    if not cmd:
        return JSONResponse({"error": "invalid app"}, status_code=400)

    command_str = " ".join(cmd).replace('"', '\\"')
    run(["hyprctl", "dispatch", f'hl.dsp.exec_cmd("{command_str}")'])
    return {"ok": True}


# ---------------------------------------------------------------------------
# websocket - pushes state to every connected iPad the moment it changes
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    await websocket.send_json({"type": "state", "data": get_full_state()})
    try:
        while True:
            # we don't need anything from the client, just keep the socket open
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# live Hyprland events (instant workspace updates instead of polling)
# ---------------------------------------------------------------------------


async def hypr_event_listener():
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not sig:
        return  # not running inside a Hyprland session - polling loop still covers us
    sock_path = f"{runtime_dir}/hypr/{sig}/.socket2.sock"
    while True:
        try:
            reader, _ = await asyncio.open_unix_connection(sock_path)
            while True:
                line = await reader.readline()
                if not line:
                    break
                event = line.decode(errors="ignore").strip()
                if event.startswith(("workspace>>", "activewindow>>", "openwindow>>", "closewindow>>")):
                    await manager.broadcast({"type": "state", "data": get_full_state()})
        except Exception:
            await asyncio.sleep(3)


# fallback poll - catches volume/brightness/media changes made from the PC
# side (keyboard media keys etc.) that don't go through Hyprland's socket
_last_full = None
_cached_stats = {"cpu": 0, "mem": 0, "gpu": None}
_last_stats_time = 0


async def poll_loop():
    global _last_full, _cached_stats, _last_stats_time
    while True:
        now = time.time()
        # heavy stuff (cpu/mem/gpu, window list) only every 2s
        if now - _last_stats_time > 2:
            _cached_stats = get_system_stats()
            _last_stats_time = now

        volume = parse_volume(run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]))
        state = {
            "volume": volume["level"],
            "muted": volume["muted"],
            "brightness": parse_brightness(),
            "media": get_media_state(),
            "system": _cached_stats,
        }

        if state != _last_full:
            # merge with the current workspace/window snapshot so the
            # frontend still gets a complete state object
            full = get_full_state()
            full.update(state)
            await manager.broadcast({"type": "state", "data": full})
            _last_full = state
        await asyncio.sleep(0.2)


@app.on_event("startup")
async def startup():
    asyncio.create_task(hypr_event_listener())
    asyncio.create_task(poll_loop())


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
