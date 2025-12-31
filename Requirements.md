
---

## Requirements

- Python 3.10+ recommended
- A Twitch account + OAuth token for `twitchio`
- `cloudflared` for exposing local server to Twitch (Twitch must reach your server)

---

## Setup

### 1) Install dependencies

```bash
pip install aiohttp aiohttp_cors twitchio certifi
