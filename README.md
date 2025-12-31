# Twitch Extension Overlay – Python Server + WebSocket + Cloudflared

This project provides a **Twitch Extension overlay** that lets viewers vote on items, select a winner, and (for the winner only) send mouse/click data to a game through a **Python WebSocket server**.

---

## 📁 Project Structure

All files should be in the **same folder**:

project/
├── python_server.py
├── overlay.html
├── overlay.js
└── spawn_log.jsonl # auto-created by the server


---

## 🐍 1. Python Requirements

### Python version
- **Python 3.10+** (3.11 recommended)

### Install dependencies
Run this in the project folder:

```bash
pip install aiohttp aiohttp_cors twitchio certifi
```

## 🔑 2. Twitch Configuration

The server uses environment variables:
```bash
TWITCH_TOKEN → your Twitch OAuth token (oauth:xxxxxxxx)

TWITCH_CHANNEL → your Twitch channel name (without @)
```

Windows (PowerShell)
$env:TWITCH_TOKEN="oauth:YOUR_TOKEN"
$env:TWITCH_CHANNEL="your_channel"
python python_server.py

```bash
Expected output
[HTTP] http://0.0.0.0:8080/overlay.html
[WS] /ws
[BOT] connected to Twitch
```
## 🌐 3. Test Locally (before Cloudflared)

Open in your browser:

http://localhost:8080/overlay.html


Health check:

http://localhost:8080/health


## ☁️ 4. Run Cloudflared (Public URL)
Start tunnel
cloudflared tunnel --url http://localhost:8080


Cloudflared will output something like:

https://xxxxx.trycloudflare.com


## 🔧 5. Configure overlay.js

Edit overlay.js:
```bash
let SERVER_HOST = "xxxxx.trycloudflare.com";
const WS_PATH = "/ws";
```

⚠️ Do not include https://

## 🧩 6. Twitch Extension Setup

In the Twitch Developer Console:

Create / edit your Extension

Enable Identity

Set URLs:

Overlay URL

https://xxxxx.trycloudflare.com/overlay.html


Install the extension on your channel

## ▶️ 7. Recommended Run Order

Start Python server

python python_server.py


Start Cloudflared

cloudflared tunnel --url http://localhost:8080


Update SERVER_HOST in overlay.js

Reload the Twitch Extension overlay



