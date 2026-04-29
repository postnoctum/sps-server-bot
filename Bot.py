import discord
from discord.ext import commands
import boto3
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
AWS_REGION      = os.getenv("AWS_REGION", "us-east-1")
EC2_INSTANCE_ID = os.getenv("EC2_INSTANCE_ID")

# Optional: restrict commands to specific channel IDs (comma-separated)
ALLOWED_CHANNEL_IDS = [
    int(x) for x in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()
]

# Role required to use !startserver (leave empty = anyone can use it)
START_ROLE = os.getenv("START_ROLE", "")  # e.g. "ServerUser"

# Role required to use !stopserver (leave empty = anyone can use it)
STOP_ROLE = os.getenv("STOP_ROLE", "")   # e.g. "ServerAdmin"
# ────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def get_ec2_client():
    return boto3.client(
        "ec2",
        region_name=AWS_REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )

def get_instance_state() -> dict:
    """Returns {'state': str, 'public_ip': str|None}"""
    ec2 = get_ec2_client()
    resp = ec2.describe_instances(InstanceIds=[EC2_INSTANCE_ID])
    instance = resp["Reservations"][0]["Instances"][0]
    return {
        "state": instance["State"]["Name"],
        "public_ip": instance.get("PublicIpAddress"),
    }

def check_channel(ctx):
    if ALLOWED_CHANNEL_IDS and ctx.channel.id not in ALLOWED_CHANNEL_IDS:
        return False
    return True

def has_role(ctx, role_name):
    if not role_name:
        return True
    return any(r.name == role_name for r in ctx.author.roles)

# ── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Managing EC2 instance: {EC2_INSTANCE_ID} in {AWS_REGION}")

# ── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name="startserver", aliases=["start"])
async def start_server(ctx):
    """Start the EC2 instance."""
    if not check_channel(ctx):
        return await ctx.send("❌ This command isn't allowed in this channel.")
    if not has_role(ctx, START_ROLE):
        return await ctx.send(f"❌ You need the **{START_ROLE}** role to start the server.")

    await ctx.send("🔍 Checking server status...")

    try:
        info = get_instance_state()
        state = info["state"]

        if state == "running":
            ip = info["public_ip"] or "unknown"
            return await ctx.send(f"✅ Server is **already running**!\n🌐 IP: `{ip}`")

        if state in ("pending", "stopping", "shutting-down"):
            return await ctx.send(f"⚠️ Server is currently **{state}** — try again in a moment.")

        if state == "stopped":
            await ctx.send("🚀 Starting the server... this may take 30–60 seconds.")
            ec2 = get_ec2_client()
            ec2.start_instances(InstanceIds=[EC2_INSTANCE_ID])

            # Poll until running (max ~90 seconds)
            msg = await ctx.send("⏳ Waiting for server to come online...")
            for _ in range(18):
                await asyncio.sleep(5)
                info = get_instance_state()
                if info["state"] == "running":
                    ip = info["public_ip"] or "unknown"
                    await msg.edit(content=f"✅ Server is **online**!\n🌐 IP: `{ip}`")
                    return

            await msg.edit(content="⚠️ Server is taking longer than expected. Use `!status` to check.")

    except Exception as e:
        await ctx.send(f"❌ Error: `{e}`")


@bot.command(name="stopserver", aliases=["stop"])
async def stop_server(ctx):
    """Stop the EC2 instance."""
    if not check_channel(ctx):
        return await ctx.send("❌ This command isn't allowed in this channel.")
    if not has_role(ctx, STOP_ROLE):
        return await ctx.send(f"❌ You need the **{STOP_ROLE}** role to stop the server.")

    try:
        info = get_instance_state()
        if info["state"] != "running":
            return await ctx.send(f"⚠️ Server is not running (current state: **{info['state']}**).")

        ec2 = get_ec2_client()
        ec2.stop_instances(InstanceIds=[EC2_INSTANCE_ID])
        await ctx.send("🛑 Server is shutting down...")

    except Exception as e:
        await ctx.send(f"❌ Error: `{e}`")


@bot.command(name="status")
async def server_status(ctx):
    """Check the current state of the EC2 instance."""
    if not check_channel(ctx):
        return await ctx.send("❌ This command isn't allowed in this channel.")

    try:
        info = get_instance_state()
        state = info["state"]
        ip    = info["public_ip"] or "—"

        icons = {
            "running":       "🟢",
            "stopped":       "🔴",
            "pending":       "🟡",
            "stopping":      "🟠",
            "shutting-down": "🟠",
        }
        icon = icons.get(state, "⚪")
        await ctx.send(f"{icon} Server is **{state}**\n🌐 IP: `{ip}`")

    except Exception as e:
        await ctx.send(f"❌ Error: `{e}`")


@bot.command(name="serverhelp")
async def server_help(ctx):
    """Show available commands."""
    help_text = (
        "**🖥️ EC2 Server Bot Commands**\n"
        "`!startserver` — Start the server\n"
        "`!stopserver`  — Stop the server\n"
        "`!status`      — Check server state & IP\n"
        "`!serverhelp`  — Show this message\n"
    )
    await ctx.send(help_text)


bot.run(DISCORD_TOKEN)
