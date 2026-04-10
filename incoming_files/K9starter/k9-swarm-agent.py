"""
k9-swarm-agent.py
─────────────────────────────────────────────────────────────────────────────
K-9 SWARM AGENT — Sprint 3
Giant Steps Framework · Phase 1 → Phase 2 bridge

Each agent in the K-9 swarm:
  • Registers its identity and capabilities with the Headscale control plane
  • Communicates with peers over the Tailscale mesh (WireGuard encrypted)
  • Broadcasts health/status via a gossip-ready message structure
  • Accepts and processes structured FIPA-lite ACL messages
  • Exposes a local HTTP API for the K-9 dashboard to query

Architecture:
  SwarmAgent
  ├── HeadscaleClient     — control plane registration + peer discovery
  ├── MeshMessenger       — peer-to-peer messaging over Tailscale IP
  ├── GossipEmitter       — periodic status broadcast (Sprint 7 hook)
  ├── MessageHandler      — FIPA-lite ACL message dispatch
  └── AgentServer         — local FastAPI server for dashboard queries

Usage:
  # Activate your component venv first
  k9-orchestrator

  # Run with defaults (auto-detects Tailscale IP)
  python k9-swarm-agent.py --role orchestrator

  # Run a second agent on another device
  python k9-swarm-agent.py --role llm-router --port 8765

  # Point at your Headscale instance
  python k9-swarm-agent.py --role quant-engine --headscale http://100.x.x.1:8080

Dependencies:
  pip install fastapi uvicorn httpx python-dotenv --break-system-packages
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import random
import socket
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("k9-agent")


# ── ENUMS ────────────────────────────────────────────────────────────────────

class AgentRole(str, Enum):
    ORCHESTRATOR  = "orchestrator"
    LLM_ROUTER    = "llm-router"
    QUANT_ENGINE  = "quant-engine"
    ORBITRON_DIAG = "orbitron-diag"
    MCP_SERVER    = "mcp-server"
    DEVICE_HUB    = "device-hub"


class AgentStatus(str, Enum):
    INITIALIZING = "initializing"
    ONLINE       = "online"
    IDLE         = "idle"
    BUSY         = "busy"
    DEGRADED     = "degraded"
    OFFLINE      = "offline"


class MessageType(str, Enum):
    # FIPA-lite performatives
    INFORM    = "inform"     # share information
    REQUEST   = "request"    # ask for action
    PROPOSE   = "propose"    # offer negotiation
    ACCEPT    = "accept"     # accept proposal
    REJECT    = "reject"     # reject proposal
    QUERY     = "query"      # ask for state
    HEARTBEAT = "heartbeat"  # gossip health ping


# ── DATA MODELS ──────────────────────────────────────────────────────────────

@dataclass
class AgentIdentity:
    """Cryptographic + network identity for a swarm agent."""
    agent_id:   str = field(default_factory=lambda: str(uuid.uuid4()))
    role:       AgentRole = AgentRole.ORCHESTRATOR
    hostname:   str = field(default_factory=socket.gethostname)
    tailscale_ip: str = ""
    port:       int = 8744
    capabilities: list[str] = field(default_factory=list)
    registered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def address(self) -> str:
        return f"http://{self.tailscale_ip}:{self.port}"


@dataclass
class AgentHealth:
    """Live health snapshot — broadcast via gossip."""
    agent_id:   str = ""
    status:     AgentStatus = AgentStatus.ONLINE
    cpu_pct:    float = 0.0
    mem_pct:    float = 0.0
    active_tasks: int = 0
    last_seen:  str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    uptime_s:   float = 0.0
    sprint:     int = 3          # current Giant Steps sprint
    phase:      int = 1          # current Giant Steps phase


@dataclass
class SwarmMessage:
    """
    FIPA-lite ACL message — the lingua franca of the K-9 swarm.

    Future: Sprint 7 adds gossip broadcast.
                Sprint 11 adds DAO signature verification.
    """
    msg_id:      str = field(default_factory=lambda: str(uuid.uuid4()))
    sender_id:   str = ""
    receiver_id: str = ""        # empty = broadcast
    performative: MessageType = MessageType.INFORM
    content:     dict[str, Any] = field(default_factory=dict)
    timestamp:   str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    reply_to:    str | None = None
    # Sprint 12 hook: reputation weight of sender (0.0–1.0)
    rep_weight:  float = 1.0


@dataclass
class SwarmPeer:
    """A known peer in the swarm."""
    identity: AgentIdentity
    health:   AgentHealth
    last_contact: float = field(default_factory=time.time)

    @property
    def reachable(self) -> bool:
        return (time.time() - self.last_contact) < 30.0


# ── HEADSCALE CLIENT ─────────────────────────────────────────────────────────

class HeadscaleClient:
    """
    Thin wrapper around the Headscale management API.

    Headscale docs: https://headscale.net/ref/remote-cli/
    API base:       GET /api/v1/node  →  list all nodes
                    GET /api/v1/node/{id}  →  single node
    """

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def list_nodes(self) -> list[dict]:
        """Return all registered Tailscale nodes from Headscale."""
        async with httpx.AsyncClient(timeout=5) as c:
            try:
                r = await c.get(f"{self.base_url}/api/v1/node", headers=self.headers)
                r.raise_for_status()
                return r.json().get("nodes", [])
            except Exception as e:
                log.warning("Headscale unreachable: %s", e)
                return []

    async def get_node_by_hostname(self, hostname: str) -> dict | None:
        nodes = await self.list_nodes()
        return next((n for n in nodes if n.get("name") == hostname), None)

    async def register_agent(self, identity: AgentIdentity) -> bool:
        """
        Headscale handles WireGuard registration automatically via the
        Tailscale client — this method posts agent metadata to K-9's own
        agent registry (not Headscale directly).

        Sprint 14 replaces this with a DHT write.
        """
        log.info(
            "Agent %s (%s) registering | tailscale_ip=%s",
            identity.agent_id[:8],
            identity.role,
            identity.tailscale_ip,
        )
        return True


# ── TAILSCALE HELPERS ─────────────────────────────────────────────────────────

def get_tailscale_ip() -> str:
    """
    Discover this machine's Tailscale (100.x.x.x) IP.
    Falls back to LAN IP if Tailscale is not running.
    """
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3
        )
        ip = result.stdout.strip()
        if ip:
            return ip
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: local IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ── MESH MESSENGER ────────────────────────────────────────────────────────────

class MeshMessenger:
    """
    Peer-to-peer messaging over the Tailscale mesh.

    Sends SwarmMessage objects to peer agents via their HTTP API.
    Sprint 4 upgrade: replace HTTP with raw WireGuard UDP when we own
    the transport layer.
    """

    def __init__(self, identity: AgentIdentity) -> None:
        self.identity = identity
        self._sent = 0
        self._failed = 0

    async def send(self, peer: SwarmPeer, msg: SwarmMessage) -> bool:
        """Send a message to a specific peer."""
        msg.sender_id = self.identity.agent_id
        msg.receiver_id = peer.identity.agent_id
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                r = await c.post(
                    f"{peer.identity.address}/swarm/message",
                    json=asdict(msg),
                )
                r.raise_for_status()
                self._sent += 1
                log.debug("→ %s [%s] to %s", msg.performative, msg.msg_id[:8], peer.identity.role)
                return True
        except Exception as e:
            self._failed += 1
            log.warning("Message to %s failed: %s", peer.identity.role, e)
            return False

    async def broadcast(self, peers: list[SwarmPeer], msg: SwarmMessage) -> int:
        """
        Broadcast to all peers concurrently.
        Sprint 7: replace with gossip fan-out (send to random subset).
        """
        results = await asyncio.gather(
            *[self.send(p, msg) for p in peers], return_exceptions=True
        )
        return sum(1 for r in results if r is True)

    @property
    def stats(self) -> dict:
        return {"sent": self._sent, "failed": self._failed}


# ── GOSSIP EMITTER ────────────────────────────────────────────────────────────

class GossipEmitter:
    """
    Periodic health broadcast to a random subset of peers.

    Sprint 3: simple round-robin broadcast to all known peers.
    Sprint 7: implement true gossip fan-out with failure detection.
    Sprint 8: add DHT-backed membership list.
    """

    def __init__(
        self,
        messenger: MeshMessenger,
        interval_s: float = 10.0,
        fanout: int = 3,
    ) -> None:
        self.messenger = messenger
        self.interval  = interval_s
        self.fanout    = fanout        # Sprint 7: only gossip to N random peers
        self._running  = False

    async def start(self, get_peers: Any, get_health: Any) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self.interval)
            peers = get_peers()
            if not peers:
                continue

            # Sprint 3: broadcast to all
            # Sprint 7: replace with random.sample(peers, min(self.fanout, len(peers)))
            targets = peers

            health = get_health()
            msg = SwarmMessage(
                performative=MessageType.HEARTBEAT,
                content=asdict(health),
            )
            sent = await self.messenger.broadcast(targets, msg)
            log.info("Gossip heartbeat → %d/%d peers", sent, len(targets))

    def stop(self) -> None:
        self._running = False


# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────

class MessageHandler:
    """
    Routes incoming SwarmMessages to handler functions by performative.

    Register handlers with @agent.on(MessageType.REQUEST) decorator pattern.
    Sprint 11: add DAO signature verification before dispatching.
    Sprint 12: weight handler priority by sender reputation.
    """

    def __init__(self) -> None:
        self._handlers: dict[MessageType, list] = {mt: [] for mt in MessageType}
        self._inbox:   list[SwarmMessage] = []

    def register(self, performative: MessageType, fn: Any) -> None:
        self._handlers[performative].append(fn)

    async def dispatch(self, msg: SwarmMessage) -> dict:
        self._inbox.append(msg)
        handlers = self._handlers.get(msg.performative, [])
        results = []
        for fn in handlers:
            try:
                result = await fn(msg) if asyncio.iscoroutinefunction(fn) else fn(msg)
                results.append(result)
            except Exception as e:
                log.error("Handler error for %s: %s", msg.performative, e)
        return {"dispatched": len(handlers), "results": results}

    @property
    def inbox_size(self) -> int:
        return len(self._inbox)


# ── SWARM AGENT ───────────────────────────────────────────────────────────────

class SwarmAgent:
    """
    K-9 Swarm Agent — the fundamental unit of the K-9 distributed system.

    One instance runs per K-9 component (orchestrator, llm-router, etc.).
    Agents discover each other, exchange health via gossip, coordinate tasks
    via FIPA-lite messages, and eventually self-govern via DAO smart contracts.

    Lifecycle:
      agent = SwarmAgent(role=AgentRole.ORCHESTRATOR)
      await agent.start()       # registers, starts gossip, binds API
      await agent.stop()        # clean shutdown
    """

    def __init__(
        self,
        role: AgentRole = AgentRole.ORCHESTRATOR,
        port: int = 8744,
        headscale_url: str | None = None,
        capabilities: list[str] | None = None,
    ) -> None:
        self._start_time = time.time()

        # Identity
        self.identity = AgentIdentity(
            role=role,
            tailscale_ip=get_tailscale_ip(),
            port=port,
            capabilities=capabilities or self._default_capabilities(role),
        )

        # Core subsystems
        self.headscale = HeadscaleClient(
            base_url=headscale_url or os.getenv("HEADSCALE_URL", "http://localhost:8080"),
            api_key=os.getenv("HEADSCALE_API_KEY"),
        )
        self.messenger = MeshMessenger(self.identity)
        self.gossip    = GossipEmitter(self.messenger)
        self.handler   = MessageHandler()

        # Swarm state
        self._peers:  dict[str, SwarmPeer] = {}
        self._status: AgentStatus = AgentStatus.INITIALIZING
        self._tasks:  int = 0

        # FastAPI app
        self.app = self._build_api()

        # Register default handlers
        self._register_default_handlers()

        log.info(
            "SwarmAgent created | role=%s id=%s ip=%s port=%d",
            self.identity.role,
            self.identity.agent_id[:8],
            self.identity.tailscale_ip,
            self.identity.port,
        )

    # ── Capabilities ────────────────────────────────────────────────────────

    @staticmethod
    def _default_capabilities(role: AgentRole) -> list[str]:
        caps = {
            AgentRole.ORCHESTRATOR:  ["routing", "task-dispatch", "gossip", "policy-eval"],
            AgentRole.LLM_ROUTER:    ["inference", "model-routing", "context-cache"],
            AgentRole.QUANT_ENGINE:  ["black-scholes", "monte-carlo", "defi-pricing"],
            AgentRole.ORBITRON_DIAG: ["obd2", "dtc-lookup", "vehicle-telemetry"],
            AgentRole.MCP_SERVER:    ["mcp-tools", "agent-registry", "dht-node"],
            AgentRole.DEVICE_HUB:    ["relay", "wireguard", "device-mesh"],
        }
        return caps.get(role, [])

    # ── Health ───────────────────────────────────────────────────────────────

    def get_health(self) -> AgentHealth:
        return AgentHealth(
            agent_id=self.identity.agent_id,
            status=self._status,
            cpu_pct=self._sample_cpu(),
            mem_pct=self._sample_mem(),
            active_tasks=self._tasks,
            last_seen=datetime.now(timezone.utc).isoformat(),
            uptime_s=time.time() - self._start_time,
        )

    @staticmethod
    def _sample_cpu() -> float:
        try:
            import psutil
            return psutil.cpu_percent(interval=None)
        except ImportError:
            return round(random.uniform(5.0, 40.0), 1)   # mock if psutil absent

    @staticmethod
    def _sample_mem() -> float:
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            return round(random.uniform(20.0, 60.0), 1)

    # ── Peer management ─────────────────────────────────────────────────────

    def get_peers(self) -> list[SwarmPeer]:
        return list(self._peers.values())

    def register_peer(self, identity: AgentIdentity, health: AgentHealth) -> None:
        peer = SwarmPeer(identity=identity, health=health)
        self._peers[identity.agent_id] = peer
        log.info("Peer registered: %s @ %s", identity.role, identity.tailscale_ip)

    def update_peer_health(self, agent_id: str, health: AgentHealth) -> None:
        if agent_id in self._peers:
            self._peers[agent_id].health = health
            self._peers[agent_id].last_contact = time.time()

    # ── Default message handlers ─────────────────────────────────────────────

    def _register_default_handlers(self) -> None:

        async def handle_heartbeat(msg: SwarmMessage) -> dict:
            health = AgentHealth(**msg.content)
            self.update_peer_health(msg.sender_id, health)
            return {"ack": True, "peer": msg.sender_id[:8]}

        async def handle_query(msg: SwarmMessage) -> dict:
            return asdict(self.get_health())

        async def handle_request(msg: SwarmMessage) -> dict:
            action = msg.content.get("action", "unknown")
            log.info("REQUEST from %s: action=%s", msg.sender_id[:8], action)
            # Sprint 11 hook: check DAO membership before executing
            return {"status": "accepted", "action": action}

        self.handler.register(MessageType.HEARTBEAT, handle_heartbeat)
        self.handler.register(MessageType.QUERY, handle_query)
        self.handler.register(MessageType.REQUEST, handle_request)

    # ── FastAPI ──────────────────────────────────────────────────────────────

    def _build_api(self) -> FastAPI:
        app = FastAPI(
            title=f"K-9 Swarm Agent — {self.identity.role}",
            version="0.3.0",
            description="Sprint 3 swarm agent API. Consumed by K-9 mobile dashboard.",
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/")
        async def root():
            return {
                "agent": self.identity.role,
                "id": self.identity.agent_id[:8],
                "sprint": 3,
                "phase": 1,
            }

        @app.get("/swarm/identity")
        async def identity():
            return asdict(self.identity)

        @app.get("/swarm/health")
        async def health():
            return asdict(self.get_health())

        @app.get("/swarm/peers")
        async def peers():
            return {
                "count": len(self._peers),
                "peers": [
                    {
                        "id":       p.identity.agent_id[:8],
                        "role":     p.identity.role,
                        "ip":       p.identity.tailscale_ip,
                        "status":   p.health.status,
                        "reachable": p.reachable,
                    }
                    for p in self._peers.values()
                ],
            }

        @app.post("/swarm/message")
        async def receive_message(payload: dict):
            try:
                msg = SwarmMessage(**payload)
                result = await self.handler.dispatch(msg)
                return {"ok": True, **result}
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

        @app.post("/swarm/peer/register")
        async def register_peer(payload: dict):
            try:
                identity = AgentIdentity(**payload["identity"])
                health   = AgentHealth(**payload["health"])
                self.register_peer(identity, health)
                return {"ok": True, "registered": identity.agent_id[:8]}
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

        @app.get("/swarm/stats")
        async def stats():
            return {
                "messenger": self.messenger.stats,
                "inbox_size": self.handler.inbox_size,
                "peer_count": len(self._peers),
                "uptime_s": round(time.time() - self._start_time, 1),
                "sprint": 3,
                "next_sprint": "Sprint 7 — gossip fan-out",
            }

        return app

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        log.info("Starting K-9 SwarmAgent [%s]...", self.identity.role)

        # 1. Headscale registration
        await self.headscale.register_agent(self.identity)

        # 2. Discover existing peers from Headscale node list
        nodes = await self.headscale.list_nodes()
        log.info("Headscale reports %d mesh nodes", len(nodes))

        # 3. Start gossip emitter
        asyncio.create_task(
            self.gossip.start(self.get_peers, self.get_health),
            name="gossip-emitter",
        )

        self._status = AgentStatus.ONLINE
        log.info(
            "SwarmAgent ONLINE | %s @ %s:%d",
            self.identity.role,
            self.identity.tailscale_ip,
            self.identity.port,
        )

    async def stop(self) -> None:
        self.gossip.stop()
        self._status = AgentStatus.OFFLINE
        log.info("SwarmAgent %s stopping.", self.identity.role)

    def run(self) -> None:
        """Blocking entry point — starts agent + uvicorn API server."""
        async def _run():
            await self.start()
            config = uvicorn.Config(
                self.app,
                host="0.0.0.0",
                port=self.identity.port,
                log_level="warning",
            )
            server = uvicorn.Server(config)
            await server.serve()

        asyncio.run(_run())


# ── CLI ENTRY POINT ───────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="K-9 Swarm Agent — Sprint 3")
    parser.add_argument(
        "--role",
        default="orchestrator",
        choices=[r.value for r in AgentRole],
        help="Agent role (maps to K-9 component)",
    )
    parser.add_argument("--port", type=int, default=8744)
    parser.add_argument(
        "--headscale",
        default=None,
        help="Headscale API URL (default: $HEADSCALE_URL or http://localhost:8080)",
    )
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════╗
║           K-9 SWARM AGENT  ·  Sprint 3              ║
║   Giant Steps Framework — Phase 1 → 2 bridge        ║
╚══════════════════════════════════════════════════════╝
  Role       : {args.role}
  Port       : {args.port}
  Tailscale  : {get_tailscale_ip()}
  Headscale  : {args.headscale or os.getenv('HEADSCALE_URL', 'http://localhost:8080')}

  Dashboard  : http://{get_tailscale_ip()}:{args.port}/swarm/health
  Peers API  : http://{get_tailscale_ip()}:{args.port}/swarm/peers
  API docs   : http://{get_tailscale_ip()}:{args.port}/docs

  Press Ctrl+C to stop.
""")

    agent = SwarmAgent(
        role=AgentRole(args.role),
        port=args.port,
        headscale_url=args.headscale,
    )
    agent.run()


if __name__ == "__main__":
    main()
