# python_server.py (UNIFIED)
# aiohttp HTTP + WebSocket (/ws) + Twitch bot + vote rounds + placement + mouse events to game
# ✅ Added JSONL logging for:
#   - spawn_item (from Twitch !place and overlay click)
#   - mouse_click (when terminate=True)
# Keeps your existing mouse_event logging too.

import os
import ssl
import json
import time
import asyncio
import contextlib
import random
import socket
import struct
from pathlib import Path
from typing import Dict, Set, Any, Optional

import certifi
import aiohttp
from aiohttp import web, TCPConnector
from aiohttp_cors import setup, ResourceOptions
from twitchio.ext import commands

# =========================
# Protobuf (optional)
# =========================
HAS_PROTOBUF = False
message_pb2 = None
try:
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src_python")))
    import message_pb2  # type: ignore
    HAS_PROTOBUF = True
except Exception:
    HAS_PROTOBUF = False
    print("[WARNING] message_pb2 not found, mouse events won't be sent to game")

# =========================
# SSL FIX FOR WINDOWS (TwitchIO)
# =========================
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
_ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl._create_default_https_context = lambda: _ssl_context  # noqa

# =========================
# Configuration
# =========================
HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8080

ROUND_DURATION = 30
ROUND_BREAK_SECONDS = 15
PLACEMENT_TIMEOUT = 15

TWITCH_TOKEN = os.environ.get("TWITCH_TOKEN", "oauth:PUT_YOUR_TOKEN").strip()
TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "your_channel_here").strip()

LOG_FILE = "spawn_log.jsonl"

ITEMS: Dict[str, Dict[str, str]] = {
    "freeze":  {"emoji": "🧊", "label": "Freeze Orb"},
    "fire":    {"emoji": "🔥", "label": "Power Core"},
    "wind":    {"emoji": "💨", "label": "Wind Boots"},
    "shield":  {"emoji": "🧱", "label": "Shield Stone"},
    "chaos":   {"emoji": "🎭", "label": "Chaos Mask"},
    "warp":    {"emoji": "🌀", "label": "Space Warp"},
    "bomb":    {"emoji": "⏳", "label": "Time Bomb"},
    "spout":   {"emoji": "🌋", "label": "Flame Spout"},
    "gravity": {"emoji": "🌑", "label": "Gravity Well"},
    "shock":   {"emoji": "⚡", "label": "Shock Pulse"},
    "tornado": {"emoji": "🌪️", "label": "Micro Tornado"},
    "meteor":  {"emoji": "💥", "label": "Impact Meteor"},
    "luck":    {"emoji": "🌈", "label": "Luck Capsule"},
    "seed":    {"emoji": "🌿", "label": "Growth Seed"},
}

# =========================
# Files
# =========================
BASE_DIR = Path(__file__).resolve().parent
OVERLAY_PATH = BASE_DIR / "overlay.html"
OVERLAY_JS_PATH = BASE_DIR / "overlay.js"

# =========================
# Global state
# =========================
clients: Set[web.WebSocketResponse] = set()

# ✅ WS -> twitch_user_id map (so we can authorize winner)
ws_user_id: Dict[web.WebSocketResponse, Optional[str]] = {}

BOT_INSTANCE: Optional["ChatBot"] = None

current_round_active: bool = False
current_round_id: int = 0
current_votes: Dict[str, int] = {k: 0 for k in ITEMS.keys()}

# ✅ Track voters by item using Twitch user_id (not only names)
votes_by_item_ids: Dict[str, Set[str]] = {k: set() for k in ITEMS.keys()}
user_id_to_name: Dict[str, str] = {}  # id -> latest name

round_end_ts: float = 0.0

# pending placement:
# { "round_id": int, "item_key": str, "chosen_user": str, "chosen_user_id": str, "ts": float }
pending_placement: Optional[Dict[str, Any]] = None

# Game socket
GAME_SOCKET: Optional[socket.socket] = None
GAME_EVENT_ID: int = 0

# =========================
# Logging
# =========================
def log_jsonl(event: Dict[str, Any]) -> None:
    data = dict(event)
    data["ts"] = time.time()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[LOG] Error writing JSONL:", e)

# =========================
# Game socket + protobuf
# =========================
def connect_game_socket() -> None:
    global GAME_SOCKET
    try:
        if GAME_SOCKET:
            try:
                GAME_SOCKET.close()
            except Exception:
                pass
        GAME_SOCKET = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        GAME_SOCKET.connect(("127.0.0.1", 31415))
        GAME_SOCKET.send(b"\x06")
        print("[GAME] Connected to game server at localhost:31415")
    except Exception as e:
        print(f"[GAME] Connection failed: {e}")
        GAME_SOCKET = None

def send_game_event(event_type: int, x: int, y: int, vx: int, vy: int, hx: int, hy: int, terminate: bool) -> None:
    global GAME_SOCKET, GAME_EVENT_ID

    if not HAS_PROTOBUF or not GAME_SOCKET:
        return

    try:
        event = message_pb2.GrpcGameEvent()
        event.event_id = GAME_EVENT_ID
        event.event_type = event_type
        event.x = x
        event.y = y
        event.vx = vx
        event.vy = vy
        event.hx = hx
        event.hy = hy
        event.time = 180
        event.terminate = terminate

        data = event.SerializeToString()
        GAME_SOCKET.send(struct.pack("<I", len(data)))
        GAME_SOCKET.send(data)

        if terminate:
            GAME_EVENT_ID += 1

    except Exception as e:
        print(f"[GAME] Send failed: {e}")
        GAME_SOCKET = None
        connect_game_socket()

# =========================
# Helpers
# =========================
def compute_remaining_seconds() -> int:
    if not current_round_active:
        return 0
    remain = int(round_end_ts - time.time())
    return max(0, remain)

def build_state_payload() -> dict:
    return {
        "type": "state",
        "round": {
            "active": current_round_active,
            "round_id": current_round_id,
            "duration_remaining": compute_remaining_seconds(),
        },
        "options": [{"key": k, "emoji": v["emoji"], "label": v["label"]} for k, v in ITEMS.items()],
        "votes": current_votes,
        "pending_placement": pending_placement,
    }

async def broadcast_ws(data: dict) -> None:
    if not clients:
        return
    msg = json.dumps(data, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
        ws_user_id.pop(ws, None)

async def send_state(ws: web.WebSocketResponse) -> None:
    await ws.send_str(json.dumps(build_state_payload(), ensure_ascii=False))

async def send_chat_message(message: str) -> None:
    global BOT_INSTANCE
    if not BOT_INSTANCE:
        return
    try:
        channel = BOT_INSTANCE.get_channel(TWITCH_CHANNEL)
        if channel:
            await channel.send(message)
    except Exception as e:
        print(f"[CHAT ERROR] Failed to send message: {e}")

def place_item_in_game(item_key: str, slot: str) -> None:
    slot_map = {"left": (160, 320), "middle": (480, 320), "right": (800, 320)}
    x, y = slot_map[slot]
    item_idx = list(ITEMS.keys()).index(item_key)
    send_game_event(item_idx, x, y, 0, 0, 100, 100, True)

# =========================
# Twitch bot
# =========================
class ChatBot(commands.Bot):
    def __init__(self):
        self._custom_connector = TCPConnector(ssl=False)
        self._custom_session = aiohttp.ClientSession(connector=self._custom_connector)

        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )
        self._http.session = self._custom_session

    async def event_ready(self):
        print(f"--- BOT CONNECTED: {self.nick} --- (channel: {TWITCH_CHANNEL})")

    async def event_message(self, message):
        author_name = message.author.name if message.author else "Unknown"
        print(f"[CHAT] {author_name}: {message.content}")

        if not message.author or getattr(message, "echo", False):
            return
        await self.handle_commands(message)

    @commands.command(name="items")
    async def items_command(self, ctx: commands.Context):
        items_list = " | ".join([f"{v['emoji']} {k}" for k, v in ITEMS.items()])
        await ctx.send(f"Available items: {items_list} | Vote with: !item <name>")

    @commands.command(name="item")
    async def item_command(self, ctx: commands.Context):
        global current_votes, current_round_active, votes_by_item_ids, user_id_to_name

        parts = ctx.message.content.split(maxsplit=1)
        if len(parts) < 2:
            await ctx.send("Usage: !item <item_key>")
            return

        choice = parts[1].strip().lower()
        if choice not in ITEMS:
            await ctx.send(f"Unknown item. Try: {', '.join(ITEMS.keys())}")
            return

        if not current_round_active:
            await ctx.send("No vote running right now. Wait for the next round!")
            return

        author = ctx.author
        user_name = author.name if author else "Unknown"
        user_id = str(getattr(author, "id", "")) if author else ""

        if not user_id:
            return

        # remember name
        user_id_to_name[user_id] = user_name

        # ✅ Prevent multiple votes per round (one vote per user)
        all_voters = set().union(*votes_by_item_ids.values())
        if user_id in all_voters:
            return

        current_votes[choice] = current_votes.get(choice, 0) + 1
        votes_by_item_ids.setdefault(choice, set()).add(user_id)

        await broadcast_ws({
            "type": "vote",
            "user": user_name,
            "user_id": user_id,
            "item": choice,
            "emoji": ITEMS[choice]["emoji"],
            "count": current_votes[choice],
        })

    @commands.command(name="place")
    async def place_command(self, ctx: commands.Context):
        global pending_placement

        if not pending_placement:
            return

        author = ctx.author
        user_id = str(getattr(author, "id", "")) if author else ""
        user_name = author.name if author else "Unknown"

        # ✅ Only winner can place
        if not user_id or user_id != str(pending_placement.get("chosen_user_id", "")):
            await ctx.send(f"Wait your turn! Only @{pending_placement['chosen_user']} can place this item.")
            return

        parts = ctx.message.content.split()
        if len(parts) < 2:
            return

        raw_slot = parts[1].strip().lower()
        if raw_slot not in ("left", "middle", "right"):
            return

        item_key = pending_placement["item_key"]

        # ✅ LOG spawn_item (Twitch chat placement)
        log_jsonl({
            "type": "spawn_item",
            "round_id": pending_placement["round_id"],
            "item_key": item_key,
            "emoji": ITEMS[item_key]["emoji"],
            "label": ITEMS[item_key]["label"],
            "chosen_user": user_name,
            "slot": raw_slot,
        })

        place_item_in_game(item_key, raw_slot)

        await ctx.send(f"✅ Item placed at {raw_slot.upper()} by @{user_name}!")

        # ✅ Everyone sees the choice
        await broadcast_ws({
            "type": "place_update",
            "chosen_user": pending_placement["chosen_user"],
            "chosen_user_id": pending_placement["chosen_user_id"],
            "place": raw_slot,
        })

        pending_placement = None
        await broadcast_ws({"type": "placement_complete"})

# =========================
# Rounds loop
# =========================
async def rounds_loop():
    global current_round_active, current_votes, current_round_id, votes_by_item_ids, pending_placement, round_end_ts

    await asyncio.sleep(3)

    while True:
        current_round_id += 1
        print(f"=== Starting round {current_round_id} ===")

        current_votes = {k: 0 for k in ITEMS.keys()}
        votes_by_item_ids = {k: set() for k in ITEMS.keys()}
        pending_placement = None

        current_round_active = True
        round_end_ts = time.time() + ROUND_DURATION

        items_list = " | ".join([f"{v['emoji']} {k}" for k, v in ITEMS.items()])
        await send_chat_message(
            f"🎮 Round {current_round_id} starts! Vote with: !item <name> | {ROUND_DURATION}s | {items_list}"
        )

        await broadcast_ws({
            "type": "round_start",
            "round_id": current_round_id,
            "duration": ROUND_DURATION,
            "options": [{"key": k, "emoji": v["emoji"], "label": v["label"]} for k, v in ITEMS.items()],
        })

        for remaining in (20, 10, 5):
            await asyncio.sleep(max(0, round_end_ts - time.time() - remaining))
            if current_round_active and remaining > 0:
                await send_chat_message(f"⏰ {remaining} seconds left to vote!")

        await asyncio.sleep(max(0, round_end_ts - time.time()))
        current_round_active = False
        round_end_ts = 0.0

        max_votes = max(current_votes.values()) if current_votes else 0
        winner_key = max(current_votes, key=lambda k: current_votes[k]) if max_votes > 0 else None

        winner = None
        if winner_key:
            winner = {
                "key": winner_key,
                "emoji": ITEMS[winner_key]["emoji"],
                "label": ITEMS[winner_key]["label"],
                "votes": current_votes[winner_key],
            }

        await broadcast_ws({
            "type": "round_result",
            "round_id": current_round_id,
            "winner": winner,
            "votes": current_votes,
        })

        if not winner_key:
            await send_chat_message(f"❌ Round {current_round_id} ended with no votes. Next round in {ROUND_BREAK_SECONDS}s...")
            await asyncio.sleep(ROUND_BREAK_SECONDS)
            continue

        vote_summary = " | ".join([f"{ITEMS[k]['emoji']} {v}" for k, v in current_votes.items() if v > 0])
        await send_chat_message(f"📊 Votes: {vote_summary}")

        # ✅ Choose winner among voters (by user_id)
        voter_ids = list(votes_by_item_ids.get(winner_key, []))
        if voter_ids:
            chosen_user_id = random.choice(voter_ids)
            chosen_user = user_id_to_name.get(chosen_user_id, "Unknown")

            pending_placement = {
                "round_id": current_round_id,
                "item_key": winner_key,
                "chosen_user": chosen_user,
                "chosen_user_id": chosen_user_id,
                "ts": time.time(),
            }

            await send_chat_message(
                f"🏆 {ITEMS[winner_key]['emoji']} {ITEMS[winner_key]['label']} wins! "
                f"@{chosen_user} place it with: !place <left|middle|right> - {PLACEMENT_TIMEOUT}s!"
            )

            await broadcast_ws({
                "type": "placement_request",
                "round_id": current_round_id,
                "item_key": winner_key,
                "emoji": ITEMS[winner_key]["emoji"],
                "label": ITEMS[winner_key]["label"],
                "chosen_user": chosen_user,
                "chosen_user_id": chosen_user_id,  # ✅ important
                "hint": "Use !place <left|middle|right> in chat OR click the overlay area",
            })

        await asyncio.sleep(ROUND_BREAK_SECONDS)

# =========================
# HTTP / WS Handlers (aiohttp)
# =========================
@web.middleware
async def log_middleware(request, handler):
    print(f"[HTTP] {request.method} {request.path}")
    return await handler(request)

async def ws_handler(request: web.Request):
    global pending_placement

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    clients.add(ws)
    ws_user_id[ws] = None

    print(f"[WS] client connected. clients={len(clients)} path={request.path}")

    await send_state(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                t = data.get("type")

                if t == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "t": int(time.time() * 1000)}))
                    continue

                # ✅ Save twitch_user_id from overlay_hello (no prompt needed)
                if t == "overlay_hello":
                    tid = data.get("twitch_user_id")
                    ws_user_id[ws] = str(tid) if tid else None
                    await send_state(ws)
                    continue

                if t in ("get_state", "sync"):
                    await send_state(ws)
                    continue

                # ✅ Winner choosing place by clicking overlay
                if t == "place_choice":
                    if not pending_placement:
                        continue

                    slot = (data.get("place") or "").strip().lower()
                    if slot not in ("left", "middle", "right"):
                        continue

                    # authorize
                    sender_id = ws_user_id.get(ws)
                    winner_id = str(pending_placement.get("chosen_user_id", ""))
                    if not sender_id or sender_id != winner_id:
                        continue

                    item_key = pending_placement["item_key"]

                    # ✅ LOG spawn_item (Overlay click placement)
                    log_jsonl({
                        "type": "spawn_item",
                        "round_id": pending_placement["round_id"],
                        "item_key": item_key,
                        "emoji": ITEMS[item_key]["emoji"],
                        "label": ITEMS[item_key]["label"],
                        "chosen_user": pending_placement["chosen_user"],
                        "slot": slot,
                    })

                    place_item_in_game(item_key, slot)

                    await broadcast_ws({
                        "type": "place_update",
                        "chosen_user": pending_placement["chosen_user"],
                        "chosen_user_id": pending_placement["chosen_user_id"],
                        "place": slot,
                    })

                    pending_placement = None
                    await broadcast_ws({"type": "placement_complete"})
                    continue

                # ✅ Mouse events allowed ONLY from winner's WS
                if t == "mouse_event":
                    x = int(data.get("x", 0))
                    y = int(data.get("y", 0))
                    vx = int(data.get("vx", 0))
                    vy = int(data.get("vy", 0))
                    terminate = bool(data.get("terminate", False))

                    if not pending_placement:
                        continue

                    sender_id = ws_user_id.get(ws)
                    winner_id = str(pending_placement.get("chosen_user_id", ""))
                    if not sender_id or sender_id != winner_id:
                        continue

                    # ✅ LOG mouse_click when terminate=True
                    if terminate:
                        log_jsonl({
                            "type": "mouse_click",
                            "x": x,
                            "y": y,
                            "button": "LEFT",
                            "timestamp": data.get("timestamp"),
                        })

                    item_key = pending_placement["item_key"]
                    item_index = list(ITEMS.keys()).index(item_key)
                    event_type = item_index if terminate else 0

                    send_game_event(
                        event_type=event_type,
                        x=x, y=y, vx=vx, vy=vy,
                        hx=100, hy=100,
                        terminate=terminate
                    )

                    # keep your existing raw mouse_event log too
                    log_jsonl({
                        "type": "mouse_event",
                        "x": x, "y": y, "vx": vx, "vy": vy,
                        "terminate": terminate,
                        "item": item_key,
                        "sent_to_game": True,
                        "from_user_id": sender_id,
                    })
                    continue

            elif msg.type == web.WSMsgType.ERROR:
                print(f"[WS] error: {ws.exception()}")

    finally:
        clients.discard(ws)
        ws_user_id.pop(ws, None)
        print(f"[WS] client disconnected. clients={len(clients)}")

    return ws

async def overlay_handler(request: web.Request):
    if OVERLAY_PATH.exists():
        return web.FileResponse(OVERLAY_PATH)
    return web.Response(text=f"overlay.html not found at: {OVERLAY_PATH}", content_type="text/plain", status=200)

async def overlay_js_handler(request: web.Request):
    if OVERLAY_JS_PATH.exists():
        return web.FileResponse(OVERLAY_JS_PATH)
    return web.Response(text=f"overlay.js not found at: {OVERLAY_JS_PATH}", content_type="text/plain", status=200)

async def health_handler(request: web.Request):
    return web.json_response({
        "ok": True,
        "clients": len(clients),
        "round_active": current_round_active,
        "round_id": current_round_id,
        "pending": pending_placement,
    })

async def favicon_handler(request: web.Request):
    return web.Response(status=204)

# =========================
# Main
# =========================
async def main():
    global BOT_INSTANCE

    if HAS_PROTOBUF:
        connect_game_socket()

    app = web.Application(middlewares=[log_middleware])

    cors = setup(app, defaults={
        "*": ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
    })

    r_ws1 = app.router.add_get("/ws", ws_handler)
    r_ws2 = app.router.add_get("/ws/", ws_handler)

    r_overlay = app.router.add_get("/overlay.html", overlay_handler)
    r_js = app.router.add_get("/overlay.js", overlay_js_handler)
    r_root = app.router.add_get("/", lambda r: web.HTTPFound("/overlay.html"))
    r_health = app.router.add_get("/health", health_handler)
    r_fav = app.router.add_get("/favicon.ico", favicon_handler)

    for r in [r_ws1, r_ws2, r_overlay, r_js, r_root, r_health, r_fav]:
        cors.add(r)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()

    print("\n--- SERVER RUNNING ---")
    print(f"HTTP:   http://{HTTP_HOST}:{HTTP_PORT}/overlay.html")
    print(f"JS:     http://{HTTP_HOST}:{HTTP_PORT}/overlay.js")
    print(f"WS:     ws://{HTTP_HOST}:{HTTP_PORT}/ws")
    print(f"Health: http://{HTTP_HOST}:{HTTP_PORT}/health")
    print(f"Overlay exists: {OVERLAY_PATH.exists()}  -> {OVERLAY_PATH}")
    print(f"JS exists:      {OVERLAY_JS_PATH.exists()} -> {OVERLAY_JS_PATH}\n")

    bot = ChatBot()
    BOT_INSTANCE = bot

    rounds_task = asyncio.create_task(rounds_loop())

    try:
        await bot.start()
    finally:
        rounds_task.cancel()
        with contextlib.suppress(Exception):
            await rounds_task

        with contextlib.suppress(Exception):
            await runner.cleanup()

        with contextlib.suppress(Exception):
            await bot._custom_session.close()

if __name__ == "__main__":
    asyncio.run(main())
