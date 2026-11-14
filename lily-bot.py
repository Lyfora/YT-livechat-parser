import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import googleapiclient.discovery
import googleapiclient.errors
import re
import webserver
import time
import pickle
import json
from datetime import date
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
TRAKTEER_TOKEN = os.getenv('TRAKTEER_TOKEN', '')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')

# Scopes for YouTube API (force-ssl is required for reading/writing chat)
YOUTUBE_SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']

# Setup logging
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# Bot permissions
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Global variables
active_streams = {}  # Dictionary to track active streams per channel
youtube_client = None

# SongQueue
song_list = {}
# current_idx is wrapped in a dict so webserver.py can mutate it from a different thread
current_idx_ref = {'value': 0}
# Each list has its own pointer = number of the last song taken from that list
# (everything in that list up to it is done). `now` = which list the song being sung is from.
# Dict, so it is mutated in place.
play_state = {'youtube': 0, 'trakteer': 0, 'now': None}
# When paused, no new songs are added (shared with webserver.py, hence the dict)
paused_ref = {'value': False}
# Auto-pause: once the queue holds `limit` songs, new YouTube-chat requests are ignored,
# but manual adds (!add / !add_yt) and Trakteer donations are still accepted.
auto_pause_ref = {'limit': None, 'notified': False}

def reset_pause():
    paused_ref['value'] = False
    auto_pause_ref['limit'] = None
    auto_pause_ref['notified'] = False
last_ngintip_time = 0  # To track cooldown for !ngintip from YouTube chat

# --- Queue cache (survives bot restarts; restored if same channel + same day) ---
CACHE_FILE = 'queue_cache.json'
processed_messages = set()  # YouTube chat message IDs already handled (persisted so a restart doesn't re-add old !req)
cache_meta = {'channel_id': None, 'video_id': None}

def song_source(entry) -> str:
    """'trakteer' or 'youtube' for a queue entry (older entries have no source field)."""
    if len(entry) > 2:
        return entry[2]
    return 'trakteer' if entry[0].startswith('(Trakteer)') else 'youtube'

def song_status(i) -> str:
    """'done' | 'playing' | 'pending' for song number i."""
    src = song_source(song_list[i])
    ptr = play_state[src]
    if i < ptr:
        return 'done'
    if i == ptr:
        return 'playing' if play_state['now'] == src else 'done'
    return 'pending'

def now_playing_id() -> int:
    """Number of the song being sung right now (0 = nothing yet)."""
    src = play_state['now']
    return play_state[src] if src else 0

def next_in_source(src) -> int:
    """Number of the next song in one list (0 = none left). NB: no builtin next() here, !next shadows it."""
    return min((i for i, s in song_list.items() if song_source(s) == src and i > play_state[src]), default=0)

def played_count(src) -> int:
    return sum(1 for i, s in song_list.items() if song_source(s) == src and i <= play_state[src])

def reset_play_state():
    play_state.update(youtube=0, trakteer=0, now=None)

def save_cache():
    if cache_meta['channel_id'] is None:
        return
    data = {
        'channel_id': cache_meta['channel_id'],
        'video_id': cache_meta['video_id'],
        'date': date.today().isoformat(),
        'song_list': {str(k): v for k, v in song_list.items()},
        'current_idx': current_idx_ref['value'],
        'play_state': dict(play_state),
        'processed_messages': list(processed_messages),
    }
    try:
        tmp = CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print(f"Error saving cache: {e}")

def restore_cache(channel_id, video_id=None) -> bool:
    """Load today's queue for this channel + video into memory. True if a matching cache was found.

    A cache saved with a video ID only matches that same video; a cache without one
    (e.g. made by !add_bulk before the stream started) matches any video that day.
    """
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if data.get('channel_id') != channel_id or data.get('date') != date.today().isoformat():
            return False
        if data.get('video_id') and video_id and data['video_id'] != video_id:
            return False
        restored = {int(k): v for k, v in data['song_list'].items()}
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return False
    song_list.clear()
    song_list.update(restored)
    current_idx_ref['value'] = data.get('current_idx', len(song_list))
    saved = data.get('play_state')
    if saved:
        play_state.update(youtube=saved.get('youtube', 0), trakteer=saved.get('trakteer', 0), now=saved.get('now'))
    else:
        # Older cache (single pointer): everything up to it was done, that song is the current one
        old = data.get('played_idx', 0)
        reset_play_state()
        for i, s in song_list.items():
            if i <= old:
                play_state[song_source(s)] = max(play_state[song_source(s)], i)
        if old in song_list:
            play_state['now'] = song_source(song_list[old])
    processed_messages.clear()
    processed_messages.update(data.get('processed_messages', []))
    return True

_BULK_LINE = re.compile(r'^(✅|▶️?|⏳)\s*(.+)$')

def parse_bulk(text: str):
    """Parse a pasted '#N / ✅ title - author' list. Returns (songs, play_state_dict)."""
    songs = []
    state = {'youtube': 0, 'trakteer': 0, 'now': None}
    for raw in text.splitlines():
        line = re.sub(r'^#\d+\s*🎵?\s*', '', raw.strip())  # allow '#1 ✅ ...' on one line
        m = _BULK_LINE.match(line)
        if not m or ' - ' not in m.group(2):
            continue
        status, body = m.groups()
        title, author = body.rsplit(' - ', 1)  # titles may contain ' - ', the author is last
        title, author = title.strip(), author.strip()
        source = 'trakteer' if title.startswith('(Trakteer)') else 'youtube'
        songs.append([title, author, source])
        if status.startswith('▶') or status == '✅':
            state[source] = len(songs)      # everything up to the last done/playing song of a list is done
            if status.startswith('▶'):
                state['now'] = source
    return songs, state

def format_export() -> str:
    lines = ["1. List song"]
    for i in sorted(song_list):
        name, value = _song_field(i)
        lines.append(name)
        lines.append(value)
    return "\n".join(lines)

def extract_youtube_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats"""
    # Regular expression patterns for different YouTube URL formats
    patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/live/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?youtu\.be/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

async def get_authenticated_youtube_client():
    """Get authenticated YouTube service using OAuth2 flow."""
    try:
        creds = None
        # token.pickle stores the user's access and refresh tokens
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
                
        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print("Refreshing YouTube token...")
                creds.refresh(Request())
                print("YouTube token refreshed successfully!")
            else:
                if not CLIENT_ID or not CLIENT_SECRET:
                    print("Error: CLIENT_ID or CLIENT_SECRET missing in .env")
                    return None
                    
                print("Starting YouTube OAuth2 flow...")
                print(">>> Open the URL below in your browser, authorize, then paste the code back here.")
                client_config = {
                    "installed": {
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
                flow = InstalledAppFlow.from_client_config(client_config, YOUTUBE_SCOPES)
                creds = flow.run_local_server(port=0)
                
            # Save the credentials for the next run
            with open('token.pickle', 'wb') as token:
                pickle.dump(creds, token)

        return googleapiclient.discovery.build("youtube", "v3", credentials=creds)
    
    except Exception as e:
        print(f"❌ YouTube auth error: {type(e).__name__}: {e}")
        return None

async def send_youtube_message(live_chat_id, text):
    """Send a message to the YouTube live chat."""
    if not youtube_client:
        return
        
    try:
        youtube_client.liveChatMessages().insert(
            part="snippet",
            body={
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {
                        "messageText": text
                    }
                }
            }
        ).execute()
        print(f"Sent to YouTube: {text}")
    except Exception as e:
        print(f"Error sending to YouTube: {e}")

NGINTIP_COOLDOWN = 300          # seconds (5 min), per YouTube user; moderators/owner are exempt
ngintip_user_cooldowns = {}     # normalized YT name -> last !ngintip time
YT_CHAT_LIMIT = 200             # YouTube live chat messages max 200 chars, single line

def _norm_name(name) -> str:
    return str(name).strip().lstrip('@').casefold()

def _clean_title(title: str) -> str:
    prefix = "(Trakteer) - "
    return title[len(prefix):] if title.startswith(prefix) else title

def build_ngintip_lines(author=None, hint=True):
    """Lines for !ngintip. With `author`, starts with that person's own request(s) if they have any."""
    total = len(song_list)
    now_id = now_playing_id()
    lines = []
    has_requested = False

    if author:
        mine = [i for i in sorted(song_list) if _norm_name(song_list[i][1]) == _norm_name(author)]
        has_requested = bool(mine)
        upcoming = [i for i in mine if song_status(i) == 'pending']
        if now_id and now_id in mine:
            lines.append(f"Lagu yg kamu request {_clean_title(song_list[now_id][0])} lagi dinyanyiin sekarang (urutan ke - {now_id}).")
        for i in upcoming[:3]:
            lines.append(f"Lagu yg kamu request {_clean_title(song_list[i][0])} ada di urutan ke - {i}.")
        if mine and not upcoming and now_id not in mine:
            last = mine[-1]
            lines.append(f"Lagu yg kamu request {_clean_title(song_list[last][0])} sudah dinyanyiin (urutan ke - {last}).")
        if lines:
            lines.append("")

    if not song_list:
        lines.append("Queue masih kosong.")
    elif not now_id:
        lines.append("Lily belum mulai nyanyi, sebentar lagi ya!")
    else:
        lines.append(f"Sekarang Lily lagi nyanyiin {_clean_title(song_list[now_id][0])} di urutan ke - {now_id} dari {total} Lagu")

    # Next 3 YouTube songs + next 1 Trakteer song (each list has its own !next)
    yt_ids = [i for i in sorted(song_list)
              if song_source(song_list[i]) == 'youtube' and song_status(i) == 'pending'][:3]
    trakteer_id = next_in_source('trakteer')
    if yt_ids:
        lines.append("")
        lines.append(f"{len(yt_ids)} Lagu yg bakal Lily nyanyiin ada :")
        for n, i in enumerate(yt_ids, start=1):
            lines.append(f"{n}. {song_list[i][1]} - {_clean_title(song_list[i][0])}")
    if trakteer_id:
        if not yt_ids:
            lines.append("")
        lines.append(f"💚 Trakteer berikutnya : {song_list[trakteer_id][1]} - {_clean_title(song_list[trakteer_id][0])}")
    if song_list and now_id and not yt_ids and not trakteer_id:
        lines.append("")
        lines.append("Belum ada lagu selanjutnya.")

    if hint and not has_requested:
        lines.append("")
        lines.append("Pakai command !req <nama_lagu> untuk request ya, atau bisa via Trakteer :-)")
    return lines

async def send_youtube_lines(live_chat_id, lines):
    """YouTube chat has no multi-line messages and a 200-char cap, so send one message per line."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if len(line) > YT_CHAT_LIMIT:
            line = line[:YT_CHAT_LIMIT - 1] + "…"
        await send_youtube_message(live_chat_id, line)
        await asyncio.sleep(1)

async def get_live_chat_id(video_id: str) -> str:
    """Get the live chat ID for a YouTube video"""
    try:
        response = youtube_client.videos().list(
            part="liveStreamingDetails",
            id=video_id
        ).execute()
        
        if response["items"]:
            live_details = response["items"][0].get("liveStreamingDetails")
            if live_details and "activeLiveChatId" in live_details:
                return live_details["activeLiveChatId"]
        
        return None
    except Exception as e:
        print(f"Error getting live chat ID: {e}")
        return None

async def fetch_live_chat_messages(video_id: str, discord_channel):
    """Fetch live chat messages and send them to Discord channel"""
    global song_list  # current_idx_ref is a dict, no need to declare global
    
    live_chat_id = await get_live_chat_id(video_id)
    
    if not live_chat_id:
        await discord_channel.send("❌ This video doesn't have an active live chat or isn't currently live.")
        return
    
    await discord_channel.send(f"✅ Started monitoring live chat for video: `{video_id}`")
    
    next_page_token = None
    # processed_messages is module-level (and cached) so a restart doesn't re-add old !req

    try:
        while discord_channel.id in active_streams and active_streams[discord_channel.id] == video_id:
            response = youtube_client.liveChatMessages().list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                pageToken=next_page_token
            ).execute()

            for item in response["items"]:
                message_id = item["id"]
                
                # Skip if we've already processed this message
                if message_id in processed_messages:
                    continue
                
                # Add to processed messages to avoid duplicates
                processed_messages.add(message_id)
                
                message_text = item["snippet"].get("displayMessage", "")
                if not message_text:
                    continue  # skip system/non-text messages
                author = item["authorDetails"]["displayName"]
                
                # Handle !ngintip (personalised queue peek, per-user cooldown)
                if message_text.lower().startswith('!ngintip'):
                    details = item["authorDetails"]
                    is_staff = bool(details.get("isChatModerator") or details.get("isChatOwner"))
                    key = _norm_name(author)
                    now = time.time()
                    if not is_staff:  # chat moderators (and the channel owner) have no cooldown
                        if now - ngintip_user_cooldowns.get(key, 0) < NGINTIP_COOLDOWN:
                            continue
                        ngintip_user_cooldowns[key] = now

                    lines = build_ngintip_lines(author)
                    peek_embed = discord.Embed(
                        title="YouTube Chat Peek (!ngintip)",
                        description="\n".join(lines),
                        color=discord.Color.gold()
                    )
                    peek_embed.set_author(name=author)
                    await discord_channel.send(embed=peek_embed)
                    await send_youtube_lines(live_chat_id, lines)
                    continue

                # Only process messages that start with !req
                if not message_text.lower().startswith('!req'):
                    continue

                # Paused: ignore new requests until !continue
                if paused_ref['value']:
                    continue

                # Auto-pause: queue already holds `limit` songs -> ignore new chat requests
                limit = auto_pause_ref['limit']
                if limit is not None and len(song_list) >= limit:
                    if not auto_pause_ref['notified']:
                        auto_pause_ref['notified'] = True
                        await discord_channel.send(
                            f"⏸️ **Auto-pause:** queue sudah {len(song_list)} lagu (limit {limit}). "
                            "Request baru dari YouTube chat diabaikan; `!add` / `!add_yt` / Trakteer tetap diterima. "
                            "Pakai `!continue` untuk membuka lagi."
                        )
                    continue
                auto_pause_ref['notified'] = False

                # Create an embed for better formatting
                embed = discord.Embed(
                    description=message_text,
                    color=discord.Color.red()
                )
                embed.set_author(name=author)
                embed.set_footer(text="YouTube Live Chat")
                
                # Create song list
                song_list_temp = message_text[5:].strip()

                # Add it to the list
                current_idx_ref['value'] += 1
                song_list[current_idx_ref['value']] = [song_list_temp, author, 'youtube']
                save_cache()

                await discord_channel.send(embed=embed)
                await send_song_list(discord_channel)

            next_page_token = response.get("nextPageToken")
            save_cache()  # also persists processed message IDs

            # Wait before polling again (YouTube API recommends polling interval)
            poll_interval = response.get("pollingIntervalMillis", 5000) / 1000
            # Minimum 20 seconds, slept in 1s steps so !stop_live_chat takes effect quickly
            for _ in range(int(max(poll_interval, 20))):
                if active_streams.get(discord_channel.id) != video_id:
                    break
                await asyncio.sleep(1)

    except googleapiclient.errors.HttpError as e:
        status = getattr(e.resp, 'status', None)
        if status in (403, 404) and ('liveChatEnded' in str(e) or 'liveChatNotFound' in str(e)):
            await discord_channel.send("📴 Live stream sudah selesai (live chat ditutup). Queue tetap tersimpan di cache hari ini.")
        else:
            await discord_channel.send(f"❌ Error while fetching live chat: {e}")
        print(f"Live chat fetch error: {e}")
    except Exception as e:
        await discord_channel.send(f"❌ Error while fetching live chat: {e}")
        print(f"Live chat fetch error: {e}")
    
    # Clean up when done
    if discord_channel.id in active_streams:
        del active_streams[discord_channel.id]
    reset_pause()

    await discord_channel.send("🛑 Stopped monitoring live chat.")

@bot.event
async def on_ready():
    global youtube_client
    print(f"Bot is ready! Logged in as {bot.user.name}")
    
    # Initialize YouTube API client (Authenticated)
    youtube_client = await get_authenticated_youtube_client()
    if youtube_client:
        print("YouTube API client (OAuth2) initialized")
    else:
        print("Warning: YouTube API client failed to initialize!")

@bot.command(name='hello')
async def hello(ctx):
    """Simple hello command"""
    await ctx.send(f"Hello {ctx.author.mention}! 👋")

@bot.command(name='start_live_chat')
async def start_live_chat(ctx, url: str, fresh: str = ""):
    """Start monitoring YouTube live chat and send messages to current Discord channel"""
    global song_list  # current_idx_ref is a dict, no need to declare global
    
    if not youtube_client:
        await ctx.send("❌ YouTube API is not configured. Please check your API key.")
        return
    
    video_id = extract_youtube_id(url)
    if not video_id:
        await ctx.send("❌ That doesn't look like a valid YouTube URL.")
        return
    
    # Check if there's already an active stream for this channel
    if ctx.channel.id in active_streams:
        await ctx.send(f"❌ Already monitoring a live chat in this channel. Use `!stop_live_chat` first.")
        return
    
    # Same channel + same day => restore the cached queue (survives bot restarts).
    # `!start_live_chat <url> fresh` forces an empty queue.
    cache_meta['channel_id'] = ctx.channel.id
    cache_meta['video_id'] = video_id
    restored = fresh.lower() != 'fresh' and restore_cache(ctx.channel.id, video_id)
    if not restored:
        song_list.clear()
        current_idx_ref['value'] = 0
        reset_play_state()
        processed_messages.clear()
    reset_pause()
    save_cache()

    # Store the active stream
    active_streams[ctx.channel.id] = video_id

    # Start fetching live chat in the background
    bot.loop.create_task(fetch_live_chat_messages(video_id, ctx.channel))

    await ctx.send(f"🔄 Starting to monitor live chat for video: `{video_id}`")
    if restored:
        await ctx.send(f"♻️ Queue hari ini di-restore dari cache ({len(song_list)} lagu). Pakai `!start_live_chat <link> fresh` untuk mulai dari kosong.")
        await send_song_list(ctx.channel)

@bot.command(name='stop_live_chat')
async def stop_live_chat(ctx):
    """Stop monitoring YouTube live chat for current Discord channel"""
    if ctx.channel.id not in active_streams:
        await ctx.send("❌ No active live chat monitoring in this channel.")
        return
    
    # Remove from active streams (this will stop the fetch loop)
    del active_streams[ctx.channel.id]
    reset_pause()
    await ctx.send("🛑 Stopped live chat monitoring for this channel.")

def collect_requesters():
    """Unique requester names (first-appearance order): (trakteer_names, youtube_names)."""
    trakteer, youtube = [], []
    seen = {'trakteer': set(), 'youtube': set()}
    for i in sorted(song_list):
        entry = song_list[i]
        name = str(entry[1]).strip()
        source = song_source(entry)
        if not name or name.lower() in seen[source]:
            continue
        seen[source].add(name.lower())
        (trakteer if source == 'trakteer' else youtube).append(name)
    return trakteer, youtube

def shoutout_messages(prefix, names, suffix="", limit=1800):
    """Build message(s) '<prefix><name, name, ...><suffix>', splitting if a list is too long for Discord."""
    names = [discord.utils.escape_markdown(n) for n in names]
    messages, current = [], []
    for name in names:
        candidate = prefix + ", ".join(current + [name]) + suffix
        if current and len(candidate) > limit:
            messages.append(prefix + ", ".join(current) + suffix)
            current = []
        current.append(name)
    if current:
        messages.append(prefix + ", ".join(current) + suffix)
    return messages

@bot.command(name='end_live_stream')
async def end_live_stream(ctx):
    """Shout out everyone who requested (Trakteer + YouTube), then stop monitoring"""
    trakteer, youtube = collect_requesters()
    if not trakteer and not youtube and ctx.channel.id not in active_streams:
        await ctx.send("❌ Tidak ada live stream aktif dan queue kosong.")
        return

    no_mentions = discord.AllowedMentions.none()  # names are user-provided: never ping anyone
    await ctx.send("🎉 **Stream selesai! Makasih semuanya!**")
    for msg in shoutout_messages("💚 Thank you for Trakteer ", trakteer):
        await ctx.send(msg, allowed_mentions=no_mentions)
    for msg in shoutout_messages("🙏 Thank you for requesting ", youtube, " from YT"):
        await ctx.send(msg, allowed_mentions=no_mentions)

    if ctx.channel.id in active_streams:
        del active_streams[ctx.channel.id]  # the fetch loop stops on its own
        reset_pause()
        await ctx.send("🛑 Stopped live chat monitoring for this channel. (Queue tetap tersimpan di cache hari ini.)")

@bot.command(name='pause')
async def pause(ctx, limit: int = None):
    """!pause = stop all new songs; !pause <n> = auto-pause YouTube requests once the queue has n songs"""
    if ctx.channel.id not in active_streams:
        await ctx.send("❌ Tidak bisa pause: belum ada live stream yang dimonitor di channel ini. Gunakan `!start_live_chat <link>` dulu.")
        return

    if limit is None:
        if paused_ref['value']:
            await ctx.send("⏸️ Sudah dalam kondisi pause. Gunakan `!continue` untuk lanjut.")
            return
        paused_ref['value'] = True
        await ctx.send("⏸️ **Paused.** Request dari YouTube chat diabaikan sampai kamu pakai `!continue`. `!add`, `!add_yt` dan Trakteer tetap diterima.")
        return

    if limit < 1:
        await ctx.send("❌ Limit harus minimal 1. Contoh: `!pause 20`")
        return
    auto_pause_ref['limit'] = limit
    auto_pause_ref['notified'] = False
    count = len(song_list)
    status = "sudah tercapai, request YouTube baru langsung diabaikan" if count >= limit else "request YouTube diterima sampai limit"
    await ctx.send(
        f"⏸️ **Auto-pause {limit} lagu** aktif (queue sekarang {count}/{limit}: {status}). "
        "`!add`, `!add_yt` dan Trakteer tetap diterima. `!continue` untuk mematikan."
    )

@bot.command(name='continue')
async def continue_(ctx):
    """Resume accepting new songs (clears both !pause and !pause <n>)"""
    if ctx.channel.id not in active_streams:
        await ctx.send("❌ Tidak ada live stream yang dimonitor di channel ini.")
        return
    if not paused_ref['value'] and auto_pause_ref['limit'] is None:
        await ctx.send("▶️ Bot tidak sedang pause.")
        return
    paused_ref['value'] = False
    auto_pause_ref['limit'] = None
    auto_pause_ref['notified'] = False
    await ctx.send("▶️ **Lanjut!** Request lagu baru diterima lagi.")

@bot.command(name='live_status')
async def live_status(ctx):
    """Check the status of live chat monitoring"""
    if ctx.channel.id in active_streams:
        video_id = active_streams[ctx.channel.id]
        await ctx.send(f"✅ Currently monitoring live chat for video: `{video_id}`")
    else:
        await ctx.send("❌ No active live chat monitoring in this channel.")

@bot.command(name='current_song')
async def current_song(ctx):
    """Check the current song (and what each list plays next)"""
    song_embed = discord.Embed(
        title="Lily Current Song",
        color=discord.Color.blue()
    )
    if not song_list:
        song_embed.add_field(name="Current Song", value="Tidak ada lagu dalam queue saat ini!", inline=False)
    else:
        now_id = now_playing_id()
        if now_id:
            now_text = f"#{now_id} {song_list[now_id][0]} - {song_list[now_id][1]}"
        else:
            now_text = "Belum ada lagu yang dimainkan."
        song_embed.add_field(name="Current Song", value=now_text, inline=False)
        for src, label in (('youtube', "Next YouTube (!next)"), ('trakteer', "Next Trakteer (!next_trakteer)")):
            nid = next_in_source(src)
            value = f"#{nid} {song_list[nid][0]} - {song_list[nid][1]}" if nid else "—"
            song_embed.add_field(name=label, value=value, inline=False)
    await ctx.send(embed=song_embed)

async def advance_list(ctx, source):
    """Move one list (YouTube for !next, Trakteer for !next_trakteer) to its next song."""
    label = "Trakteer" if source == 'trakteer' else "YouTube"
    embed = discord.Embed(
        title=f"Move to the next {label} song",
        color=discord.Color.green() if source == 'trakteer' else discord.Color.dark_purple()
    )
    if not any(song_source(s) == source for s in song_list.values()):
        embed.add_field(name="Queue Empty", value=f"Tidak ada lagu {label} dalam queue!", inline=False)
    else:
        nid = next_in_source(source)
        if not nid:
            embed.add_field(name="Info", value=f"Sudah di lagu terakhir di list {label}!", inline=False)
        else:
            play_state[source] = nid
            play_state['now'] = source
            embed.add_field(name="Now Playing", value=f"#{nid} {song_list[nid][0]} - {song_list[nid][1]}", inline=False)
            following = next_in_source(source)
            embed.add_field(
                name=f"Next {label} Song",
                value=f"#{following} {song_list[following][0]} - {song_list[following][1]}" if following else "Tidak ada lagu selanjutnya!",
                inline=False
            )
    save_cache()
    await ctx.send(embed=embed)
    await send_song_list(ctx.channel)

@bot.command(name='next')
async def next(ctx):
    """Move to the next YouTube song"""
    await advance_list(ctx, 'youtube')

@bot.command(name='next_trakteer')
async def next_trakteer(ctx):
    """Move to the next Trakteer song"""
    await advance_list(ctx, 'trakteer')

def split_song_request(raw: str):
    """Split '<song>-<requester>' on the LAST dash, so titles may contain dashes."""
    if '-' not in raw:
        return None
    title, name = raw.rsplit('-', 1)
    title, name = title.strip(), name.strip()
    if not title or not name:
        return None
    return title, name

async def manual_add(ctx, raw: str, source: str):
    """Shared logic for !add (Trakteer) and !add_yt (YouTube)."""
    if ctx.channel.id not in active_streams:
        await ctx.send("❌ No active live chat monitoring in this channel.")
        return
    # !pause / !pause <n> only affect YouTube chat requests; manual adds are never blocked.
    parsed = split_song_request(raw)
    if not parsed:
        cmd = 'add' if source == 'trakteer' else 'add_yt'
        await ctx.send(f"❌ Format salah. Pakai: `!{cmd} <nama lagu>-<nama yang request>`")
        return
    title, requester = parsed

    if source == 'trakteer':
        display = "(Trakteer) - " + title
        embed = discord.Embed(description=display, color=discord.Color.green())
        embed.set_footer(text="Trakteer Request Chat 💚")
    else:
        display = title
        embed = discord.Embed(description=display, color=discord.Color.red())
        embed.set_footer(text="YouTube Request (manual)")
    embed.set_author(name=requester)

    current_idx_ref['value'] += 1
    song_list[current_idx_ref['value']] = [display, requester, source]
    save_cache()
    await ctx.send(embed=embed)
    await send_song_list(ctx.channel)

@bot.command(name='add')
async def add(ctx, *, song):
    """Add a Trakteer song (goes to the Trakteer list)"""
    await manual_add(ctx, song, 'trakteer')

@bot.command(name='add_yt')
async def add_yt(ctx, *, song):
    """Add a YouTube-chat song manually (goes to the YouTube list)"""
    await manual_add(ctx, song, 'youtube')

@bot.command(name='add_bulk')
async def add_bulk(ctx, *, text: str):
    """Replace the queue with a pasted list (same format as !list_song / !export_song)"""
    songs, new_state = parse_bulk(text)
    if not songs:
        await ctx.send("❌ Tidak ada lagu yang terbaca. Format tiap lagu: `#1` lalu baris `✅ judul - @nama` (✅ = sudah, ▶️ = sedang, ⏳ = belum).")
        return

    song_list.clear()
    for n, song in enumerate(songs, start=1):
        song_list[n] = song
    current_idx_ref['value'] = len(songs)
    play_state.update(new_state)

    # Remember this queue for today in this channel
    cache_meta['channel_id'] = ctx.channel.id
    cache_meta['video_id'] = active_streams.get(ctx.channel.id)  # None if no stream yet: matches any video that day
    save_cache()

    await ctx.send(f"✅ Queue di-restore: **{len(songs)}** lagu (sedang dimainkan: #{now_playing_id() or '-'}). Disimpan ke cache.")
    await send_song_list(ctx.channel)

@bot.command(name='export_song')
async def export_song(ctx):
    """Post a copy-pasteable text version of the queue (re-importable with !add_bulk)"""
    if not song_list:
        await ctx.send("❌ Queue kosong, tidak ada yang di-copy.")
        return
    lines = format_export().split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1800:
            await ctx.send(f"```\n{chunk}```")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await ctx.send(f"```\n{chunk}```")

def _song_field(i):
    title, author = song_list[i][0], song_list[i][1]
    status = song_status(i)
    if status == 'done':
        return f"#{i}", f"✅ {title} - {author}"
    if status == 'playing':
        return f"#{i} 🎵", f"▶️ {title} - {author}"
    return f"#{i}", f"⏳ {title} - {author}"

MAX_SHOWN_TOTAL = 20     # max songs shown across both lists in one view (Discord-friendly)
MAX_SHOWN_TRAKTEER = 5   # max Trakteer songs shown

def page_size(source):
    """Trakteer: up to 5. YouTube: whatever is left of 20 (4 Trakteer shown -> 16, 5 -> 15)."""
    trakteer_shown = min(MAX_SHOWN_TRAKTEER, sum(1 for s in song_list.values() if song_source(s) == 'trakteer'))
    if source == 'trakteer':
        return MAX_SHOWN_TRAKTEER
    return MAX_SHOWN_TOTAL - trakteer_shown
CONTEXT_PLAYED = {'trakteer': 1, 'youtube': 2}  # already-played songs kept above the current one
MAX_DESC_CHARS = 3800  # embed description hard limit is 4096; stay well under
MAX_LINE_CHARS = 150

def _song_line(i):
    title, author = song_list[i][0], song_list[i][1]
    text = f"{title} - {author}"
    if len(text) > MAX_LINE_CHARS:
        text = text[:MAX_LINE_CHARS - 1] + "…"
    status = song_status(i)
    if status == 'done':
        return f"✅ **#{i}** {text}"
    if status == 'playing':
        return f"▶️ **#{i} 🎵** {text}"
    return f"⏳ **#{i}** {text}"

def _build_list_embed(title, color, ids, page=None, source='youtube'):
    """One embed with up to page_size(source) songs, one line per song.

    page=None -> window around the current song; page=N -> Nth block of that many songs.
    """
    size = page_size(source)
    total_pages = max(1, -(-len(ids) // size))
    if page is None:
        if len(ids) <= size:
            start = 0
        else:
            # NB: don't use builtin next() here, the !next command shadows it
            pos = sum(1 for i in ids if i < play_state[source])
            start = max(0, min(pos - CONTEXT_PLAYED[source], len(ids) - size))
    else:
        start = (page - 1) * size
    shown = ids[start:start + size]

    def render(shown):
        lines = []
        if start > 0:
            lines.append(f"⏸️ {start} lagu sebelumnya disembunyikan (`!list_song <halaman>` / `!list_song all`)")
        lines.extend(_song_line(i) for i in shown)
        more = len(ids) - (start + len(shown))
        if more > 0:
            lines.append(f"➕ {more} lagu lagi (`!list_song <halaman>` / `!list_song all`)")
        return "\n".join(lines)

    desc = render(shown)
    while len(desc) > MAX_DESC_CHARS and len(shown) > 1:  # long titles: drop from the far end
        shown = shown[:-1]
        desc = render(shown)

    embed = discord.Embed(title=title, description=desc or "—", color=color)
    return embed, total_pages

def _parse_page_arg(arg):
    """None -> window, 'all' -> everything, '<n>' -> page n. Raises ValueError on junk."""
    if arg is None:
        return None
    if arg.lower() == 'all':
        return 'all'
    if arg.isdigit() and int(arg) >= 1:
        return int(arg)
    raise ValueError(arg)

async def send_source_list(channel, source, page=None):
    """Send the Trakteer or YouTube list. page: None (window), int, or 'all'."""
    ids = sorted(i for i, s in song_list.items() if song_source(s) == source)
    if source == 'trakteer':
        title, color = "💚 Trakteer List (Priority)", discord.Color.green()
    else:
        title, color = "List song", discord.Color.magenta()

    if not ids:
        embed = discord.Embed(title=title, color=color)
        if source == 'trakteer':
            embed.description = "Belum ada request Trakteer."
        elif not song_list:
            embed.description = "Tidak ada lagu dalam queue!"
        else:
            embed.description = "Belum ada request dari YouTube."
        await channel.send(embed=embed)
        return

    total_pages = max(1, -(-len(ids) // page_size(source)))
    if page == 'all':
        pages = list(range(1, total_pages + 1))
    elif page is None:
        pages = [None]
    elif page > total_pages:
        await channel.send(f"❌ Halaman {page} tidak ada. {title} hanya punya {total_pages} halaman.")
        return
    else:
        pages = [page]

    for p in pages:
        embed, _ = _build_list_embed(title, color, ids, p, source)
        if p is None:
            footer = f"{len(ids)} song(s)"
        else:
            footer = f"Halaman {p}/{total_pages} | {len(ids)} song(s)"
        footer += f" | {played_count(source)}/{len(ids)} played"
        if source == 'youtube':
            footer += f" | Total: {len(song_list)} songs"
        embed.set_footer(text=footer)
        await channel.send(embed=embed)

async def send_song_list(channel):
    """Auto-post: Trakteer list on top (only if any), then the YouTube list. Windowed."""
    if any(song_source(s) == 'trakteer' for s in song_list.values()):
        await send_source_list(channel, 'trakteer')
    await send_source_list(channel, 'youtube')

@bot.command(name='list_song')
async def list_song(ctx, page: str = None):
    """List queue. `!list_song` (window), `!list_song <n>` (YouTube page n), `!list_song all`"""
    try:
        parsed = _parse_page_arg(page)
    except ValueError:
        await ctx.send("❌ Pakai `!list_song`, `!list_song <halaman>` atau `!list_song all`.")
        return
    if parsed is None:
        await send_song_list(ctx.channel)
    elif parsed == 'all':
        if any(song_source(s) == 'trakteer' for s in song_list.values()):
            await send_source_list(ctx.channel, 'trakteer', 'all')
        await send_source_list(ctx.channel, 'youtube', 'all')
    else:
        await send_source_list(ctx.channel, 'youtube', parsed)

@bot.command(name='list_song_trakteer')
async def list_song_trakteer(ctx, page: str = None):
    """Show only the Trakteer list. `!list_song_trakteer [<n>|all]`"""
    try:
        parsed = _parse_page_arg(page)
    except ValueError:
        await ctx.send("❌ Pakai `!list_song_trakteer`, `!list_song_trakteer <halaman>` atau `!list_song_trakteer all`.")
        return
    await send_source_list(ctx.channel, 'trakteer', parsed)

@bot.command(name='goto')
async def goto(ctx, number: int):
    """Set song #number as the one being sung (earlier songs of the same list count as played)"""
    if not song_list:
        await ctx.send("❌ Queue kosong.")
        return
    if number not in song_list:
        await ctx.send(f"❌ Nomor tidak valid. Pilih 1–{current_idx_ref['value']}.")
        return
    source = song_source(song_list[number])
    play_state[source] = number
    play_state['now'] = source
    save_cache()
    label = "Trakteer" if source == 'trakteer' else "YouTube"
    await ctx.send(f"⏭️ Lagu sekarang: **#{number}** {song_list[number][0]} - {song_list[number][1]} (lagu {label} sebelumnya ditandai ✅)")
    await send_song_list(ctx.channel)

@bot.command(name='ngintip')
@commands.cooldown(1, 120, commands.BucketType.guild)
async def ngintip(ctx):
    """Peek at the current song and the next 3 upcoming songs"""
    peek_embed = discord.Embed(
        title="Queue Peek (!ngintip)",
        description="\n".join(build_ngintip_lines(hint=False)),
        color=discord.Color.gold()
    )
    peek_embed.set_author(name=ctx.author.name)
    await ctx.send(embed=peek_embed)

@bot.command(name='delete')
async def delete_song(ctx, song_id: int):
    """Delete a song from the queue by its ID"""
    # Check if queue is empty
    if not song_list:
        await ctx.send("❌ Queue is empty! No songs to delete.")
        return

    # Check if song_id is valid
    if song_id < 1 or song_id > current_idx_ref['value']:
        await ctx.send(f"❌ Invalid song ID! Please use a number between 1 and {current_idx_ref['value']}")
        return

    # Check if song exists in the dictionary
    if song_id not in song_list:
        await ctx.send(f"❌ Song #{song_id} not found in queue!")
        return

    # Check if trying to delete currently playing song
    if song_id == now_playing_id():
        await ctx.send("❌ Cannot delete the currently playing song! Use `!next` / `!next_trakteer` to move on.")
        return

    deleted_song = song_list[song_id]

    # Renumber consecutively; each list's pointer follows (= new number of the last
    # surviving song of that list at or below its old pointer)
    old_ptr = {'youtube': play_state['youtube'], 'trakteer': play_state['trakteer']}
    remaining = {i: s for i, s in song_list.items() if i != song_id}
    song_list.clear()
    new_ptr = {'youtube': 0, 'trakteer': 0}
    for new_id, old_id in enumerate(sorted(remaining), start=1):
        entry = remaining[old_id]
        song_list[new_id] = entry
        src = song_source(entry)
        if old_id <= old_ptr[src]:
            new_ptr[src] = new_id
    current_idx_ref['value'] = len(song_list)
    play_state.update(new_ptr)
    if not song_list or (play_state['now'] and play_state[play_state['now']] == 0):
        play_state['now'] = None

    # Send confirmation message
    embed = discord.Embed(
        title="Song Deleted",
        color=discord.Color.red()
    )
    embed.add_field(
        name=f"Deleted Song #{song_id}",
        value=f"🗑️ {deleted_song[0]} - {deleted_song[1]}",
        inline=False
    )
    embed.add_field(
        name="Queue Status",
        value=f"Remaining songs: {len(song_list)}",
        inline=False
    )
    
    save_cache()
    await ctx.send(embed=embed)


@bot.command(name="help_live")
async def help_live(ctx):
    embed = discord.Embed(
        title="🎶 Lily-bot Documentation",
        description="Panduan lengkap penggunaan Lily-bot selama livestream #Lypsing!",
        color=discord.Color.purple()
    )

    embed.add_field(
        name="👋 Apa itu Lily-bot?",
        value="Lily-bot ada untuk dapetin semua request song di **Live Chat Lily** secara otomatis selama **Livestream #Lypsing** berlangsung!",
        inline=False
    )

    embed.add_field(
        name="📥 Gimana cara Lily-bot dapetin song?",
        value="Selama ada request yang diawali dengan ``!req`` di **Live Chat YouTube Lily**, bot otomatis mengenali request songnya **beserta siapa yang request**.",
        inline=False
    )

    embed.add_field(
        name="💖 Request dari Trakteer",
        value="Gunakan command:\n``!add <nama lagu>-<nama yang request>``\nContoh:\n``!add Miniatur-Ryoda``",
        inline=False
    )

    embed.add_field(
        name="🗑️ Hapus Lagu",
        value="Gunakan:\n``!delete <nomor di listnya>``",
        inline=False
    )

    embed.add_field(
        name="📋 Lihat Queue",
        value=(
            "```\n"
            "!list_song [n|all]         = Semua lagu (total max 20, Trakteer max 5) + status\n"
            "!list_song_trakteer [n|all] = Khusus list Trakteer\n"
            "!current_song              = Lagu yang sedang dinyanyikan\n"
            "!ngintip                   = Intip 3 lagu selanjutnya\n"
            "!export_song               = Copy queue (bisa di-paste ke !add_bulk)\n"
            "```"
        ),
        inline=False
    )

    embed.add_field(
        name="🎛️ Atur Queue",
        value=(
            "```\n"
            "!next                = Lagu YouTube berikutnya\n"
            "!next_trakteer       = Lagu Trakteer berikutnya\n"
            "!goto <nomor>        = Set lagu yang sedang dimainkan\n"
            "!add <lagu>-<req>    = Tambah lagu Trakteer (list Trakteer)\n"
            "!add_yt <lagu>-<req> = Tambah lagu YouTube manual\n"
            "!add_bulk <list>     = Restore queue dari list yang di-paste\n"
            "!ganti <lagu>-<req>  = (in progress) Ganti request\n"
            "```"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Kontrol Bot",
        value=(
            "```\n"
            "!start_live_chat <link>       = Memulai bot (restore queue hari ini)\n"
            "!start_live_chat <link> fresh = Mulai dari queue kosong\n"
            "!stop_live_chat               = Mematikan bot\n"
            "!end_live_stream             = Shoutout requester + matikan bot\n"
            "!pause / !continue            = Stop / lanjut terima chat YouTube\n"
            "!pause <n>                    = Auto-pause chat YouTube di n lagu\n"
            "!help_live                    = Dokumentasi ini\n"
            "```"
        ),
        inline=False
    )

    embed.add_field(
        name="🚀 Mulai Pakai Lily-bot",
        value="Cukup panggil:\n``!start_live_chat <link youtube live>``\n\nEnjoyyy! 🎶",
        inline=False
    )

    embed.set_footer(text="Lily-bot | Powered by #Lypsing")

    await ctx.send(embed=embed)


# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing required argument. Use `!help_live` for command usage.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Command on cooldown! Please wait {error.retry_after:.1f}s.")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Ignore unknown commands
    else:
        print(f"Command error: {error}")
        await ctx.send(f"❌ An error occurred: {error}")

# Run the bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN not found in environment variables")
    elif not YOUTUBE_API_KEY:
        print("Error: YOUTUBE_API_KEY not found in environment variables")
    else:
        # Wire shared state into webserver so the Trakteer webhook can use it
        webserver.setup(bot, song_list, active_streams, current_idx_ref, send_song_list, paused_ref, save_cache)
        webserver.keep_alive()
        bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.DEBUG)