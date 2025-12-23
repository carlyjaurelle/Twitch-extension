# Twitch-extension
Created a twich extension 

1) Create Twitch OAuth token

You need an OAuth token for the bot (Twitch chat access).
Set it as an environment variable:

Windows PowerShell:
$env:TWITCH_TOKEN="oauth:YOUR_TOKEN_HERE"
$env:TWITCH_CHANNEL="your_channel_name"

macOS / Linux:
export TWITCH_TOKEN="oauth:YOUR_TOKEN_HERE"
export TWITCH_CHANNEL="your_channel_name"

2)Run the server
python server.py
You should see logs like:

--- SERVER RUNNING ---

--- BOT CONNECTED ---

3) Expose the server to Twitch with ngrok

Twitch Extensions cannot access localhost, so you must expose it publicly.

ngrok http 8080

