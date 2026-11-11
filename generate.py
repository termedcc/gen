"""
generate.py — /generate command with LIVE Ubisoft account checking.
"""

import asyncio
import time
import discord
import base64
import aiohttp
from discord import app_commands
from discord.ext import commands

import database as db
import utils

_DEFAULT_COLORS = {"free": 0x57F287, "free+": 0x5865F2, "premium": 0xFEE75C}
CATEGORY_LABELS = {"free": "🟢 Free", "free+": "🔵 Free+", "premium": "⭐ Premium"}

def _category_color(category: str) -> int:
    key = f"color_{category.replace('+', 'plus')}"
    stored = db.get_config(key)
    if stored:
        try:
            return int(stored)
        except (ValueError, TypeError):
            pass
    return _DEFAULT_COLORS.get(category, 0x5865F2)

def parse_account(raw) -> dict:
    """Parse a pipe-delimited account string."""
    if not isinstance(raw, str):
        if isinstance(raw, dict):
            raw = raw.get("credentials") or raw.get("account") or raw.get("data") \
                  or raw.get("value") or raw.get("text") or str(raw)
        else:
            raw = str(raw or "")

    parts = [p.strip() for p in raw.split("|") if p.strip()]
    credentials = parts[0] if parts else raw.strip()
    skin_link = None
    username = None
    detail_lines = []
    currency_fields = []

    for part in parts[1:]:
        if part.startswith("http://") or part.startswith("https://"):
            skin_link = part
            continue
        if " / " in part:
            subs = [s.strip() for s in part.split(" / ") if s.strip()]
            if len(subs) > 1 and all(":" in s or "➡" in s for s in subs):
                for sub in subs:
                    ai = sub.find("➡")
                    ci = sub.find(":")
                    idx = ai if ai != -1 else ci
                    name = sub[:idx].strip()
                    val = sub[idx + 1:].strip()
                    if val.startswith("http://") or val.startswith("https://"):
                        skin_link = val
                        continue
                    currency_fields.append({"name": name, "value": val or "\u200b", "inline": True})
                continue

        ai = part.find("➡")
        ci = part.find(":")
        if ai != -1:
            label, value = part[:ai].strip(), part[ai + 1:].strip()
        elif ci != -1:
            label, value = part[:ci].strip(), part[ci + 1:].strip()
        else:
            detail_lines.append(part)
            continue

        if value.startswith("http://") or value.startswith("https://"):
            skin_link = value
            continue
        if label.lower() == "username" and not username:
            username = value
        detail_lines.append(f"**{label}** ➡️ {value}")

    return {
        "credentials": credentials,
        "skin_link": skin_link,
        "username": username,
        "detail_lines": detail_lines,
        "currency_fields": currency_fields,
    }

async def is_account_valid(account_str: str) -> bool:
    """Hits Ubisoft's API to check if the account is actually alive."""
    # Extract just the email:pass part
    creds = account_str.split("|")[0].strip()
    if ":" not in creds:
        return False

    email, password = creds.split(":", 1)
    
    # Public Ubisoft App ID
    UBI_APP_ID = "39b0e8b8-33df-41da-a9e1-b459f22dbcf2" 
    url = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
    
    b64_auth = base64.b64encode(f"{email}:{password}".encode('utf-8')).decode('utf-8')
    headers = {
        "Content-Type": "application/json",
        "Ubi-AppId": UBI_APP_ID,
        "Authorization": f"Basic {b64_auth}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Timeout is set to 3 seconds so we don't hang if Ubisoft is slow
            async with session.post(url, headers=headers, json={}, timeout=3) as res:
                # 200 = Success. 
                # 429 = Rate Limited (Ubisoft is blocking us). We count it as "valid" 
                # so we don't accidentally burn a good account just because we're being throttled.
                if res.status == 200 or res.status == 429:
                    return True
                return False
    except Exception:
        # If network fails, assume valid to prevent wiping stock.
        return True 

class Generate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="generate", description="Generate an account from stock.")
    @app_commands.describe(category="Account tier to generate")
    @app_commands.choices(category=[
        app_commands.Choice(name="🟢 Free",    value="free"),
        app_commands.Choice(name="🔵 Free+",   value="free+"),
        app_commands.Choice(name="⭐ Premium", value="premium"),
    ])
    @app_commands.guild_only()
    async def generate(self, interaction: discord.Interaction, category: str):

        if db.get_config("gen_offline", "false") == "true":
            embed = discord.Embed(color=0xFEE75C, title="🔌 Generator Offline",
                description="The generator is temporarily offline for maintenance.")
            return await interaction.response.send_message(embeds=[embed], ephemeral=True)

        gen_channel_id = db.get_config("gen_channel")
        if gen_channel_id and str(interaction.channel_id) != str(gen_channel_id):
            return await interaction.response.send_message(f"❌ Head to <#{gen_channel_id}> to generate.", ephemeral=True)

        if db.is_blacklisted(str(interaction.user.id)):
            embed = discord.Embed(color=0xED4245, title="❌ Access Denied",
                description="You are not permitted to use the generator.")
            return await interaction.response.send_message(embeds=[embed], ephemeral=True)

        member = interaction.user if isinstance(interaction.user, discord.Member) \
            else await interaction.guild.fetch_member(interaction.user.id)

        if not utils.has_generate_access(member, category):
            role_id = utils.get_category_role_id(category)
            role_ref = f"<@&{role_id}>" if role_id else f"**{category}**"
            embed = discord.Embed(color=0xED4245, title="❌ No Access",
                description=f"You need the {role_ref} role to generate **{category}** accounts.")
            return await interaction.response.send_message(embeds=[embed], ephemeral=True)

        cat_key = category.replace("+", "plus")
        try:
            cooldown = int(db.get_config(f"cooldown_{cat_key}") or db.get_config("gen_cooldown") or 0)
        except (ValueError, TypeError):
            cooldown = 0
        now = int(time.time())
        user = db.get_user(str(interaction.user.id))
        last_gen = user.get(f"last_gen_{cat_key}", 0)
        remaining = (last_gen + cooldown) - now

        if cooldown > 0 and remaining > 0 and not utils.is_owner(str(interaction.user.id)):
            embed = discord.Embed(color=0xED4245, title="⏳ Cooldown Active",
                description=f"You must wait **{remaining}s** before generating again.")
            return await interaction.response.send_message(embeds=[embed], ephemeral=True)

        await interaction.response.defer()

        if db.stock_count(category) <= 0:
            embed = discord.Embed(color=0xED4245, title="❌ Out of Stock",
                description=f"**{CATEGORY_LABELS[category]}** accounts are currently out of stock.")
            return await interaction.followup.send(embeds=[embed])

        # ── Open DM ────────────────────────────────────────────────────────────
        try:
            status_msg = await interaction.user.send("⏳ **Processing Your Account** • Verifying stock...")
        except discord.Forbidden:
            embed = discord.Embed(color=0xFEE75C, title="⚠️ Could Not Send DM",
                description=f"<@{interaction.user.id}> your DMs are closed. Please enable them.")
            return await interaction.followup.send(embeds=[embed])

        # ── LIVE FILTER LOOP ───────────────────────────────────────────────────
        raw = None
        valid_found = False
        tried_count = 0

        try:
            await status_msg.edit(content="🔄 **Verifying Account...**")
            while not valid_found:
                raw = db.pop_stock(category)
                if not raw:
                    break # Ran out of stock while looping
                
                tried_count += 1
                if await is_account_valid(raw):
                    valid_found = True # Found a working one!
                # If invalid, it silently loops and tries the next account
                
            if not valid_found or not raw:
                await status_msg.edit(
                    content=f"❌ **{CATEGORY_LABELS[category]}** is out of stock (cleaned {tried_count} dead accounts)."
                )
                embed = discord.Embed(color=0xED4245, title="❌ Out of Stock",
                    description=f"**{CATEGORY_LABELS[category]}** just sold out.")
                return await interaction.followup.send(embeds=[embed])

            # ── Record cooldown ────────────────────────────────────────────────
            db.update_user(str(interaction.user.id), {f"last_gen_{cat_key}": now, "last_gen": now})
            left = db.stock_count(category)

            # ── Parse account ──────────────────────────────────────────────────
            parsed = parse_account(raw)
            
            # ── Banner ─────────────────────────────────────────────────────────
            gen_image   = db.get_config("gen_image", "")
            banner_file = db.get_banner_file_info() if str(gen_image).startswith("local:") else None
            banner_url = f"attachment://{banner_file['name']}" if banner_file else (gen_image if gen_image.startswith("http") else None)

            color         = _category_color(category)
            guild_icon    = interaction.guild.icon.url if interaction.guild.icon else None
            stock_cap     = max(20, left)
            stock_bar     = utils.progress_bar(left, stock_cap)

            # ── Channel Embed ──────────────────────────────────────────────────
            channel_embed = discord.Embed(color=color, title=f"✨ {CATEGORY_LABELS[category]} Account Claimed ✨",
                description=f"<@{interaction.user.id}> just generated an account! Check your DMs 📬")
            channel_embed.set_author(name=interaction.guild.name, icon_url=guild_icon)
            channel_embed.set_thumbnail(url=interaction.user.display_avatar.url)
            channel_embed.add_field(name="📦 Stock Remaining", value=f"`{stock_bar}` {left} left", inline=False)
            channel_embed.set_footer(text="Generator • Use /generate to claim your own")
            channel_embed.timestamp = discord.utils.utcnow()
            if banner_url: channel_embed.set_image(url=banner_url)

            # ── DM Embed ───────────────────────────────────────────────────────
            dm_description = "\n".join(parsed["detail_lines"]) if parsed["detail_lines"] else "\u200b"
            title = f"🎁✨ Account Delivered — {parsed['username'] or CATEGORY_LABELS[category]} ✨🎁"

            dm_embed = discord.Embed(color=color, title=title, description=dm_description)
            dm_embed.set_author(name=f"{interaction.guild.name} ✨ {CATEGORY_LABELS[category].upper()}", icon_url=guild_icon)
            dm_embed.set_thumbnail(url=guild_icon)
            dm_embed.set_footer(text="Generator • Do NOT share your credentials")
            dm_embed.timestamp = discord.utils.utcnow()

            for field in parsed["currency_fields"][:3]:
                dm_embed.add_field(name=field["name"], value=field["value"], inline=field["inline"])

            dm_embed.add_field(name="🔑 Login Credentials", value=f"```{parsed['credentials']}```", inline=False)
            dm_embed.add_field(name="", value="━━━━━━━━━━━━━━━━━━", inline=False)
            if parsed["skin_link"]:
                dm_embed.add_field(name="🎨 Skin Link", value=parsed["skin_link"], inline=False)
            dm_embed.add_field(name="📦 Tier Stock Remaining", value=f"`{stock_bar}` {left} left", inline=False)
            if banner_url: dm_embed.set_image(url=banner_url)

            # ── Buttons ────────────────────────────────────────────────────────
            buttons = [discord.ui.Button(label="Copy Email:Pass", style=discord.ButtonStyle.primary, custom_id="copy_creds", emoji="📋")]
            if parsed["skin_link"]:
                buttons.append(discord.ui.Button(label="Copy Skin Link", style=discord.ButtonStyle.link, url=parsed["skin_link"], emoji="🎨"))
            buttons.append(discord.ui.Button(label="How to Link", style=discord.ButtonStyle.secondary, custom_id="how_to_link", emoji="❓"))
            buttons.append(discord.ui.Button(label="Upgrade Premium", style=discord.ButtonStyle.link, url=f"https://discord.com/channels/{interaction.guild_id}", emoji="⬆️"))

            view = discord.ui.View(timeout=None)
            for btn in buttons[:5]: view.add_item(btn)

            # ── Send DM cascade ────────────────────────────────────────────────
            await asyncio.sleep(1.0)
            await status_msg.edit(content="✅ **Account Verified!** Details below 👇")

            dm_payload = {"embeds": [dm_embed], "view": view}
            channel_payload = {"embeds": [channel_embed]}

            if banner_file:
                try:
                    dm_file = discord.File(banner_file["path"], filename=banner_file["name"])
                    ch_file = discord.File(banner_file["path"], filename=banner_file["name"])
                    dm_payload["files"] = [dm_file]
                    channel_payload["files"] = [ch_file]
                except Exception:
                    pass

            try:
                await interaction.user.send(**dm_payload)
                db.log_generate(str(interaction.user.id), category, raw)
                await interaction.followup.send(**channel_payload)
            except Exception:
                # Restore stock if DM fails
                db.restore_stock(category, raw)
                db.update_user(str(interaction.user.id), {f"last_gen_{cat_key}": last_gen, "last_gen": user.get("last_gen", 0)})
                await status_msg.edit(content="⚠️ Couldn't deliver your account (DMs closed). It was returned to stock.")
                
        except Exception as e:
            print(f"[generate] Error: {e}")
            if raw:
                db.restore_stock(category, raw)
