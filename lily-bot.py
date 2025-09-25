import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import googleapiclient.discovery
import re
import webserver

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')

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
current_idx = 0
played_idx = 0

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
    global current_idx, song_list  # Fix: Add global declaration
    
    live_chat_id = await get_live_chat_id(video_id)
    
    if not live_chat_id:
        await discord_channel.send("❌ This video doesn't have an active live chat or isn't currently live.")
        return
    
    await discord_channel.send(f"✅ Started monitoring live chat for video: `{video_id}`")
    
    next_page_token = None
    processed_messages = set()  # To avoid duplicate messages
    
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
                
                message_text = item["snippet"]["displayMessage"]
                author = item["authorDetails"]["displayName"]
                
                # Only process messages that start with !req
                if not message_text.lower().startswith('!req'):
                    continue
                
                # Create an embed for better formatting
                embed = discord.Embed(
                    description=message_text,
                    color=discord.Color.red()
                )
                embed.set_author(name=author)
                embed.set_footer(text="YouTube Live Chat")
                
                # Create song list
                song_list_temp = message_text[5:]

                # Add it to the list
                current_idx += 1
                song_list[current_idx] = [song_list_temp,author]
                
                await discord_channel.send(embed=embed)

            next_page_token = response.get("nextPageToken")
            
            # Wait before polling again (YouTube API recommends polling interval)
            poll_interval = response.get("pollingIntervalMillis", 5000) / 1000
            await asyncio.sleep(max(poll_interval, 20))  # Minimum 20 seconds
            
    except Exception as e:
        await discord_channel.send(f"❌ Error while fetching live chat: {e}")
        print(f"Live chat fetch error: {e}")
    
    # Clean up when done
    if discord_channel.id in active_streams:
        del active_streams[discord_channel.id]
    
    await discord_channel.send("🛑 Stopped monitoring live chat.")

@bot.event
async def on_ready():
    global youtube_client
    print(f"Bot is ready! Logged in as {bot.user.name}")
    
    # Initialize YouTube API client
    if YOUTUBE_API_KEY:
        youtube_client = googleapiclient.discovery.build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        print("YouTube API client initialized")
    else:
        print("Warning: YouTube API key not found!")

@bot.command(name='hello')
async def hello(ctx):
    """Simple hello command"""
    await ctx.send(f"Hello {ctx.author.mention}! 👋")

@bot.command(name='start_live_chat')
async def start_live_chat(ctx, url: str):
    """Start monitoring YouTube live chat and send messages to current Discord channel"""
    global song_list, current_idx, played_idx  # Fix: Reset queue for new stream
    
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
    
    # Reset the queue for new stream
    # song_list = {}
    # current_idx = 0
    # played_idx = 0
    
    # Store the active stream
    active_streams[ctx.channel.id] = video_id
    
    # Start fetching live chat in the background
    bot.loop.create_task(fetch_live_chat_messages(video_id, ctx.channel))
    
    await ctx.send(f"🔄 Starting to monitor live chat for video: `{video_id}`")

@bot.command(name='stop_live_chat')
async def stop_live_chat(ctx):
    """Stop monitoring YouTube live chat for current Discord channel"""
    if ctx.channel.id not in active_streams:
        await ctx.send("❌ No active live chat monitoring in this channel.")
        return
    
    # Remove from active streams (this will stop the fetch loop)
    del active_streams[ctx.channel.id]
    await ctx.send("🛑 Stopped live chat monitoring for this channel.")

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
    """Check the current song"""
    song_embed = discord.Embed(
        title="Lily Current Song",
        color=discord.Color.blue()
    )
    
    # Fix: Better logic for current song
    if not song_list:
        song_embed.add_field(
            name="Current Song",
            value="Tidak ada lagu dalam queue saat ini!",
            inline=False
        )
    elif played_idx == 0:
        if current_idx >= 1:
            song_embed.add_field(
                name="Current Song",
                value=f"Belum ada lagu yang dimainkan. Next: {song_list[1][0]} - {song_list[1][1]}",
                inline=False
            )
        else:
            song_embed.add_field(
                name="Current Song",
                value="Tidak ada lagu dalam queue saat ini!",
                inline=False
            )
    else:
        song_embed.add_field(
            name="Current Song",
            value=f"{song_list[played_idx][0]} - {song_list[played_idx][1]}", 
            inline=False
        )
    await ctx.send(embed=song_embed)

@bot.command(name='next')
async def next(ctx):
    """Move to the next song"""
    global played_idx  # Fix: Add global declaration
    
    next_song = discord.Embed(
        title="Move to the next song",
        color=discord.Color.dark_purple()
    )
    
    # Fix: Simplified and corrected logic
    if not song_list:
        next_song.add_field(
            name="Queue Empty",
            value="Tidak ada lagu dalam queue!",
            inline=False
        )
    elif played_idx == 0 and current_idx >= 1:
        # Start playing the first song
        played_idx = 1
        next_song.add_field(
            name="Now Playing",
            value=f"{song_list[played_idx][0]} - {song_list[played_idx][1]}",
            inline=False
        )
        if current_idx > 1:
            next_song.add_field(
                name="Next Song",
                value=f"{song_list[played_idx + 1][0]} - {song_list[played_idx + 1][1]}",
                inline=False
            )
    elif played_idx < current_idx:
        # Move to next song
        played_idx += 1
        next_song.add_field(
            name="Now Playing",
            value=f"{song_list[played_idx][0]} - {song_list[played_idx][1]}",
            inline=False
        )
        if played_idx < current_idx:
            next_song.add_field(
                name="Next Song",
                value=f"{song_list[played_idx + 1][0]} - {song_list[played_idx + 1][1]}",
                inline=False
            )
        else:
            next_song.add_field(
                name="Next Song",
                value="Tidak ada lagu selanjutnya!",
                inline=False
            )
    else:
        # Already at the last song
        next_song.add_field(
            name="Info",
            value="Sudah di lagu terakhir!",
            inline=False
        )
    
    await ctx.send(embed=next_song)

@bot.command(name='add')
async def add(ctx, *, song):
    """Add song from Trakteer"""
    global current_idx
    if ctx.channel.id in active_streams:
        print(song, song.split("-"))
        song_list_format = "(Trakteer) - " + song.split("-")[0]
        nama_request = song.split("-")[1]
        current_idx += 1

        song_list[current_idx] = [song_list_format,nama_request]
        embed = discord.Embed(
                    description=song_list_format,
                    color=discord.Color.red()
                )
        embed.set_author(name=nama_request)
        embed.set_footer(text="Trakteer Request Chat")
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ No active live chat monitoring in this channel.")

@bot.command(name='list_song')
async def list_song(ctx):
    """Check the List Song Queue with Discord embed limit handling"""
    # Create embed with valid color
    queue_embed = discord.Embed(
        title="List song",
        color=discord.Color.magenta()
    )
    
    # Check if queue is empty
    if not song_list:
        queue_embed.add_field(
            name="Queue Status",
            value="Tidak ada lagu dalam queue!",
            inline=False
        )
    else:
        total_songs = len(song_list)
        
        # Determine which songs to show based on Discord's 25 field limit
        if total_songs <= 24:
            # Show all songs if 24 or fewer
            songs_to_show = list(range(1, current_idx + 1))
        else:
            # Show last 3 played songs + all unplayed songs
            songs_to_show = []
            
            # Get last 3 played songs
            if played_idx > 0:
                start_played = max(1, played_idx - 2)  # Last 3 played songs
                songs_to_show.extend(range(start_played, played_idx + 1))
            
            # Add all unplayed songs
            if played_idx < current_idx:
                songs_to_show.extend(range(played_idx + 1, current_idx + 1))
            
            # Ensure we don't exceed 24 fields (keeping 1 for potential gap indicator)
            if len(songs_to_show) > 23:
                # Prioritize current and upcoming songs
                if played_idx > 0:
                    # Show current + next songs up to limit or end of queue
                    max_next_songs = min(22, current_idx - played_idx)
                    songs_to_show = [played_idx] + list(range(played_idx + 1, played_idx + 1 + max_next_songs))
                else:
                    # Show first 23 songs or all if less
                    max_songs = min(23, current_idx)
                    songs_to_show = list(range(1, max_songs + 1))
        
        # Add gap indicator if we're not showing all songs
        show_gap = total_songs > 24 and played_idx > 3
        
        # Display songs
        for i, song_id in enumerate(songs_to_show):
            # Add gap indicator before current song if needed
            if show_gap and song_id == played_idx and played_idx > 3:
                queue_embed.add_field(
                    name="...",
                    value=f"⏸️ {played_idx - 3} songs skipped for display",
                    inline=False
                )
            
            if song_id < played_idx:
                # Songs that have been played
                queue_embed.add_field(
                    name=f"#{song_id}",
                    value=f"✅ {song_list[song_id][0]} - {song_list[song_id][1]}",
                    inline=False
                )
            elif song_id == played_idx:
                # Currently playing song
                queue_embed.add_field(
                    name=f"#{song_id} 🎵",
                    value=f"▶️ {song_list[song_id][0]} - {song_list[song_id][1]}",
                    inline=False
                )
            else:
                # Upcoming songs
                queue_embed.add_field(
                    name=f"#{song_id}",
                    value=f"⏳ {song_list[song_id][0]} - {song_list[song_id][1]}",
                    inline=False
                )
    
    # Add queue info at the footer
    if song_list:
        queue_embed.set_footer(text=f"{played_idx}/{len(song_list)} played | Total: {len(song_list)} songs")
    else:
        queue_embed.set_footer(text="No songs in queue")
    
    await ctx.send(embed=queue_embed)

@bot.command(name='delete')
async def delete_song(ctx, song_id: int):
    """Delete a song from the queue by its ID"""
    global current_idx, played_idx
    
    # Check if queue is empty
    if not song_list:
        await ctx.send("❌ Queue is empty! No songs to delete.")
        return
    
    # Check if song_id is valid
    if song_id < 1 or song_id > current_idx:
        await ctx.send(f"❌ Invalid song ID! Please use a number between 1 and {current_idx}")
        return
    
    # Check if song exists in the dictionary
    if song_id not in song_list:
        await ctx.send(f"❌ Song #{song_id} not found in queue!")
        return
    
    # Check if trying to delete currently playing song
    if song_id == played_idx:
        await ctx.send("❌ Cannot delete the currently playing song! Use `!next` to skip it.")
        return
    
    # Store song info for confirmation message
    deleted_song = song_list[song_id]
    
    # Delete the song
    del song_list[song_id]
    
    # Adjust played_idx if we deleted a song before the current one
    if song_id < played_idx:
        played_idx -= 1
    
    # Rebuild the song_list with consecutive IDs
    if song_list:
        # Create a new dictionary with consecutive IDs
        old_song_list = song_list.copy()
        song_list.clear()
        
        new_id = 1
        old_played_id = played_idx
        new_played_id = 0
        
        for old_id in range(1, current_idx + 1):
            if old_id in old_song_list:
                song_list[new_id] = old_song_list[old_id]
                
                # Update played_idx to match the new numbering
                if old_id == old_played_id:
                    new_played_id = new_id
                elif old_id < old_played_id and new_played_id == 0:
                    new_played_id = new_id
                
                new_id += 1
        
        # Update global variables
        current_idx = len(song_list)
        played_idx = new_played_id
    else:
        # Queue is now empty
        current_idx = 0
        played_idx = 0
    
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
        name="⚙️ Fitur Lainnya",
        value=(
            "```\n"
            "- !list_song                = Menampilkan semua lagu, lengkap dengan status\n"
            "- !current_song             = Menampilkan lagu yang sedang dinyanyikan\n"
            "- !next                     = Pindah ke lagu selanjutnya\n"
            "- !add <lagu>-<req>         = Menambah lagu (khusus Trakteer)\n"
            "- !start_live_chat <link>   = Memulai bot\n"
            "- !end_live_chat            = Mematikan bot\n"
            "- !ganti <lagu>-<req>       = (in progress) Ganti request\n"
            "- !help_live                = Dokumentasi lainnya\n"
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
        webserver.keep_alive()
        bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.DEBUG)