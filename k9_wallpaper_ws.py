"""
k9_wallpaper_ws.py — K-9 Wallpaper WebSocket Bridge
─────────────────────────────────────────────────────────────────────────────
Sprint: Wallpaper-WS-1
Port:   8790

Purpose:
  Bridges WSL2 service events (BOOT_COMPLETE, STATUS_UPDATE, ALERT) to the
  Windows-side wallpaper.html overlay via WebSocket.

  wallpaper.html connects to ws://localhost:8790/ws and receives JSON events.
  The launch script POSTs to /boot-complete after all services are up.

Endpoints:
  GET  /health           — liveness check
  GET  /ws               — WebSocket connection (for wallpaper.html)
  POST /boot-complete    — trigger BOOT_COMPLETE broadcast to all overlay clients
  POST /event            — broadcast any named event to all overlay clients
  GET  /clients          — count of connected overlay clients

Usage (wallpaper.html):
  const ws = new WebSocket('ws://localhost:8790/ws');
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.event === 'BOOT_COMPLETE') initSetupTab();
    if (msg.event === 'STATUS_UPDATE') updatePanel(msg.data);
  };
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="[K9-WS] %(levelname)s %(message)s")
log = logging.getLogger("k9-wallpaper-ws")

app = FastAPI(title="K-9 Wallpaper WebSocket Bridge", version="1.0.0")

# Allow connections from Windows-side file:// origin and localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # wallpaper.html loads as file:// — needs wildcard
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Connected clients ─────────────────────────────────────────────────────────

connected_clients: Set[WebSocket] = set()
boot_complete_fired: bool = False
last_status: dict = {}


async def broadcast(payload: dict):
    """Send JSON payload to all connected overlay clients."""
    if not connected_clients:
        log.info("No overlay clients connected — event queued in last_status")
        return
    dead = set()
    msg = json.dumps(payload)
    for ws in connected_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)
    log.info(f"Broadcast {payload.get('event')} → {len(connected_clients)} client(s)")


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def wallpaper_ws(websocket: WebSocket):
    """Overlay clients connect here to receive stack events."""
    await websocket.accept()
    connected_clients.add(websocket)
    client = websocket.client
    log.info(f"Overlay connected: {client} ({len(connected_clients)} total)")

    # If BOOT_COMPLETE already fired before overlay connected — replay it
    if boot_complete_fired:
        await websocket.send_text(json.dumps({
            "event": "BOOT_COMPLETE",
            "replayed": True,
            "timestamp": datetime.utcnow().isoformat(),
            "stack": "k9-economic",
        }))

    # Replay last status if available
    if last_status:
        await websocket.send_text(json.dumps({
            "event": "STATUS_UPDATE",
            "data": last_status,
            "timestamp": datetime.utcnow().isoformat(),
        }))

    try:
        while True:
            # Keep connection alive — client pings are ignored
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.discard(websocket)
        log.info(f"Overlay disconnected: {client} ({len(connected_clients)} remaining)")


# ── HTTP trigger endpoints ────────────────────────────────────────────────────

class BootPayload(BaseModel):
    event: str = "BOOT_COMPLETE"
    stack: str = "k9-economic"
    version: str = "AEG-5"


class EventPayload(BaseModel):
    event: str
    data: dict = {}


@app.post("/boot-complete")
async def trigger_boot_complete(payload: BootPayload = BootPayload()):
    """Called by launch-economic-stack.sh after all services are up."""
    global boot_complete_fired
    boot_complete_fired = True

    msg = {
        "event": "BOOT_COMPLETE",
        "stack": payload.stack,
        "version": payload.version,
        "timestamp": datetime.utcnow().isoformat(),
        "clients": len(connected_clients),
    }
    await broadcast(msg)
    log.info("BOOT_COMPLETE fired ✅")
    return {"status": "ok", "clients_notified": len(connected_clients)}


@app.post("/event")
async def send_event(payload: EventPayload):
    """Broadcast any arbitrary event to all overlay clients."""
    global last_status
    msg = {
        "event": payload.event,
        "data": payload.data,
        "timestamp": datetime.utcnow().isoformat(),
    }
    if payload.event == "STATUS_UPDATE":
        last_status = payload.data
    await broadcast(msg)
    return {"status": "ok", "clients_notified": len(connected_clients)}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "k9-wallpaper-ws",
        "port": 8790,
        "connected_clients": len(connected_clients),
        "boot_complete_fired": boot_complete_fired,
    }


@app.get("/clients")
async def clients():
    return {"count": len(connected_clients), "boot_complete_fired": boot_complete_fired}


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("WALLPAPER_WS_PORT", "8790"))
    log.info(f"K-9 Wallpaper WS Bridge starting on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
