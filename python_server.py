import os, ssl, json, asyncio, contextlib, time
from pathlib import Path
from typing import Dict, Set

import certifi
import aiohttp
from aiohttp import web, TCPConnector
from aiohttp_cors import setup, ResourceOptions
from twitchio.ext import commands

# =========================
# SSL FIX FOR WINDOWS
# =========================
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl._create_default_https_context = lambda: ssl_context

# =========================
# Configuration
# =========================
HTTP_HOST = "0.0.0.0"
HTTP_PORT = 8080
ROUND_DURATION = 30

TWITCH_TOKEN = os.environ.get("TWITCH_TOKEN", "your_oauth_token_here").strip()
TWITCH_CHANNEL = os.environ.get("TWITCH_CHANNEL", "your_channel_here").strip()

ITEMS: Dict[str, Dict[str, str]] = {
    "freeze": {"emoji": "🧊", "label": "Freeze Orb"},
    "fire":   {"emoji": "🔥", "label": "Power Core"},
    "wind":   {"emoji": "💨", "label": "Wind Boots"},
    "shield": {"emoji": "🧱", "label": "Shield Stone"},
    "chaos":  {"emoji": "🎭", "label": "Chaos Mask"},
}

# =========================
# Global state
# =========================
clients: Set[web.WebSocketResponse] = set()

current_round_active = False
current_round_id = 0
current_votes = {k: 0 for k in ITEMS.keys()}
votes_by_item = {k: set() for k in ITEMS.keys()}

# Track end time for accurate remaining seconds
round_end_ts: float = 0.0

BASE_DIR = Path(__file__).resolve().parent
OVERLAY_PATH = BASE_DIR / "overlay.html"


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
    }

async def broadcast_ws(data: dict) -> None:
    """Broadcast JSON to all connected overlay clients."""
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

async def send_state(ws: web.WebSocketResponse) -> None:
    """Send current full state to one client (useful on connect / resync)."""
    await ws.send_str(json.dumps(build_state_payload(), ensure_ascii=False))


# =========================
# Twitch chat bot
# =========================
class ChatBot(commands.Bot):
    def __init__(self):
        # custom session that ignores SSL errors (your machine)
        self._custom_connector = TCPConnector(ssl=False)
        self._custom_session = aiohttp.ClientSession(connector=self._custom_connector)

        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
        )

        # Override TwitchIO internal session
        self._http.session = self._custom_session

    async def event_ready(self):
        print(f"--- BOT CONNECTED: {self.nick} ---")

    async def event_message(self, message):
        if not message.author or getattr(message, "echo", False):
            return
        await self.handle_commands(message)

    @commands.command(name="item")
    async def item_command(self, ctx: commands.Context):
        global current_votes, votes_by_item

        parts = ctx.message.content.split(maxsplit=1)
        if len(parts) < 2:
            return

        choice = parts[1].strip().lower()
        if (choice not in ITEMS) or (not current_round_active):
            return

        user = ctx.author.name

        # Prevent multiple votes per round
        all_voters = set().union(*votes_by_item.values())
        if user in all_voters:
            return

        current_votes[choice] += 1
        votes_by_item[choice].add(user)

        await broadcast_ws({
            "type": "vote",
            "user": user,
            "item": choice,
            "count": current_votes[choice],
        })


# =========================
# Round loop
# =========================
async def rounds_loop():
    global current_round_active, current_votes, votes_by_item, current_round_id, round_end_ts

    await asyncio.sleep(5)

    while True:
        current_round_id += 1
        current_votes = {k: 0 for k in ITEMS.keys()}
        votes_by_item = {k: set() for k in ITEMS.keys()}
        current_round_active = True
        round_end_ts = time.time() + ROUND_DURATION

        await broadcast_ws({
            "type": "round_start",
            "round_id": current_round_id,
            "duration": ROUND_DURATION,
            "options": [{"key": k, "emoji": v["emoji"], "label": v["label"]} for k, v in ITEMS.items()],
        })

        await asyncio.sleep(ROUND_DURATION)

        current_round_active = False
        round_end_ts = 0.0

        max_v = max(current_votes.values()) if any(current_votes.values()) else 0
        winner_key = (max(current_votes, key=lambda k: current_votes[k]) if max_v > 0 else None)
        winner = {"key": winner_key, **ITEMS[winner_key], "votes": max_v} if winner_key else None

        await broadcast_ws({"type": "round_result", "winner": winner, "votes": current_votes})
        await asyncio.sleep(10)


# =========================
# HTTP / WS Handlers
# =========================
@web.middleware
async def log_middleware(request, handler):
    # Helps you immediately see whether Twitch is requesting /ws or /ws/
    print(f"[HTTP] {request.method} {request.path}")
    return await handler(request)

async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    clients.add(ws)
    print(f"[WS] client connected. clients={len(clients)} path={request.path}")

    # ✅ Send state immediately so overlay isn't blank
    await send_state(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                # Overlay may send ping / hello / get_state
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                t = data.get("type")
                if t == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "t": int(time.time()*1000)}))
                elif t in ("overlay_hello", "get_state", "sync"):
                    await send_state(ws)
            elif msg.type == web.WSMsgType.ERROR:
                print(f"[WS] error: {ws.exception()}")
    finally:
        clients.discard(ws)
        print(f"[WS] client disconnected. clients={len(clients)}")

    return ws

async def overlay_handler(request: web.Request):
    # ✅ Avoid 404 if file missing (common “it works locally but not on server” issue)
    if OVERLAY_PATH.exists():
        return web.FileResponse(OVERLAY_PATH)

    # fallback simple page (so you instantly know the server is serving *something*)
    return web.Response(
        text=f"overlay.html not found at: {OVERLAY_PATH}",
        content_type="text/plain",
        status=200,
    )

async def favicon_handler(request: web.Request):
    # ✅ Stops annoying 404 in ngrok console for /favicon.ico
    return web.Response(status=204)


# =========================
# Main
# =========================
async def main():
    app = web.Application(middlewares=[log_middleware])

    # CORS (useful when testing outside Twitch)
    cors = setup(app, defaults={
        "*": ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
    })

    # ✅ Add BOTH /ws and /ws/ (trailing slash is a frequent cause of 404)
    r_ws1 = app.router.add_get("/ws", ws_handler)
    r_ws2 = app.router.add_get("/ws/", ws_handler)

    r_overlay = app.router.add_get("/overlay.html", overlay_handler)
    r_root = app.router.add_get("/", lambda r: web.HTTPFound("/overlay.html"))
    r_fav = app.router.add_get("/favicon.ico", favicon_handler)

    for r in [r_ws1, r_ws2, r_overlay, r_root, r_fav]:
        cors.add(r)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()

    print(f"--- SERVER RUNNING ---")
    print(f"HTTP:  http://{HTTP_HOST}:{HTTP_PORT}/overlay.html")
    print(f"WS:    ws://{HTTP_HOST}:{HTTP_PORT}/ws")
    print(f"Overlay file: {OVERLAY_PATH} (exists={OVERLAY_PATH.exists()})")

    bot = ChatBot()
    round_task = asyncio.create_task(rounds_loop())

    try:
        await bot.start()
    finally:
        round_task.cancel()
        with contextlib.suppress(Exception):
            await round_task

        with contextlib.suppress(Exception):
            await runner.cleanup()

        with contextlib.suppress(Exception):
            await bot._custom_session.close()


if __name__ == "__main__":
    asyncio.run(main())
