import discord
from discord.ext import commands
import boto3
import asyncio
import os
import aiohttp
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN       = os.getenv("DISCORD_TOKEN")
AWS_REGION          = os.getenv("AWS_REGION", "us-east-1")
EC2_INSTANCE_ID     = os.getenv("EC2_INSTANCE_ID")
YOUTUBE_CHANNEL_URL = os.getenv("YOUTUBE_CHANNEL_URL", "")
SCHEDULER_ROLE_ARN  = os.getenv("SCHEDULER_ROLE_ARN", "arn:aws:iam::853027285668:role/EventBridgeEC2Role")

TEAM_CHAT_CHANNEL     = "team-chat"
STREAMERGENCY_CHANNEL = "streamergency"

EC2USER_ROLE   = os.getenv("START_ROLE", "EC2User")
EC2ADMIN_ROLE  = os.getenv("STOP_ROLE", "EC2Admin")
BOTNOTIFY_ROLE = "BotNotify"

EST = ZoneInfo("America/New_York")

# Runtime state (session only)
server_start_time = None
auto_stop_task    = None
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── AWS clients ───────────────────────────────────────────────────────────────

def get_ec2_client():
    return boto3.client(
        "ec2",
        region_name=AWS_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )

def get_scheduler_client():
    return boto3.client(
        "scheduler",
        region_name=AWS_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )

def get_instance_state() -> dict:
    ec2 = get_ec2_client()
    resp = ec2.describe_instances(InstanceIds=[EC2_INSTANCE_ID])
    instance = resp["Reservations"][0]["Instances"][0]
    return {
        "state": instance["State"]["Name"],
        "public_ip": instance.get("PublicIpAddress"),
    }

# ── EventBridge Scheduler ─────────────────────────────────────────────────────

def make_schedule_name(label: str, action: str) -> str:
    """e.g. sps-start-20260510-1800, sps-stop-20260510-2300"""
    return f"sps-{action}-{label}"

def create_ec2_schedule(name: str, dt: datetime, action: str):
    """Create a one-time EventBridge schedule to start or stop the EC2."""
    scheduler = get_scheduler_client()
    ec2_action = "StartInstances" if action == "start" else "StopInstances"
    # Convert to UTC for EventBridge
    dt_utc = dt.astimezone(ZoneInfo("UTC"))
    schedule_expr = f"at({dt_utc.strftime('%Y-%m-%dT%H:%M:%S')})"

    scheduler.create_schedule(
        Name=name,
        ScheduleExpression=schedule_expr,
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": f"arn:aws:ec2:{AWS_REGION}::instance/{EC2_INSTANCE_ID}",
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": "{}",
            "EcsParameters": None,
            "Arn": f"arn:aws:ssm:{AWS_REGION}::automation-definition/AWS-StartEC2Instance:$DEFAULT" if action == "start"
                   else f"arn:aws:ssm:{AWS_REGION}::automation-definition/AWS-StopEC2Instance:$DEFAULT",
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": f'{{"InstanceId":["{EC2_INSTANCE_ID}"]}}',
        },
        ActionAfterCompletion="DELETE",
    )

def create_ec2_schedule_v2(name: str, dt: datetime, action: str):
    """Create schedule using EC2 API directly via EventBridge."""
    scheduler = get_scheduler_client()
    dt_utc = dt.astimezone(ZoneInfo("UTC"))
    schedule_expr = f"at({dt_utc.strftime('%Y-%m-%dT%H:%M:%S')})"
    ec2_action_arn = (
        f"arn:aws:scheduler:::aws-sdk:ec2:startInstances"
        if action == "start"
        else f"arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    )
    scheduler.create_schedule(
        Name=name,
        ScheduleExpression=schedule_expr,
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        Target={
            "Arn": ec2_action_arn,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": f'{{"InstanceIds":["{EC2_INSTANCE_ID}"]}}',
        },
        ActionAfterCompletion="DELETE",
    )

def delete_schedule(name: str):
    try:
        get_scheduler_client().delete_schedule(Name=name)
    except Exception:
        pass

def list_sps_schedules() -> list:
    scheduler = get_scheduler_client()
    resp = scheduler.list_schedules(NamePrefix="sps-")
    return resp.get("Schedules", [])

# ── YouTube ───────────────────────────────────────────────────────────────────

async def check_youtube_live(channel_url: str) -> dict:
    url = channel_url.rstrip("/") + "/live"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            html = await resp.text()
    is_live = '"isLive":true' in html or 'isLiveBroadcast' in html
    title = None
    if is_live:
        match = re.search(r'"title":"([^"]+)"', html)
        if match:
            title = match.group(1)
    return {"live": is_live, "title": title}

# ── Helpers ───────────────────────────────────────────────────────────────────

def has_role(ctx, role_name):
    if not role_name:
        return True
    return any(r.name == role_name for r in ctx.author.roles)

async def get_channel_by_name(name):
    for guild in bot.guilds:
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch:
            return ch
    return None

async def post_to_team_chat(msg):
    ch = await get_channel_by_name(TEAM_CHAT_CHANNEL)
    if ch:
        await ch.send(msg)

async def post_to_streamergency(msg):
    ch = await get_channel_by_name(STREAMERGENCY_CHANNEL)
    if ch:
        await ch.send(msg)

def format_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"

# ── Auto-stop (for manual !startserver) ──────────────────────────────────────

async def schedule_auto_stop_local(stop_dt: datetime):
    global auto_stop_task
    if auto_stop_task:
        auto_stop_task.cancel()
    auto_stop_task = asyncio.create_task(_auto_stop_local(stop_dt))

async def _auto_stop_local(stop_dt: datetime):
    delay = (stop_dt - datetime.now(EST)).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        info = get_instance_state()
        if info["state"] == "running":
            get_ec2_client().stop_instances(InstanceIds=[EC2_INSTANCE_ID])
            await post_to_team_chat("🛑 Server auto-stopped (6-hour limit reached).")
    except Exception as e:
        await post_to_team_chat(f"⚠️ Auto-stop failed: `{e}`")

# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Managing EC2 instance: {EC2_INSTANCE_ID} in {AWS_REGION}")

# ── Commands: Everyone ────────────────────────────────────────────────────────

@bot.command(name="serverstatus")
async def server_status(ctx):
    try:
        info = get_instance_state()
        state = info["state"]
        icons = {"running": "🟢", "stopped": "🔴", "pending": "🟡", "stopping": "🟠", "shutting-down": "🟠"}
        await ctx.reply(f"{icons.get(state, '⚪')} Server is **{state}**")
    except Exception as e:
        await ctx.reply(f"❌ Error: `{e}`")


@bot.command(name="streamstatus")
async def stream_status(ctx):
    if not YOUTUBE_CHANNEL_URL:
        return await ctx.reply("⚠️ YouTube channel is not configured.")
    await ctx.reply("🔍 Checking stream status...")
    try:
        result = await check_youtube_live(YOUTUBE_CHANNEL_URL)
        if result["live"]:
            title = result["title"] or "Live Stream"
            await ctx.reply(f"🔴 **Stream is LIVE!**\n📺 {title}\n🔗 {YOUTUBE_CHANNEL_URL}/live")
        else:
            await ctx.reply("⚫ Stream is currently **offline**.")
    except Exception as e:
        await ctx.reply(f"❌ Error: `{e}`")


@bot.command(name="viewschedule")
async def view_schedule(ctx):
    try:
        schedules = list_sps_schedules()
        if not schedules:
            return await ctx.reply("📅 No upcoming scheduled windows.")

        # Pair up start/stop by their label
        pairs = {}
        for s in schedules:
            name = s["Name"]  # e.g. sps-start-20260510-1800
            parts = name.split("-", 2)  # ['sps', 'start'/'stop', 'label']
            if len(parts) < 3:
                continue
            action = parts[1]
            label  = parts[2]
            if label not in pairs:
                pairs[label] = {}
            pairs[label][action] = s

        if not pairs:
            return await ctx.reply("📅 No upcoming scheduled windows.")

        lines = ["**📅 Upcoming Server Schedule (EST)**"]
        for label, p in sorted(pairs.items()):
            start_s = p.get("start")
            stop_s  = p.get("stop")
            start_str = f"Start: `{label}`" if not start_s else ""
            # Parse UTC time back to EST for display
            def fmt(s):
                expr = s.get("ScheduleExpression", "")
                # at(2026-05-10T22:00:00)
                m = re.search(r'at\((.+?)\)', expr)
                if m:
                    dt = datetime.fromisoformat(m.group(1)).replace(tzinfo=ZoneInfo("UTC")).astimezone(EST)
                    return dt.strftime("%a %b %d, %I:%M %p EST")
                return "unknown"

            start_str = fmt(start_s) if start_s else "—"
            stop_str  = fmt(stop_s)  if stop_s  else "—"
            lines.append(f"`{label}` ▶️ {start_str} → ⏹️ {stop_str}")

        await ctx.reply("\n".join(lines))
    except Exception as e:
        await ctx.reply(f"❌ Error: `{e}`")


@bot.command(name="serverhelp")
async def server_help(ctx):
    await ctx.reply(
        "**🖥️ SPS Server Bot Commands**\n\n"
        "**Everyone**\n"
        "`!serverstatus` — Check if server is running\n"
        "`!streamstatus` — Check if YouTube stream is live\n"
        "`!viewschedule` — View upcoming server schedule\n"
        "`!serverhelp`   — Show this message\n\n"
        "**EC2User**\n"
        "`!startserver`  — Start the server (auto-stops in 6h)\n\n"
        "**EC2Admin**\n"
        "`!stopserver`   — Stop the server immediately\n"
        "`!schedule <date> <start> [stop]` — Schedule a window\n"
        "  e.g. `!schedule 2026-05-10 18:00 23:00`\n"
        "`!cancelschedule <label>` — Cancel a scheduled window\n\n"
        "**BotNotify**\n"
        "`!starting`                — Match starting now!\n"
        "`!upnext <scene> [mins]`   — Announce next scene\n"
        "`!adbreak [mins]`          — Ad break starting\n"
        "`!td <message>`            — Tech difficulties alert\n"
        "`!serveruptime`            — How long server has been up\n"
        "`!timeuntilstop`           — Time until auto-stop\n"
    )

# ── Commands: EC2User ─────────────────────────────────────────────────────────

@bot.command(name="startserver", aliases=["start"])
async def start_server(ctx):
    if not has_role(ctx, EC2USER_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    await ctx.reply("🔍 Checking server status...")
    try:
        info = get_instance_state()
        state = info["state"]
        if state == "running":
            return await ctx.reply("✅ Server is **already running**!")
        if state in ("pending", "stopping", "shutting-down"):
            return await ctx.reply(f"⚠️ Server is currently **{state}** — try again in a moment.")
        if state == "stopped":
            await ctx.reply("🚀 Starting the server... this may take 30–60 seconds.")
            get_ec2_client().start_instances(InstanceIds=[EC2_INSTANCE_ID])
            global server_start_time
            server_start_time = datetime.now(EST)
            stop_dt = server_start_time + timedelta(hours=6)
            await schedule_auto_stop_local(stop_dt)
            msg = await ctx.reply("⏳ Waiting for server to come online...")
            for _ in range(18):
                await asyncio.sleep(5)
                if get_instance_state()["state"] == "running":
                    await msg.edit(content=f"✅ Server is **online**! Auto-stop at {stop_dt.strftime('%I:%M %p EST')}.")
                    await post_to_team_chat(f"🟢 Server started by **{ctx.author.display_name}**. Auto-stop at {stop_dt.strftime('%I:%M %p EST')}.")
                    return
            await msg.edit(content="⚠️ Server is taking longer than expected. Use `!serverstatus` to check.")
    except Exception as e:
        await ctx.reply(f"❌ Error: `{e}`")

# ── Commands: EC2Admin ────────────────────────────────────────────────────────

@bot.command(name="stopserver", aliases=["stop"])
async def stop_server(ctx):
    if not has_role(ctx, EC2ADMIN_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    try:
        info = get_instance_state()
        if info["state"] != "running":
            return await ctx.reply(f"⚠️ Server is not running (state: **{info['state']}**).")
        global auto_stop_task
        if auto_stop_task:
            auto_stop_task.cancel()
            auto_stop_task = None
        get_ec2_client().stop_instances(InstanceIds=[EC2_INSTANCE_ID])
        await ctx.reply("🛑 Server is shutting down...")
        await post_to_team_chat(f"🔴 Server stopped by **{ctx.author.display_name}**.")
    except Exception as e:
        await ctx.reply(f"❌ Error: `{e}`")


@bot.command(name="schedule")
async def schedule_server(ctx, date: str, start_time: str, stop_time: str = None):
    if not has_role(ctx, EC2ADMIN_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    try:
        start_dt = datetime.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=EST)
        stop_dt  = datetime.strptime(f"{date} {stop_time}", "%Y-%m-%d %H:%M").replace(tzinfo=EST) if stop_time else start_dt + timedelta(hours=6)

        if start_dt < datetime.now(EST):
            return await ctx.reply("⚠️ Start time is in the past.")
        if stop_dt <= start_dt:
            return await ctx.reply("⚠️ Stop time must be after start time.")

        label      = start_dt.strftime("%Y%m%d-%H%M")
        start_name = make_schedule_name(label, "start")
        stop_name  = make_schedule_name(label, "stop")

        create_ec2_schedule_v2(start_name, start_dt, "start")
        create_ec2_schedule_v2(stop_name,  stop_dt,  "stop")

        await ctx.reply(
            f"✅ Scheduled (`{label}`):\n"
            f"▶️ Start: {start_dt.strftime('%a %b %d, %I:%M %p EST')}\n"
            f"⏹️ Stop:  {stop_dt.strftime('%I:%M %p EST')}"
        )
        await post_to_team_chat(
            f"📅 Server scheduled by **{ctx.author.display_name}** (`{label}`):\n"
            f"▶️ {start_dt.strftime('%a %b %d, %I:%M %p EST')} → ⏹️ {stop_dt.strftime('%I:%M %p EST')}"
        )
    except ValueError:
        await ctx.reply("⚠️ Invalid format. Use: `!schedule YYYY-MM-DD HH:MM [HH:MM]`\nExample: `!schedule 2026-05-10 18:00 23:00`")
    except Exception as e:
        await ctx.reply(f"❌ Error creating schedule: `{e}`")


@bot.command(name="cancelschedule")
async def cancel_schedule(ctx, label: str):
    if not has_role(ctx, EC2ADMIN_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    try:
        delete_schedule(make_schedule_name(label, "start"))
        delete_schedule(make_schedule_name(label, "stop"))
        await ctx.reply(f"✅ Schedule `{label}` cancelled.")
        await post_to_team_chat(f"❌ Schedule `{label}` cancelled by **{ctx.author.display_name}**.")
    except Exception as e:
        await ctx.reply(f"❌ Error: `{e}`")

# ── Commands: BotNotify ───────────────────────────────────────────────────────

@bot.command(name="starting")
async def match_starting(ctx):
    if not has_role(ctx, BOTNOTIFY_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    await post_to_streamergency(f"⚔️ **Match starting now!** (from {ctx.author.display_name})")
    await ctx.reply("✅ Posted to #streamergency.")


@bot.command(name="upnext")
async def up_next(ctx, scene: str, minutes: int = None):
    if not has_role(ctx, BOTNOTIFY_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    if minutes:
        msg = f"🎬 **Up next: {scene}** — in {minutes} minute{'s' if minutes != 1 else ''}! (from {ctx.author.display_name})"
    else:
        msg = f"🎬 **Up next: {scene}** — starting now! (from {ctx.author.display_name})"
    await post_to_streamergency(msg)
    await ctx.reply("✅ Posted to #streamergency.")


@bot.command(name="adbreak")
async def ad_break(ctx, minutes: int = None):
    if not has_role(ctx, BOTNOTIFY_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    if minutes:
        msg = f"📢 **Ad break — back in {minutes} minute{'s' if minutes != 1 else ''}!** (from {ctx.author.display_name})"
    else:
        msg = f"📢 **Ad break starting now!** (from {ctx.author.display_name})"
    await post_to_streamergency(msg)
    await ctx.reply("✅ Posted to #streamergency.")


@bot.command(name="td")
async def tech_difficulties(ctx, *, message: str):
    if not has_role(ctx, BOTNOTIFY_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    await post_to_streamergency(
        f"🔧 **Technical difficulties: {message}** — please stand by. (from {ctx.author.display_name})"
    )
    await ctx.reply("✅ Posted to #streamergency.")


@bot.command(name="serveruptime")
async def server_uptime(ctx):
    if not has_role(ctx, BOTNOTIFY_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    if not server_start_time:
        return await ctx.reply("⚠️ No uptime recorded this session — server may have been started before the bot.")
    elapsed = (datetime.now(EST) - server_start_time).total_seconds()
    await ctx.reply(f"⏱️ Server has been running for **{format_duration(elapsed)}**.")


@bot.command(name="timeuntilstop")
async def time_until_stop(ctx):
    if not has_role(ctx, BOTNOTIFY_ROLE):
        return await ctx.reply("❌ You do not have permission to use this command.")
    if not server_start_time:
        return await ctx.reply("⚠️ No stop time recorded this session.")
    stop_dt   = server_start_time + timedelta(hours=6)
    remaining = (stop_dt - datetime.now(EST)).total_seconds()
    if remaining <= 0:
        return await ctx.reply("⚠️ Auto-stop should have already triggered.")
    await ctx.reply(f"⏳ Server auto-stops in **{format_duration(remaining)}** (at {stop_dt.strftime('%I:%M %p EST')}).")


bot.run(DISCORD_TOKEN)
