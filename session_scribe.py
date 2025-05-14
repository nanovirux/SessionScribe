#!/usr/bin/env python3
import os
from dotenv import load_dotenv

import discord
from discord.ext import commands
from collections import defaultdict
from supabase import create_client
from discord.utils import utcnow

# ─── LOAD SECRETS ─────────────────────────────────────────────────────────────
load_dotenv()  # loads from .env into os.environ

TOKEN        = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([TOKEN, SUPABASE_URL, SUPABASE_KEY]):
    raise RuntimeError(
        "Missing one of DISCORD_TOKEN, SUPABASE_URL, or SUPABASE_KEY in environment"
    )

# ─── INIT CLIENTS ──────────────────────────────────────────────────────────────
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

intents = discord.Intents.default()
intents.members         = True    # join/remove events
intents.messages        = True    # on_message
intents.message_content = True    # if you need message content

bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory counters: { guild_id: { user_id: { channel_id: count } } }
message_counters = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))


# ─── EVENTS ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    # set presence & log
    await bot.change_presence(activity=discord.Game(name="SessionScribe"))
    print(f"SessionScribe is online as {bot.user} (ID: {bot.user.id})")

    # ─── BOOTSTRAP EXISTING MEMBERS ──────────────────────────────
    now = utcnow().isoformat()
    for guild in bot.guilds:
        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue

            # ensure in-memory counter
            message_counters[guild.id][member.id].clear()

            # check for an open session in Supabase
            res = await supabase.table("sessions") \
                .select("id") \
                .eq("guild_id", guild.id) \
                .eq("user_id", member.id) \
                .is_("leave_time", None) \
                .execute()

            if not (res.data or []):
                # no session in progress → create one
                await supabase.table("sessions").insert({
                    "guild_id":  guild.id,
                    "user_id":   member.id,
                    "join_time": now
                }).execute()
                print(f"[SessionScribe][INIT] Started session for {member} at {now}")
            else:
                print(f"[SessionScribe][INIT] Found existing session for {member}")

    print("✅ Bootstrapped sessions for all current members.")


@bot.event
async def on_member_join(member):
    # reset in-memory counter
    message_counters[member.guild.id][member.id].clear()
    join_time = utcnow().isoformat()
    print(f"[SessionScribe][JOIN] {member} at {join_time}")

    # persist in Supabase
    await supabase.table("sessions").insert({
        "guild_id":  member.guild.id,
        "user_id":   member.id,
        "join_time": join_time
    }).execute()


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    g, u, c = message.guild.id, message.author.id, message.channel.id
    if u in message_counters[g]:
        message_counters[g][u][c] += 1

    await bot.process_commands(message)


@bot.event
async def on_member_remove(member):
    g, u = member.guild.id, member.id
    leave_time = utcnow().isoformat()
    counts = message_counters[g].pop(u, {})
    channel_counts = { str(cid): cnt for cid, cnt in counts.items() }

    print(f"[SessionScribe][LEAVE] {member} at {leave_time} → {channel_counts or 'none'}")

    # update the open session row
    await supabase.table("sessions") \
      .update({
        "leave_time":     leave_time,
        "channel_counts": channel_counts
      }) \
      .eq("guild_id",     g) \
      .eq("user_id",      u) \
      .is_("leave_time",  None) \
      .order("join_time", desc=True) \
      .limit(1) \
      .execute()


# ─── MODERATOR COMMANDS ───────────────────────────────────────────────────────

@bot.command(name="stats")
@commands.has_permissions(manage_guild=True)
async def stats(ctx, member: discord.Member = None):
    """Show the last session stats for a user."""
    member = member or ctx.author
    res = await supabase.table("sessions") \
        .select("*") \
        .eq("guild_id", ctx.guild.id) \
        .eq("user_id",  member.id) \
        .order("join_time", desc=True) \
        .limit(1) \
        .execute()

    data = res.data or []
    if not data:
        return await ctx.send(f"No sessions found for {member.mention}.")

    ses = data[0]
    jc = ses["join_time"]
    lt = ses.get("leave_time") or "still here"
    counts = ses.get("channel_counts", {})

    lines = [
        f"**Session for {member.mention}:**",
        f"• Joined: `{jc}`",
        f"• Left:   `{lt}`",
        "",
        "**Message counts:**"
    ]
    if counts:
        for cid, cnt in counts.items():
            chan = ctx.guild.get_channel(int(cid))
            name = f"#{chan.name}" if chan else cid
            lines.append(f"• {name}: {cnt}")
    else:
        lines.append("• No messages recorded.")

    await ctx.send("\n".join(lines))


@bot.command(name="active")
@commands.has_permissions(manage_guild=True)
async def active(ctx):
    """List all users currently in an open session."""
    res = await supabase.table("sessions") \
        .select("user_id") \
        .eq("guild_id", ctx.guild.id) \
        .is_("leave_time", None) \
        .execute()

    users = {row["user_id"] for row in (res.data or [])}
    if not users:
        return await ctx.send("No active sessions right now.")

    mentions = []
    for uid in users:
        m = ctx.guild.get_member(int(uid))
        mentions.append(m.mention if m else f"`{uid}`")

    await ctx.send("**Active sessions:** " + ", ".join(mentions))


# ─── RUN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
