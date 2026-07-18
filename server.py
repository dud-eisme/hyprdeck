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
import time
from pathlib import Path
from typing import Set

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


async def run(cmd: list[str]) -> str:
    """Run a command asynchronously, return stdout (empty string on any failure).

    IMPORTANT: this must stay non-blocking. The old version used
    subprocess.run() (blocking) inside async route handlers and the poll
    loop, which froze the whole event loop - including the websocket -
    every time any command was spawned. asyncio.create_subprocess_exec
    lets other requests (and the websocket) keep being served while this
    command is running.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return stdout.decode(errors="ignore").strip()
    except Exception:
        return ""


async def hyprctl_json(args: list[str]):
    out = await run(["hyprctl", "-j", *args])
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
# background task registry
# ---------------------------------------------------------------------------
#
# asyncio.create_task() only stores a WEAK reference to the task inside the
# event loop. If nothing else holds a strong reference to the returned Task
# object, it is eligible for garbage collection at any point - even while
# still pending - and a GC pass triggered by unrelated allocations (like a
# new websocket connection) can silently destroy it. This is a well-known
# asyncio footgun ("Task was destroyed but it is pending!" in the logs is
# the tell). Every long-running background task in this file MUST be
# spawned through spawn_task() below instead of calling
# asyncio.create_task() directly, so a strong reference is kept for the
# lifetime of the task.

_background_tasks: set[asyncio.Task] = set()


def spawn_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

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


async def parse_brightness():
    cur, mx = await asyncio.gather(
        run(["brightnessctl", "g"]),
        run(["brightnessctl", "m"]),
    )
    try:
        return round(int(cur) / int(mx) * 100)
    except Exception:
        return None


async def get_media_state():
    status = await run(["playerctl", "-p", "spotify_player", "status"])
    if not status:
        return None
    title, artist, art_url = await asyncio.gather(
        run(["playerctl", "-p", "spotify_player", "metadata", "title"]),
        run(["playerctl", "-p", "spotify_player", "metadata", "artist"]),
        run(["playerctl", "-p", "spotify_player", "metadata", "mpris:artUrl"]),
    )
    return {"status": status, "title": title, "artist": artist, "art_url": art_url}


async def get_clients():
    """All open windows, raw from hyprctl."""
    return await hyprctl_json(["clients"]) or []


async def get_monitors():
    return await hyprctl_json(["monitors"]) or []


async def get_windows_by_workspace():
    """
    Group open windows by workspace id, with position/size normalized to
    0..1 relative to the monitor that workspace lives on - this is what
    the frontend needs to draw the little "windows inside the workspace
    tile" preview, same idea as the end-4 (ii) overview grid.
    """
    clients, monitors = await asyncio.gather(get_clients(), get_monitors())

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


async def get_gpu_stats():
    # Requires nvidia-smi. If you're not on Nvidia, this just returns None
    # and the frontend hides the GPU row - safe to leave in either way.
    out = await run(
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


async def get_system_stats():
    # cpu/mem read local files synchronously (fast, no subprocess) - only
    # the gpu read needs to be awaited since it shells out to nvidia-smi
    gpu = await get_gpu_stats()
    return {"cpu": get_cpu_percent(), "mem": get_mem_percent(), "gpu": gpu}


async def get_hotspot_status():
    # NOTE: adjust "Hotspot" (nmcli connection name) and "wlo1" (interface)
    # below if yours are named differently.
    state = await run(["nmcli", "-t", "-f", "GENERAL.STATE", "connection", "show", "Hotspot"])
    active = "activated" in state.lower()
    ssid = await run(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "Hotspot"])
    client_count = 0
    if active:
        raw = await run(["iw", "dev", "wlo1", "station", "dump"])
        client_count = sum(1 for line in raw.splitlines() if line.startswith("Station"))
    return {"active": active, "ssid": ssid, "client_count": client_count}


# ---------------------------------------------------------------------------
# background caches for anything that CAN be slow (nvidia-smi waking a GPU
# from idle, playerctl talking to a flaky player, etc). These refresh on
# their own schedule and get_full_state() just reads whatever's cached -
# it never awaits them directly, so a slow nvidia-smi call can no longer
# delay a workspace switch, a volume change, or any other control.
# ---------------------------------------------------------------------------

_cached_system = {"cpu": 0, "mem": 0, "gpu": None}
_cached_media = None


async def system_refresher():
    """Updates _cached_system every 2s. Runs forever, independent of
    everything else - if nvidia-smi hangs, this task just runs slow;
    nothing else waits on it."""
    global _cached_system
    while True:
        try:
            new_stats = await get_system_stats()
            if new_stats != _cached_system:
                _cached_system = new_stats
                await manager.broadcast({"type": "state", "data": await get_full_state(), "sent_at": time.time()})
        except Exception as e:
            print(f"[system_refresher] error: {e}")
        await asyncio.sleep(2)


async def media_refresher():
    """Updates _cached_media every 0.5s - fast enough that song/pause
    changes feel responsive, but still fully decoupled from the fast
    control path."""
    global _cached_media
    while True:
        try:
            new_media = await get_media_state()
            if new_media != _cached_media:
                _cached_media = new_media
                await manager.broadcast({"type": "state", "data": await get_full_state(), "sent_at": time.time()})
        except Exception as e:
            print(f"[media_refresher] error: {e}")
        await asyncio.sleep(0.5)


async def get_full_state():
    """Fast path only: hyprctl/wpctl/brightnessctl calls, all of which are
    reliably quick locally. Slow stuff (system stats, media) is read from
    cache, never awaited here."""
    workspaces, active, vol_raw, windows_by_ws, brightness = await asyncio.gather(
        hyprctl_json(["workspaces"]),
        hyprctl_json(["activeworkspace"]),
        run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]),
        get_windows_by_workspace(),
        parse_brightness(),
    )
    workspaces = workspaces or []
    active = active or {}
    volume = parse_volume(vol_raw)
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
        "brightness": brightness,
        "media": _cached_media,
        "system": _cached_system,
    }


# ---------------------------------------------------------------------------
# action helpers - shared by REST endpoints and websocket commands so the
# logic (clamping, escaping, validation) lives in exactly one place
# ---------------------------------------------------------------------------


async def do_set_workspace(ws_id: int):
    await run(["hyprctl", "dispatch", f"hl.dsp.focus({{ workspace = {ws_id} }})"])


async def do_focus_window(address: str):
    await run(["hyprctl", "dispatch", "focuswindow", f"address:{address}"])


async def do_media_action(action: str) -> bool:
    if action not in {"play-pause", "next", "previous"}:
        return False
    await run(["playerctl", "-p", "spotify_player", action])
    return True


async def do_set_volume(level: int):
    level = max(0, min(100, level))
    await run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"])


async def do_toggle_mute():
    await run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])


async def do_set_brightness(level: int):
    level = max(1, min(100, level))
    await run(["brightnessctl", "set", f"{level}%"])


LAUNCH_APPS = {
    "wireshark": ["wireshark"],
    "ghidra": ["ghidra"],
    "audacity": ["audacity"],
    "virt-manager": ["virt-manager"],
    "filemanager": ["dolphin"],  # swap the binary if you use a different file manager
}


async def do_launch_app(name: str) -> bool:
    cmd = LAUNCH_APPS.get(name)
    if not cmd:
        return False
    command_str = " ".join(cmd).replace('"', '\\"')
    await run(["hyprctl", "dispatch", f'hl.dsp.exec_cmd("{command_str}")'])
    return True


# ---------------------------------------------------------------------------
# REST endpoints (kept working for anything that wants plain HTTP - e.g.
# curl/testing/scripts - but the UI itself now talks over the websocket)
# ---------------------------------------------------------------------------


@app.get("/api/state")
async def get_state():
    return JSONResponse(await get_full_state())


class WorkspaceReq(BaseModel):
    id: int


@app.post("/api/workspace")
async def set_workspace(req: WorkspaceReq):
    await do_set_workspace(req.id)
    return {"ok": True}


class WindowFocusReq(BaseModel):
    address: str


@app.post("/api/window/focus")
async def focus_window(req: WindowFocusReq):
    # address comes back from /api/state as e.g. "0x55b2..." - hyprctl wants
    # it prefixed like this for focuswindow
    await do_focus_window(req.address)
    return {"ok": True}


@app.post("/api/media/{action}")
async def media_action(action: str):
    ok = await do_media_action(action)
    if not ok:
        return JSONResponse({"error": "invalid action"}, status_code=400)
    return {"ok": True}


class VolumeReq(BaseModel):
    level: int  # 0-100


@app.post("/api/volume")
async def set_volume(req: VolumeReq):
    await do_set_volume(req.level)
    return {"ok": True}


@app.post("/api/volume/mute")
async def toggle_mute():
    await do_toggle_mute()
    return {"ok": True}


class BrightnessReq(BaseModel):
    level: int  # 0-100


@app.post("/api/brightness")
async def set_brightness(req: BrightnessReq):
    await do_set_brightness(req.level)
    return {"ok": True}


@app.post("/api/launch/{name}")
async def launch_app(name: str):
    ok = await do_launch_app(name)
    if not ok:
        return JSONResponse({"error": "invalid app"}, status_code=400)
    return {"ok": True}


# ---------------------------------------------------------------------------
# websocket - pushes state to every connected iPad the moment it changes,
# AND now accepts commands from the client so taps don't need a fresh HTTP
# request each time (the socket's already open and warm).
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    await websocket.send_json({"type": "state", "data": await get_full_state()})
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            action = msg.get("type")
            try:
                if action == "workspace":
                    await do_set_workspace(int(msg["id"]))
                elif action == "window_focus":
                    await do_focus_window(str(msg["address"]))
                elif action == "media":
                    await do_media_action(msg["action"])
                elif action == "volume":
                    await do_set_volume(int(msg["level"]))
                elif action == "volume_mute":
                    await do_toggle_mute()
                elif action == "brightness":
                    await do_set_brightness(int(msg["level"]))
                elif action == "launch":
                    await do_launch_app(msg["name"])
                # unknown types are ignored rather than erroring, in case
                # the frontend and server ever drift out of sync
            except Exception:
                # a malformed command shouldn't kill the socket
                continue
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# live Hyprland events (instant workspace updates instead of polling)
# ---------------------------------------------------------------------------


async def hypr_event_listener():
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not sig:
        print("[hypr_event_listener] HYPRLAND_INSTANCE_SIGNATURE not set - listener disabled, relying on poll_loop only", flush=True)
        return  # not running inside a Hyprland session - polling loop still covers us
    sock_path = f"{runtime_dir}/hypr/{sig}/.socket2.sock"
    print(f"[hypr_event_listener] starting, target socket: {sock_path}", flush=True)

    # A single workspace switch/focus change often fires several events in
    # quick succession (workspace>>, activewindow>>, sometimes
    # openwindow>>/closewindow>> too). Firing a full get_full_state() +
    # broadcast per line means several redundant, sequential rebuilds
    # queue up before the final (correct) one lands - visible as lag.
    # Instead: mark "a relevant event happened" and let one debounce task
    # coalesce bursts into a single fetch+broadcast shortly after.
    pending = asyncio.Event()

    async def debounced_broadcaster():
        while True:
            await pending.wait()
            pending.clear()
            try:
                # short window to absorb the rest of the burst - long
                # enough to catch the follow-up events, short enough to
                # feel instant
                await asyncio.sleep(0.03)
                pending.clear()
                await manager.broadcast({"type": "state", "data": await get_full_state()})
            except Exception as e:
                # a broadcast failure must never kill this loop - if it
                # did, workspace updates would silently stop forever
                print(f"[debounced_broadcaster] error: {e}", flush=True)

    spawn_task(debounced_broadcaster())

    while True:
        try:
            reader, _ = await asyncio.open_unix_connection(sock_path)
            print("[hypr_event_listener] connected to hyprland event socket", flush=True)
            while True:
                line = await reader.readline()
                if not line:
                    break
                event = line.decode(errors="ignore").strip()
                if event.startswith(("workspace>>", "activewindow>>", "openwindow>>", "closewindow>>")):
                    pending.set()
        except Exception as e:
            print(f"[hypr_event_listener] connection error: {e}", flush=True)
            await asyncio.sleep(3)


# fallback poll - catches volume/brightness changes made from the PC side
# (keyboard media keys etc.) that don't go through Hyprland's socket.
# media/system are no longer polled here at all - they have their own
# dedicated background refreshers above, so this loop only ever touches
# fast, reliable commands (wpctl/brightnessctl) and can safely stay tight
# at 200ms without risking a slow subprocess blocking anything.
_last_volume_brightness = None


async def poll_loop():
    global _last_volume_brightness
    while True:
        try:
            vol_raw, brightness = await asyncio.gather(
                run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]),
                parse_brightness(),
            )
            volume = parse_volume(vol_raw)
            state = {
                "volume": volume["level"],
                "muted": volume["muted"],
                "brightness": brightness,
            }

            if state != _last_volume_brightness:
                await manager.broadcast({"type": "state", "data": await get_full_state(), "sent_at": time.time()})
                _last_volume_brightness = state
        except Exception as e:
            # a failure here must never kill the whole loop - that would
            # silently stop volume/brightness updates forever
            print(f"[poll_loop] error: {e}")
        await asyncio.sleep(0.2)


@app.on_event("startup")
async def startup():
    spawn_task(hypr_event_listener())
    spawn_task(poll_loop())
    spawn_task(system_refresher())
    spawn_task(media_refresher())


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
