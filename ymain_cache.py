import os
import asyncio
from collections import deque

import discord
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

# -------------------- Config --------------------

load_dotenv()
TOKEN = os.getenv("TOKEN_TWO")
try:
    CREATOR_USER_ID = int(os.getenv("CREATOR_USER_ID"))
except (TypeError, ValueError):
    CREATOR_USER_ID = None

PREFIX = "&"
OG_ROLE_NAME = "OG"

# -------------------- Bot Setup --------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
bot.remove_command("help")

# -------------------- State --------------------
guild_queues = {}        # guild_id -> deque([song, ...])
guild_playing = {}       # guild_id -> bool
skip_votes = {}          # guild_id -> set(user_id)
disconnect_tasks = {}    # guild_id -> asyncio.Task
audio_cache = {}         # webpage_url -> direct_audio_url
guild_volumes = {}       # guild_id -> float (0.0-2.0)

# -------------------- yt-dlp & ffmpeg options --------------------
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "cachedir": False,
}

# Use PCM output with 48kHz stereo (Discord native) — good quality
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": (
        "-vn "
        "-ar 48000 "
        "-ac 2 "
        "-b:a 320k "
        "-bufsize 64k "
        "-af "
        "\""
        "equalizer=f=80:t=q:w=1:g=1.5,"      # gentle low-end warmth
        "equalizer=f=250:t=q:w=1:g=1,"       # low mids lift
        "equalizer=f=1000:t=q:w=1:g=0.5,"    # presence/mid clarity
        "equalizer=f=4000:t=q:w=1:g=0.5,"    # upper mids for definition
        "equalizer=f=8000:t=q:w=1.5:g=-1,"   # tame harsh upper mids
        "equalizer=f=12000:t=q:w=2:g=-1.5"   # smooth high-end cut
        "\""
    )
}



# -------------------- Helpers --------------------
def format_duration(seconds: int) -> str:
    if not seconds:
        return "🔴 Live"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fetch_song(query: str) -> dict:
    """Blocking function that uses yt-dlp to return metadata + direct audio URL."""
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)
        if not info:
            raise RuntimeError("yt-dlp returned no info.")
        if "entries" in info:
            # ytsearch returns 'entries'
            info = info["entries"][0]
        return {
            "title": info.get("title"),
            "webpage_url": info.get("webpage_url"),
            "audio_url": info.get("url"),
            "duration": info.get("duration"),
            "thumbnail": (info.get("thumbnails") or [{}])[-1].get("url"),
            "channel": info.get("uploader", "Unknown"),
        }


async def preload_song(query: str, requester_id: int) -> dict:
    """Run fetch_song in a threadpool and attach requester info."""
    song = await asyncio.to_thread(fetch_song, query)
    song["requester_id"] = requester_id
    # Cache audio_url for quicker reuse (note: direct URLs can expire)
    audio_cache[song["webpage_url"]] = song["audio_url"]
    return song


async def auto_disconnect(ctx):
    """Disconnect after prolonged inactivity (3 mins)."""
    guild_id = ctx.guild.id
    voice_client = ctx.voice_client
    if not voice_client:
        return

    idle_seconds = 0
    check_interval = 5
    while True:
        queue = guild_queues.get(guild_id, deque())
        members = [m for m in voice_client.channel.members if not m.bot]

        if members or voice_client.is_playing() or queue:
            idle_seconds = 0
        else:
            idle_seconds += check_interval

        if idle_seconds >= 180:
            try:
                await voice_client.disconnect()
            except Exception:
                pass
            try:
                await ctx.send("✅ Left the voice channel due to inactivity.")
            except Exception:
                pass

            guild_playing[guild_id] = False
            guild_queues[guild_id] = deque()
            skip_votes[guild_id] = set()
            disconnect_tasks.pop(guild_id, None)
            break

        await asyncio.sleep(check_interval)

# -------------------- Playback --------------------
async def play_next(ctx):
    guild_id = ctx.guild.id
    queue = guild_queues.get(guild_id, deque())

    # nothing to play
    if not queue:
        guild_playing[guild_id] = False
        return

    song = queue.popleft()
    voice_client = ctx.voice_client
    if not voice_client:
        guild_playing[guild_id] = False
        return

    # Mark playing and store current song on voice client for reference
    guild_playing[guild_id] = True
    voice_client.current_song = song

    # Build now-playing embed
    requester_name = "Unknown"
    if song.get("requester_id"):
        member = ctx.guild.get_member(song["requester_id"])
        if member:
            requester_name = member.display_name

    embed = discord.Embed(
        title="🎶 Now Playing",
        description=f"**[{song['title']}]({song['webpage_url']})**",
        color=discord.Color.green()
    )
    embed.add_field(name="Channel", value=song.get("channel", "Unknown"), inline=True)
    embed.add_field(name="Duration", value=format_duration(song.get("duration", 0)), inline=True)
    embed.add_field(name="Requested by", value=requester_name, inline=True)
    embed.set_thumbnail(url=song.get("thumbnail") or "https://i.imgur.com/6Y0G3yI.png")

    try:
        await ctx.send(embed=embed)
    except Exception:
        pass

    # prepare playback
    def after_play(error):
        if error:
            print(f"[Playback Error] {error}")
        # schedule next track
        bot.loop.create_task(play_next(ctx))

    audio_url = audio_cache.get(song["webpage_url"], song["audio_url"])
    # FFmpegPCMAudio will produce PCM data that discord.py can send
    base_source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
    volume = guild_volumes.get(guild_id, 1.0)
    source = discord.PCMVolumeTransformer(base_source, volume=volume)

    try:
        voice_client.play(source, after=after_play)
    except Exception as exc:
        print(f"[Error] voice_client.play failed: {exc}")
        # attempt to continue anyway
        bot.loop.create_task(play_next(ctx))
        return

    # reset/refresh auto-disconnect timer
    if guild_id in disconnect_tasks:
        try:
            disconnect_tasks[guild_id].cancel()
        except Exception:
            pass
    disconnect_tasks[guild_id] = bot.loop.create_task(auto_disconnect(ctx))

# -------------------- Events --------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    ctx = await bot.get_context(message)

    # If the user has OG role, allow prefixless commands
    og_role = discord.utils.get(message.author.roles, name=OG_ROLE_NAME)
    if og_role and ctx.command is None:
        # Try interpreting their message as a command without prefix
        fake_message = message
        fake_message.content = f"{bot.command_prefix}{message.content}"
        ctx = await bot.get_context(fake_message)

    if ctx.command is not None:
        await bot.invoke(ctx)
    else:
        # If it's not a command, ignore and let it be normal chat
        return

# -------------------- Commands --------------------
@bot.command(aliases=["p"])
async def play(ctx, *, query: str):
    """Search & queue a song (YouTube search supported)."""
    guild_id = ctx.guild.id
    guild_queues.setdefault(guild_id, deque())
    guild_playing.setdefault(guild_id, False)
    guild_volumes.setdefault(guild_id, 1.0)

    # Ensure bot is connected to VC
    if not ctx.voice_client:
        if ctx.author.voice and ctx.author.voice.channel:
            try:
                await ctx.author.voice.channel.connect()
            except Exception as e:
                await ctx.send(f"❌ Could not connect to voice channel: {e}")
                return
        else:
            await ctx.send("❌ You must be in a voice channel to use this command.")
            return

    # immediate feedback
    await ctx.send(f"📥 Loading **{query}** — fetching metadata...")

    # fetch metadata in background
    try:
        song = await preload_song(query, ctx.author.id)
    except Exception as e:
        await ctx.send(f"❌ Error loading track: {e}")
        return

    guild_queues[guild_id].append(song)

    embed = discord.Embed(
        title="✅ Added to Queue",
        description=f"**[{song['title']}]({song['webpage_url']})**",
        color=discord.Color.dark_grey()
    )
    embed.add_field(name="Duration", value=format_duration(song.get("duration")), inline=True)
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    await ctx.send(embed=embed)

    # start playback if not already playing
    if not guild_playing[guild_id]:
        await play_next(ctx)


@bot.command(aliases=["s"])
async def skip(ctx):
    """Vote to skip the current song. Requester or OG override."""
    voice_client = ctx.voice_client
    guild_id = ctx.guild.id

    if not voice_client or not voice_client.is_playing():
        await ctx.send("❌ Nothing is currently playing.")
        return

    member = ctx.author
    if not member.voice or not member.voice.channel:
        await ctx.send("❌ You must be in the voice channel to vote skip.")
        return

    # init skip set
    skip_votes.setdefault(guild_id, set())

    # current song info
    current_song = getattr(voice_client, "current_song", None)

    # Requester override
    if current_song and member.id == current_song.get("requester_id"):
        voice_client.stop()
        skip_votes[guild_id].clear()
        await ctx.send("⏭️ Skipped — requester override.")
        return

    # OG override
    og_role = discord.utils.get(member.roles, name=OG_ROLE_NAME)
    if og_role:
        voice_client.stop()
        skip_votes[guild_id].clear()
        await ctx.send("⏭️ Skipped — OG override.")
        return

    # register vote
    skip_votes[guild_id].add(member.id)
    vc_members = [m for m in member.voice.channel.members if not m.bot]
    votes_needed = max(1, (len(vc_members) // 2))
    current_votes = len(skip_votes[guild_id])

    if current_votes >= votes_needed:
        voice_client.stop()
        skip_votes[guild_id].clear()
        await ctx.send(f"⏭️ Skip passed ({current_votes}/{len(vc_members)}).")
    else:
        await ctx.send(f"⏭️ Skip vote registered ({current_votes}/{votes_needed})")


@bot.command(aliases=["st"])
async def stop(ctx):
    """Clear queue and disconnect. Creator-only (or owner)."""
    if CREATOR_USER_ID and ctx.author.id != CREATOR_USER_ID:
        await ctx.send("❌ Only the bot creator may use this command.")
        return

    guild_id = ctx.guild.id
    if ctx.voice_client:
        guild_queues.setdefault(guild_id, deque()).clear()
        guild_playing[guild_id] = False
        try:
            await ctx.voice_client.disconnect()
        except Exception:
            pass
        await ctx.send("🛑 Stopped playback and disconnected.")
    else:
        await ctx.send("❌ I'm not connected to a voice channel.")

    if guild_id in disconnect_tasks:
        try:
            disconnect_tasks[guild_id].cancel()
        except Exception:
            pass
        disconnect_tasks.pop(guild_id, None)


@bot.command(aliases=["q"])
async def queue(ctx):
    """Show the current queue."""
    guild_id = ctx.guild.id
    queue = guild_queues.get(guild_id, deque())
    vc = ctx.voice_client

    if not queue and not getattr(vc, "current_song", None):
        await ctx.send("📭 Queue is empty.")
        return

    embed = discord.Embed(title="🎵 Queue", color=discord.Color.orange())

    # Now playing
    if vc and getattr(vc, "current_song", None):
        current = vc.current_song
        requester = "Unknown"
        if current.get("requester_id"):
            m = ctx.guild.get_member(current["requester_id"])
            if m:
                requester = m.display_name
        embed.add_field(
            name="▶ Now Playing",
            value=f"**{current['title']}**\nRequested by: {requester}\nDuration: {format_duration(current.get('duration'))}",
            inline=False
        )

    # Next up
    for idx, s in enumerate(queue):
        pos = idx + 2  # 1 is now playing
        label = "Next" if idx == 0 else f"{pos}th"
        requester = "Unknown"
        if s.get("requester_id"):
            m = ctx.guild.get_member(s["requester_id"])
            if m:
                requester = m.display_name
        embed.add_field(
            name=f"{label}",
            value=f"**{s['title']}**\nRequested by: {requester}\nDuration: {format_duration(s.get('duration'))}",
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command()
async def pause(ctx):
    """Pause playback (requester or creator only)."""
    vc = ctx.voice_client
    if not vc or not vc.is_playing():
        await ctx.send("❌ Nothing is playing.")
        return

    current_song = getattr(vc, "current_song", None)
    if current_song:
        allowed = (ctx.author.id == current_song.get("requester_id")) or (CREATOR_USER_ID and ctx.author.id == CREATOR_USER_ID)
        if not allowed:
            await ctx.send("❌ Only the requester or bot creator can pause.")
            return

    try:
        vc.pause()
        await ctx.send("⏸️ Paused playback.")
    except Exception as e:
        await ctx.send(f"❌ Could not pause: {e}")


@bot.command(aliases=["rm"])
async def remove(ctx, queue_no: int):
    """Remove a song from the queue by queue number (2 = next)."""
    guild_id = ctx.guild.id
    queue = guild_queues.get(guild_id, deque())
    if not queue:
        await ctx.send("📭 Queue is empty.")
        return

    total_len = len(queue) + 1  # +1 for now playing
    if queue_no < 2 or queue_no > total_len:
        await ctx.send(f"❌ Invalid queue number. Use 2 to {total_len}.")
        return

    song = queue[queue_no - 2]
    allowed = (ctx.author.id == song.get("requester_id")) or (CREATOR_USER_ID and ctx.author.id == CREATOR_USER_ID)
    if not allowed:
        await ctx.send("❌ Only the requester or bot creator can remove this track.")
        return

    try:
        queue.remove(song)
        await ctx.send(f"🗑️ Removed **{song['title']}** from the queue.")
    except ValueError:
        await ctx.send("❌ Could not find that track in the queue.")


@bot.command()
async def resume(ctx):
    """Resume playback (requester or creator only)."""
    vc = ctx.voice_client
    if not vc or not vc.is_paused():
        await ctx.send("❌ Nothing is paused.")
        return

    current_song = getattr(vc, "current_song", None)
    if current_song:
        allowed = (ctx.author.id == current_song.get("requester_id")) or (CREATOR_USER_ID and ctx.author.id == CREATOR_USER_ID)
        if not allowed:
            await ctx.send("❌ Only the requester or bot creator can resume.")
            return

    try:
        vc.resume()
        await ctx.send("▶️ Resumed playback.")
    except Exception as e:
        await ctx.send(f"❌ Could not resume: {e}")


@bot.command()
async def volume(ctx, vol: int):
    """Set server playback volume (0-200)."""
    guild_id = ctx.guild.id
    vc = ctx.voice_client
    if not vc or not getattr(vc, "current_song", None):
        await ctx.send("❌ Nothing is playing.")
        return

    if vol < 0 or vol > 200:
        await ctx.send("❌ Volume must be between 0 and 200.")
        return

    guild_volumes[guild_id] = vol / 100.0
    # apply immediately if a source exists
    if hasattr(vc, "source") and isinstance(vc.source, discord.PCMVolumeTransformer):
        try:
            vc.source.volume = guild_volumes[guild_id]
        except Exception:
            pass

    await ctx.send(f"🔊 Volume set to {vol}%.")


@bot.command(name="help", aliases=["h"])
async def help_command(ctx):
    embed = discord.Embed(
        title="🎵 Music Bot — Commands",
        description=f"Prefix: `{PREFIX}`\nOG members may use commands without prefix.",
        color=discord.Color.teal()
    )
    embed.add_field(name="▶ Play", value=f"`{PREFIX}play <query>`\nSearch & add to queue. Alias: `{PREFIX}p`", inline=False)
    embed.add_field(name="📑 Queue", value=f"`{PREFIX}queue` | `{PREFIX}q`\nShow current queue.", inline=False)
    embed.add_field(name="⏭ Skip", value=f"`{PREFIX}skip` | `{PREFIX}s`\nVote to skip (requester/OG override).", inline=False)
    embed.add_field(name="⏸ Pause", value=f"`{PREFIX}pause`\nPause playback (requester/creator only).", inline=False)
    embed.add_field(name="▶ Resume", value=f"`{PREFIX}resume`\nResume playback (requester/creator only).", inline=False)
    embed.add_field(name="🗑 Remove", value=f"`{PREFIX}remove <queue no.>` | `{PREFIX}rm`\nRemove a queue item (requester/creator only).", inline=False)
    embed.add_field(name="🛑 Stop", value=f"`{PREFIX}stop` | `{PREFIX}st`\nStop and disconnect (creator only).", inline=False)
    embed.add_field(name="🔊 Volume", value=f"`{PREFIX}volume <0-200>`\nSet server playback volume.", inline=False)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    await ctx.send(embed=embed)

# -------------------- Run --------------------
if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: TOKEN_TWO not found in environment.")
    else:
        bot.run(TOKEN)
