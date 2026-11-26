"""
links.py — Account linking via Ubisoft API (pure HTTP, no browser).

NFA UBISOFT FLOW
────────────────
POST https://public-ubiservices.ubi.com/v3/profiles/sessions
  Authorization: Basic base64(email:password)
  Ubi-AppId:     86263886-327a-4328-ac69-527f0d20a237
  User-Agent:    (custom — Ubisoft blocks default aiohttp/requests agents)
  Content-Type:  application/json

NFA accounts → ticket returned immediately, no 2FA field in response.
2FA accounts → twoFactorAuthenticationTicket returned, we ask for code.

PSN LINK
────────
User opens Sony OAuth URL in browser while logged into PSN,
pastes the redirect URL back, we extract the code and POST it to
Ubisoft's linking endpoint.

XBOX LINK
─────────
User supplies Microsoft email:password.
We exchange those through Microsoft's auth chain
(OAuth2 password grant → XBL → XSTS) and POST the XSTS token
to Ubisoft's linking endpoint.
"""

import asyncio
import base64
import io
import random
import re
import string

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import database as db
import utils

# ── Ubisoft constants ────────────────────────────────────────────────────────
UBI_APP_ID   = "86263886-327a-4328-ac69-527f0d20a237"
UBI_SESSIONS = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
UBI_LINK_PSN = "https://public-ubiservices.ubi.com/v3/profiles/me/linked-providers/psn"
UBI_LINK_XBL = "https://public-ubiservices.ubi.com/v3/profiles/me/linked-providers/xbl"

# Custom UA — Ubisoft silently rejects default aiohttp/requests agents
CUSTOM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

UBI_BASE_HEADERS = {
    "Content-Type" : "application/json",
    "Ubi-AppId"    : UBI_APP_ID,
    "User-Agent"   : CUSTOM_UA,
    "Connection"   : "keep-alive",
}

# Sony OAuth URL user opens in their browser
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


# ── Ubisoft auth ─────────────────────────────────────────────────────────────

async def _ubi_login(session: aiohttp.ClientSession, email: str, password: str):
    """
    Attempt Ubisoft Basic auth login.
    Returns:
      (ticket, profile_id, None)       — NFA success
      (None,   None,       twofa_tkn)  — 2FA required
      (None,   None,       None)        — bad credentials / blocked
    """
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    headers = {**UBI_BASE_HEADERS, "Authorization": f"Basic {token}"}

    try:
        async with session.post(UBI_SESSIONS, headers=headers, json={}) as r:
            data = await r.json(content_type=None)
    except Exception as e:
        return None, None, f"REQUEST_ERROR:{e}"

    twofa  = data.get("twoFactorAuthenticationTicket")
    ticket = data.get("ticket")
    pid    = data.get("profileId") or data.get("userId")

    if twofa:
        return None, None, twofa
    if ticket:
        return ticket, pid, None
    return None, None, None


async def _ubi_submit_2fa(session: aiohttp.ClientSession, twofa_ticket: str, code: str):
    """Submit 2FA code. Returns (ticket, pid) or (None, None)."""
    headers = {
        **UBI_BASE_HEADERS,
        "Authorization" : f"ubi_2fa_v1 t={twofa_ticket}",
        "Ubi-2FACode"   : code,
    }
    try:
        async with session.post(UBI_SESSIONS, headers=headers, json={}) as r:
            data = await r.json(content_type=None)
    except Exception:
        return None, None

    ticket = data.get("ticket")
    pid    = data.get("profileId") or data.get("userId")
    return (ticket, pid) if ticket else (None, None)


# ── Xbox XSTS token chain ────────────────────────────────────────────────────

async def _xbox_xsts(session: aiohttp.ClientSession, ms_email: str, ms_pass: str):
    """
    Microsoft auth chain → XSTS token + userhash.
    Returns (xsts_token, userhash) or (None, error_string).
    """
    # Step 1 — MS OAuth2 password grant
    try:
        async with session.post(
            "https://login.live.com/oauth20_token.srf",
            data={
                "grant_type" : "password",
                "client_id"  : "000000004C12AE6F",
                "scope"      : "service::user.auth.xboxlive.com::MBI_SSL",
                "username"   : ms_email,
                "password"   : ms_pass,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            ms = await r.json(content_type=None)
    except Exception as e:
        return None, f"MS_OAUTH_ERROR:{e}"

    access_token = ms.get("access_token")
    if not access_token:
        err = ms.get("error_description", ms.get("error", "unknown"))
        return None, f"MS_LOGIN_FAILED:{err}"

    # Step 2 — Xbox Live RPS
    try:
        async with session.post(
            "https://user.auth.xboxlive.com/user/authenticate",
            json={
                "Properties": {
                    "AuthMethod" : "RPS",
                    "SiteName"   : "user.auth.xboxlive.com",
                    "RpsTicket"  : access_token,
                },
                "RelyingParty" : "http://auth.xboxlive.com",
                "TokenType"    : "JWT",
            },
            headers={
                "Content-Type"           : "application/json",
                "x-xbl-contract-version" : "0",
            },
        ) as r:
            xbl = await r.json(content_type=None)
    except Exception as e:
        return None, f"XBL_AUTH_ERROR:{e}"

    xbl_token = xbl.get("Token")
    userhash  = (xbl.get("DisplayClaims", {}).get("xui") or [{}])[0].get("uhs", "")
    if not xbl_token:
        return None, "XBL_TOKEN_MISSING"

    # Step 3 — XSTS
    try:
        async with session.post(
            "https://xsts.auth.xboxlive.com/xsts/authorize",
            json={
                "Properties": {
                    "SandboxId"  : "RETAIL",
                    "UserTokens" : [xbl_token],
                },
                "RelyingParty" : "http://xboxlive.com",
                "TokenType"    : "JWT",
            },
            headers={
                "Content-Type"           : "application/json",
                "x-xbl-contract-version" : "1",
            },
        ) as r:
            xsts = await r.json(content_type=None)
    except Exception as e:
        return None, f"XSTS_ERROR:{e}"

    xsts_token = xsts.get("Token")
    if not xsts_token:
        xerr = xsts.get("XErr", "unknown")
        return None, f"XSTS_FAILED:XErr={xerr}"

    return xsts_token, userhash


# ── Platform linking via Ubisoft API ─────────────────────────────────────────

async def _link_psn(session: aiohttp.ClientSession, ubi_ticket: str, psn_code: str):
    headers = {
        **UBI_BASE_HEADERS,
        "Authorization" : f"Ubi_v1 t={ubi_ticket}",
    }
    payload = {
        "authorizationCode" : psn_code,
        "redirectUri"       : "com.scee.psxandroid.scecompcall://redirect",
    }
    async with session.post(UBI_LINK_PSN, headers=headers, json=payload) as r:
        return r.status, await r.text()


async def _link_xbl(session: aiohttp.ClientSession, ubi_ticket: str,
                    xsts_token: str, userhash: str):
    headers = {
        **UBI_BASE_HEADERS,
        "Authorization" : f"Ubi_v1 t={ubi_ticket}",
    }
    payload = {
        "xstsToken" : f"XBL3.0 x={userhash};{xsts_token}",
    }
    async with session.post(UBI_LINK_XBL, headers=headers, json=payload) as r:
        return r.status, await r.text()


# ── Master process_link ───────────────────────────────────────────────────────

async def process_link(bot, user, console: str,
                       ubi_email: str, ubi_pass: str, console_cred: str):
    dm = await user.create_dm()

    async with aiohttp.ClientSession() as session:

        # ── Ubisoft login ─────────────────────────────────────────────────
        ticket, pid, twofa_or_err = await _ubi_login(session, ubi_email, ubi_pass)

        if twofa_or_err and not ticket:
            if isinstance(twofa_or_err, str) and twofa_or_err.startswith("REQUEST_ERROR"):
                return await _refund(user, f"❌ Network error reaching Ubisoft.\n`{twofa_or_err}` (Credit refunded)")

            # 2FA required
            twofa_ticket = twofa_or_err
            await _dm(user,
                "🔐 **2FA detected on this Ubisoft account.**\n"
                "Reply with your 6-digit code within 2 minutes."
            )
            code = await _wait(bot, user, dm, timeout=120.0)
            if not code:
                return await _refund(user, "❌ Timed out waiting for 2FA code. (Credit refunded)")

            ticket, pid = await _ubi_submit_2fa(session, twofa_ticket, code)
            if not ticket:
                return await _refund(user, "❌ 2FA code was rejected by Ubisoft. (Credit refunded)")

        elif not ticket:
            return await _refund(user,
                "❌ Ubisoft login failed.\n"
                "Make sure the email and password are correct.\n"
                "(Credit refunded)"
            )

        await _dm(user, f"✅ Ubisoft login OK (profile `{pid}`). Linking console…")

        # ── Console linking ───────────────────────────────────────────────
        if console == "ps":
            status, body = await _link_psn(session, ticket, console_cred)
            if status in (200, 201):
                await _dm(user, "✅ **Success!** PlayStation account linked to Ubisoft.")
            else:
                await _refund(user,
                    f"❌ PSN link failed (HTTP {status}).\n"
                    f"```{body[:300]}```\n"
                    "Possible causes: PSN already linked to another Ubi account, or OAuth code expired.\n"
                    "(Credit refunded)"
                )

        elif console == "xbox":
            ms_email, ms_pass = console_cred.split(":", 1)
            await _dm(user, "⏳ Authenticating with Microsoft…")

            xsts_token, result = await _xbox_xsts(session, ms_email, ms_pass)
            if not xsts_token:
                return await _refund(user,
                    f"❌ Microsoft login failed: `{result}`\n"
                    "Check the Xbox email and password.\n(Credit refunded)"
                )

            status, body = await _link_xbl(session, ticket, xsts_token, result)
            if status in (200, 201):
                await _dm(user, "✅ **Success!** Xbox account linked to Ubisoft.")
            else:
                await _refund(user,
                    f"❌ Xbox link failed (HTTP {status}).\n"
                    f"```{body[:300]}```\n"
                    "Possible causes: Xbox already linked to another Ubi account.\n"
                    "(Credit refunded)"
                )


# ── Cog ───────────────────────────────────────────────────────────────────────

class Links(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="createkey", description="[Owner] Create a linking key.")
    @app_commands.describe(uses="Link credits this key gives")
    async def createkey(self, interaction: discord.Interaction, uses: int):
        if not await utils.owner_only(interaction):
            return
        key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
        keys = db.get_config("link_keys", {})
        keys[key] = uses
        db.set_config("link_keys", keys)
        embed = discord.Embed(
            title="🔑 Key Created",
            description=f"`{key}` — **{uses}** credit(s)",
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bulkcreatekeys", description="[Owner] Generate multiple keys.")
    @app_commands.describe(amount="Number of keys (1–100)", uses="Credits per key")
    async def bulkcreatekeys(self, interaction: discord.Interaction, amount: int, uses: int):
        if not await utils.owner_only(interaction):
            return
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
        embed = discord.Embed(
            title="🔑 Bulk Keys Generated",
            description=f"**{amount}** keys · **{uses}** credit(s) each.",
            color=0x57F287,
        )
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
        embed = discord.Embed(
            title="✅ Redeemed",
            description=f"Added **{amount}** link credit(s).",
            color=0x57F287,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="balance", description="Check your link credit balance.")
    async def balance(self, interaction: discord.Interaction):
        c = get_user_credits(str(interaction.user.id))
        embed = discord.Embed(
            title="🎟️ Balance",
            description=f"**{c}** link credit(s).",
            color=0x5865F2,
        )
        embed.set_footer(text="Use /redeemkey to add more.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="linknow", description="Link a console account to Ubisoft.")
    async def linknow(self, interaction: discord.Interaction):
        if get_user_credits(str(interaction.user.id)) <= 0:
            return await interaction.response.send_message(
                "❌ No link credits. Use `/redeemkey` first.", ephemeral=True
            )
        embed = discord.Embed(
            title="🔗 Choose Console",
            description=(
                "Pick the platform to link to your Ubisoft account.\n\n"
                "**NFA accounts link instantly — no browser, pure API.**\n"
                "2FA accounts will be asked for their code."
            ),
            color=0x5865F2,
        )
        await interaction.response.send_message(
            embed=embed, view=ConsoleSelectView(self.bot), ephemeral=True
        )


# ── UI ────────────────────────────────────────────────────────────────────────

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
            dm = await interaction.user.create_dm()

            await dm.send(
                "**Step 1 of 2 — Ubisoft credentials**\n"
                "Send your Ubisoft login in this format:\n"
                "`email:password`\n\n"
                "*(NFA = no 2FA needed. If 2FA is on, you'll be asked for the code.)*"
            )
            raw_ubi = await _wait(self.bot, interaction.user, dm, 60)
            if not raw_ubi or ":" not in raw_ubi:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format or timed out. (Credit refunded)")
            ubi_email, ubi_pass = raw_ubi.split(":", 1)

            await dm.send(
                "**Step 2 of 2 — Microsoft credentials**\n"
                "Send your Xbox / Microsoft login:\n"
                "`email:password`"
            )
            raw_ms = await _wait(self.bot, interaction.user, dm, 60)
            if not raw_ms or ":" not in raw_ms:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format or timed out. (Credit refunded)")

            await dm.send("⏳ Processing via Ubisoft API…")
            asyncio.create_task(
                process_link(self.bot, interaction.user, "xbox", ubi_email, ubi_pass, raw_ms)
            )
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
                "**Step 1 of 2 — Get your PSN code**\n"
                "Open **Safari or Chrome** while logged in to PSN and visit:\n"
                f"`{PSN_OAUTH_URL}`\n\n"
                "You'll land on a blank/redirect page.\n"
                "Copy the **entire URL** from the address bar and paste it here."
            )
            url_raw = await _wait(self.bot, interaction.user, dm, 180)
            if not url_raw:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Timed out. (Credit refunded)")
            match = re.search(r"code=([^&\s]+)", url_raw)
            if not match:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send(
                    "❌ Could not find `code=` in that URL.\n"
                    "Make sure you copied the full redirect URL. (Credit refunded)"
                )
            psn_code = match.group(1)

            await dm.send(
                "**Step 2 of 2 — Ubisoft credentials**\n"
                "Send your Ubisoft login:\n"
                "`email:password`\n\n"
                "*(NFA = no 2FA needed.)*"
            )
            raw_ubi = await _wait(self.bot, interaction.user, dm, 60)
            if not raw_ubi or ":" not in raw_ubi:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format or timed out. (Credit refunded)")
            ubi_email, ubi_pass = raw_ubi.split(":", 1)

            await dm.send("⏳ Processing via Ubisoft API…")
            asyncio.create_task(
                process_link(self.bot, interaction.user, "ps", ubi_email, ubi_pass, psn_code)
            )
        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await _dm(interaction.user, f"❌ Error: `{e}` (Credit refunded)")


async def setup(bot):
    await bot.add_cog(Links(bot))
