"""
links.py — Account linking via Ubisoft API (Silent 2FA handling).
"""

import base64
import io
import random
import re
import string
import asyncio

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import database as db
import utils

# ── Ubisoft constants ────────────────────────────────────────────────────────
UBI_APP_ID   = "86263886-327a-4328-ac69-527f0d20a237"
UBI_SESSIONS = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
UBI_LINK_PSN = "https://public-ubiservices.ubi.com/v3/profiles/me/links"
UBI_LINK_XBL = "https://public-ubiservices.ubi.com/v3/profiles/me/links"

UBI_HEADERS_BASE = {
    "Content-Type" : "application/json",
    "Ubi-AppId"    : UBI_APP_ID,
    "User-Agent"   : "NexoraLinker/1.0",
}

PSN_OAUTH_URL = (
    "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
    "?access_type=offline"
    "&client_id=09515159-7237-4370-9b40-3806e67c0891"
    "&redirect_uri=com.scee.psxandroid.scecompcall://redirect"
    "&response_type=code"
    "&scope=psn:mobile.v2.core%20psn:clientapp"
)

# ── Credit helpers ───────────────────────────────────────────────────────────
def get_user_credits(user_id: str) -> int:
    return db.get_config("user_credits", {}).get(str(user_id), 0)

def add_user_credits(user_id: str, amount: int):
    c = db.get_config("user_credits", {})
    c[str(user_id)] = c.get(str(user_id), 0) + amount
    db.set_config("user_credits", c)

def remove_user_credits(user_id: str, amount: int) -> bool:
    c = db.get_config("user_credits", {})
    cur = c.get(str(user_id), 0)
    if cur >= amount:
        c[str(user_id)] = cur - amount
        db.set_config("user_credits", c)
        return True
    return False

# ── DM helpers ───────────────────────────────────────────────────────────────
async def _dm(user, text: str):
    try:
        await user.send(text)
    except Exception:
        pass

async def _refund(user, text: str):
    add_user_credits(str(user.id), 1)
    await _dm(user, text)

def _check(user, dm):
    def inner(m):
        return m.author.id == user.id and m.channel.id == dm.id
    return inner

async def _wait(bot, user, dm, timeout=120.0):
    try:
        msg = await bot.wait_for("message", timeout=timeout, check=_check(user, dm))
        return msg.content.strip()
    except asyncio.TimeoutError:
        return None

# ── Ubisoft auth (Silent 2FA) ────────────────────────────────────────────────
async def _ubi_session(session: aiohttp.ClientSession, email: str, password: str):
    creds = base64.b64encode(f"{email}:{password}".encode()).decode()
    headers = {**UBI_HEADERS_BASE, "Authorization": f"Basic {creds}"}

    async with session.post(UBI_SESSIONS, headers=headers, json={}) as r:
        data = await r.json()

    twofa = data.get("twoFactorAuthenticationTicket")
    if twofa:
        # SILENT 2FA: Send the ticket back immediately without a code
        headers_2fa = {**UBI_HEADERS_BASE, "Authorization": f"ubi_2fa_v1 t={twofa}"}
        async with session.post(UBI_SESSIONS, headers=headers_2fa, json={}) as r2:
            data2 = await r2.json()
            
        ticket = data2.get("ticket")
        pid = data2.get("profileId") or data2.get("userId")
        if ticket:
            return ticket, pid, None
        else:
            # If it fails, print exactly what Ubisoft said
            return None, None, f"2FA_REJECTED: {data2}"

    ticket = data.get("ticket")
    pid    = data.get("profileId") or data.get("userId")
    if ticket:
        return ticket, pid, None
    return None, None, "BAD_CREDS"

# ── Xbox XSTS token chain ────────────────────────────────────────────────────
async def _xbox_xsts(session: aiohttp.ClientSession, ms_email: str, ms_pass: str):
    async with session.post(
        "https://login.live.com/oauth20_token.srf",
        data={
            "grant_type"    : "password",
            "client_id"     : "000000004C12AE6F",
            "scope"         : "service::user.auth.xboxlive.com::MBI_SSL",
            "username"      : ms_email,
            "password"      : ms_pass,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as r:
        ms = await r.json()

    access_token = ms.get("access_token")
    if not access_token:
        return None, None

    async with session.post(
        "https://user.auth.xboxlive.com/user/authenticate",
        json={
            "Properties": {
                "AuthMethod" : "RPS",
                "SiteName"   : "user.auth.xboxlive.com",
                "RpsTicket"  : access_token,
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType"   : "JWT",
        },
        headers={"Content-Type": "application/json", "x-xbl-contract-version": "0"},
    ) as r:
        xbl = await r.json()

    xbl_token = xbl.get("Token")
    userhash  = (xbl.get("DisplayClaims", {}).get("xui") or [{}])[0].get("uhs", "")
    if not xbl_token:
        return None, None

    async with session.post(
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        json={
            "Properties": {
                "SandboxId"  : "RETAIL",
                "UserTokens" : [xbl_token],
            },
            "RelyingParty": "http://xboxlive.com",
            "TokenType"   : "JWT",
        },
        headers={"Content-Type": "application/json", "x-xbl-contract-version": "1"},
    ) as r:
        xsts = await r.json()

    xsts_token = xsts.get("Token")
    if not xsts_token:
        return None, None

    return xsts_token, userhash

# ── Platform linking via Ubisoft API ─────────────────────────────────────────
async def _link_psn_api(session: aiohttp.ClientSession, ubi_ticket: str, psn_code: str):
    headers = {**UBI_HEADERS_BASE, "Authorization": f"Ubi_v1 t={ubi_ticket}"}
    payload = {"platformType": "psn", "ticket": psn_code}
    async with session.post(UBI_LINK_PSN, headers=headers, json=payload) as r:
        return r.status, await r.text()

async def _link_xbl_api(session: aiohttp.ClientSession, ubi_ticket: str, xsts_token: str, userhash: str):
    headers = {**UBI_HEADERS_BASE, "Authorization": f"Ubi_v1 t={ubi_ticket}"}
    payload = {"platformType": "xbox", "ticket": f"XBL3.0 x={userhash};{xsts_token}"}
    async with session.post(UBI_LINK_XBL, headers=headers, json=payload) as r:
        return r.status, await r.text()

# ── Master link flow ──────────────────────────────────────────────────────────
async def process_link(bot, user, console: str, ubi_email: str, ubi_pass: str, console_cred: str):
    async with aiohttp.ClientSession() as session:
        ticket, pid, err = await _ubi_session(session, ubi_email, ubi_pass)

        if err and "2FA_REJECTED" in err:
            return await _refund(user, f"❌ Ubisoft requires a 2FA code for this account. Silent bypass failed.\nDebug: `{err[:150]}`")
        elif err == "BAD_CREDS" or not ticket:
            return await _refund(user, "❌ Ubisoft login failed. Check your email and password. (Credit refunded)")

        await _dm(user, "✅ Ubisoft login successful. Linking your console account…")

        if console == "ps":
            status, body = await _link_psn_api(session, ticket, console_cred)
            if status in (200, 201):
                await _dm(user, "✅ **Success!** Your PlayStation account is now linked to Ubisoft.")
            else:
                await _refund(user, f"❌ PSN link failed (HTTP {status}).\nUbisoft said: `{body[:200]}`\n(Credit refunded)")

        elif console == "xbox":
            ms_email, ms_pass = console_cred.split(":", 1)
            await _dm(user, "⏳ Authenticating with Microsoft…")

            xsts_token, userhash = await _xbox_xsts(session, ms_email, ms_pass)
            if not xsts_token:
                return await _refund(user, "❌ Microsoft login failed. (Credit refunded)")

            status, body = await _link_xbl_api(session, ticket, xsts_token, userhash)
            if status in (200, 201):
                await _dm(user, "✅ **Success!** Your Xbox account is now linked to Ubisoft.")
            else:
                await _refund(user, f"❌ Xbox link failed (HTTP {status}).\nUbisoft said: `{body[:200]}`\n(Credit refunded)")

# ── Cog ───────────────────────────────────────────────────────────────────────
class Links(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="createkey", description="[Owner] Create a linking key.")
    @app_commands.describe(uses="Link credits this key gives")
    async def createkey(self, interaction: discord.Interaction, uses: int):
        if not await utils.owner_only(interaction): return
        key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
        keys = db.get_config("link_keys", {})
        keys[key] = uses
        db.set_config("link_keys", keys)
        embed = discord.Embed(title="🔑 Key Created", description=f"`{key}` — **{uses}** credit(s)", color=0x57F287)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bulkcreatekeys", description="[Owner] Generate multiple keys.")
    @app_commands.describe(amount="Number of keys (1–100)", uses="Credits per key")
    async def bulkcreatekeys(self, interaction: discord.Interaction, amount: int, uses: int):
        if not await utils.owner_only(interaction): return
        if not (1 <= amount <= 100):
            return await interaction.response.send_message("❌ Amount must be 1–100.", ephemeral=True)
        if uses <= 0:
            return await interaction.response.send_message("❌ Uses must be > 0.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        keys = db.get_config("link_keys", {})
        generated = []
        for _ in range(amount):
            key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            keys[key] = uses
            generated.append(key)
        db.set_config("link_keys", keys)
        file = discord.File(io.BytesIO("\n".join(generated).encode()), filename=f"{amount}_keys.txt")
        embed = discord.Embed(title="🔑 Bulk Keys Generated", description=f"**{amount}** keys · **{uses}** credit(s) each.", color=0x57F287)
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    @app_commands.command(name="redeemkey", description="Redeem a link key for credits.")
    @app_commands.describe(key="The key to redeem")
    async def redeemkey(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        keys = db.get_config("link_keys", {})
        key  = key.upper().strip()
        if key not in keys:
            return await interaction.followup.send("❌ Invalid or already-used key.", ephemeral=True)
        amount = keys.pop(key)
        db.set_config("link_keys", keys)
        add_user_credits(str(interaction.user.id), amount)
        embed = discord.Embed(title="✅ Redeemed", description=f"Added **{amount}** link credit(s).", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="balance", description="Check your link credit balance.")
    async def balance(self, interaction: discord.Interaction):
        c = get_user_credits(str(interaction.user.id))
        embed = discord.Embed(title="🎟 Balance", description=f"**{c}** link credit(s).", color=0x5865F2)
        embed.set_footer(text="Use /redeemkey to add more.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="linknow", description="Link a console account to Ubisoft.")
    async def linknow(self, interaction: discord.Interaction):
        if get_user_credits(str(interaction.user.id)) <= 0:
            return await interaction.response.send_message("❌ No link credits. Use `/redeemkey` first.", ephemeral=True)
        embed = discord.Embed(
            title="🔗 Choose Console",
            description="Pick the platform to link to your Ubisoft account.",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, view=ConsoleSelectView(self.bot), ephemeral=True)

class ConsoleSelectView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Xbox", style=discord.ButtonStyle.success, emoji="🎮")
    async def xbox_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ No credits.", ephemeral=True)
        await interaction.response.send_message("⏳ Check your DMs.", ephemeral=True)
        try:
            dm    = await interaction.user.create_dm()
            await dm.send("**Step 1:** Send your Ubisoft credentials:\n`email:password`")
            raw = await _wait(self.bot, interaction.user, dm, 60)
            if not raw or ":" not in raw:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format or timed out. (Credit refunded)")
            ubi_email, ubi_pass = raw.split(":", 1)

            await dm.send("**Step 2:** Send your Microsoft credentials:\n`email:password`")
            ms_raw = await _wait(self.bot, interaction.user, dm, 60)
            if not ms_raw or ":" not in ms_raw:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format or timed out. (Credit refunded)")

            await dm.send("⏳ Processing via API…")
            asyncio.create_task(process_link(self.bot, interaction.user, "xbox", ubi_email, ubi_pass, ms_raw))
        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await _dm(interaction.user, f"❌ Error: `{e}` (Credit refunded)")

    @discord.ui.button(label="PlayStation", style=discord.ButtonStyle.primary, emoji="🎮")
    async def ps_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ No credits.", ephemeral=True)
        await interaction.response.send_message("⏳ Check your DMs.", ephemeral=True)
        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                "**Step 1:** Open Safari/Chrome while **logged in to PSN** and go to:\n"
                f"`{PSN_OAUTH_URL}`\n\n"
                "**Step 2:** You'll hit a blank page. Copy the **full URL** from the address bar and paste it here."
            )
            url_raw = await _wait(self.bot, interaction.user, dm, 120)
            if not url_raw:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Timed out. (Credit refunded)")
            match = re.search(r"code=([^&\s]+)", url_raw)
            if not match:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Could not find the OAuth code in that URL. (Credit refunded)")
            psn_code = match.group(1)

            await dm.send("**Step 3:** Send your Ubisoft credentials:\n`email:password`")
            ubi_raw = await _wait(self.bot, interaction.user, dm, 60)
            if not ubi_raw or ":" not in ubi_raw:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format or timed out. (Credit refunded)")
            ubi_email, ubi_pass = ubi_raw.split(":", 1)

            await dm.send("⏳ Processing via API…")
            asyncio.create_task(process_link(self.bot, interaction.user, "ps", ubi_email, ubi_pass, psn_code))
        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await _dm(interaction.user, f"❌ Error: `{e}` (Credit refunded)")

async def setup(bot):
    await bot.add_cog(Links(bot))