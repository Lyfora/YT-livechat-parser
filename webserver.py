from flask import Flask, request, jsonify
from threading import Thread
import os
import asyncio
import hmac
import logging

logging.basicConfig(level=logging.INFO)

app = Flask('')

# Shared references injected by lily-bot.py
_bot = None
_song_list = None
_active_streams = None
_current_idx_ref = None
_paused_ref = None
_save_cache = None
_send_song_list = None  # coroutine fn: async def send_song_list(channel)

TRAKTEER_TOKEN = os.getenv('TRAKTEER_TOKEN', '')

def setup(bot_instance, song_list, active_streams, idx_ref, send_song_list_fn, paused_ref=None, save_cache_fn=None):
    """Called from lily-bot.py to give this module access to bot state."""
    global _bot, _song_list, _active_streams, _current_idx_ref, _send_song_list, _paused_ref, _save_cache
    _paused_ref = paused_ref
    _save_cache = save_cache_fn
    _bot = bot_instance
    _song_list = song_list
    _active_streams = active_streams
    _current_idx_ref = idx_ref
    _send_song_list = send_song_list_fn

@app.route('/')
def home():
    return "Discord Bot is Ok"

@app.route('/trakteer-webhook', methods=['POST'])
def trakteer_webhook():
    # --- Debug logging ---
    logging.info("=== TRAKTEER WEBHOOK HIT ===")
    logging.info(f"Body: {request.get_data(as_text=True)}")  # headers are not logged: they contain the token

    # --- Token validation ---
    token = request.headers.get('X-Webhook-Token', '')
    if TRAKTEER_TOKEN and not hmac.compare_digest(token, TRAKTEER_TOKEN):
        logging.warning("Webhook token mismatch — returning 401")
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "Invalid JSON"}), 400

    supporter_name = data.get('supporter_name', 'Anonymous')
    supporter_message = data.get('supporter_message', '').strip()

    if not supporter_message:
        return jsonify({"status": "ignored", "message": "No supporter_message, nothing to add"}), 200

    # Find the active discord channel
    if not _active_streams:
        return jsonify({"status": "error", "message": "No active stream"}), 400

    # Note: !pause / !pause <n> only affect YouTube chat requests. Trakteer donations are paid,
    # so they are always queued while a stream is active.

    # Pick the first active channel (there is typically only one)
    channel_id = next(iter(_active_streams))
    channel = _bot.get_channel(channel_id)
    if channel is None:
        return jsonify({"status": "error", "message": "Channel not found"}), 500

    # Schedule the coroutine on the bot's event loop
    asyncio.run_coroutine_threadsafe(
        _add_song_from_trakteer(channel, supporter_name, supporter_message),
        _bot.loop
    )

    return jsonify({"status": "ok", "message": "Song queued"}), 200


async def _add_song_from_trakteer(channel, supporter_name: str, song_title: str):
    """Add a song to the queue and post a Discord embed (mirrors !add logic)."""
    import discord

    _current_idx_ref['value'] += 1
    idx = _current_idx_ref['value']
    song_display = f"(Trakteer) - {song_title}"
    _song_list[idx] = [song_display, supporter_name, 'trakteer']
    if _save_cache:
        _save_cache()

    embed = discord.Embed(
        description=song_display,
        color=discord.Color.green()
    )
    embed.set_author(name=supporter_name)
    embed.set_footer(text="Trakteer Donation 💚")
    await channel.send(embed=embed)
    if _send_song_list:
        await _send_song_list(channel)


def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()