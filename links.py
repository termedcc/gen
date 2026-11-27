"""
links.py — Account linking via Ubisoft API (AppID Rotation + Debug + Smart Proxy)
"""

import asyncio
import base64
import io
import os
import random
import re
import string

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import database as db
import utils

# ── Ubisoft Constants ────────────────────────────────────────────────────────
UBI_SESSIONS = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
UBI_LINK_PSN = "https://public-ubiservices.ubi.com/v3/profiles/me/links"
UBI_LINK_XBL = "https://public-ubiservices.ubi.com/v3/profiles/me/links"

# Multiple App IDs to rotate through if one gets blocked
APP_IDS = [
    "86263886-327a-4328-ac69-527f0d20a237", # Ubisoft Connect
    "314d4fef-e568-454a-ae06-43e3bece12a6", # Rainbow Six Siege
    "f68a4a32-28b1-4e20-9f9f-6b9b6d1b8a52", # R6 Tracker
    "0d2ae42d-4c27-4cb7-af6c-2099062302bb"  # R6 Mobile
]

CUSTOM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

PSN_OAUTH_URL = (
    "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
    "?access_type=offline"
    "&client_id=09515159-7237-4370-9b40-3806e67c0891"
    "&redirect_uri=com.scee.psxandroid.scecompcall://redirect"
    "&response_type=code"
    "&scope=psn:mobile.v2.core%20psn:clientapp"
)

# ── Smart Proxy Parser ───────────────────────────────────────────────────────
def load_proxies():
    if not os.path.exists("proxies.txt"):
        return []
        
    proxies = []
    try:
        with open("proxies.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                    
                if line.startswith("http://") or line.startswith("socks5://"):
                    proxies.append(line)
                    continue
                    
                parts = line.split(":")
                if len(parts) == 4:
                    host, port, user, pwd = parts
                    proxies.append(f"http://{user}:{pwd}@{host}:{port}")
                elif "@" in line and ":" in line:
                    host_port, user_pass = line.split("@")
                    host, port = host_port.split(":")
                    user, pwd = user_pass.split(":")
                    proxies.append(f"http://{user}:{pwd}@{host}:{port}")
                else:
                    proxies.append(line)
    except Exception as e:
        print(f"[PROXY] Error loading proxies: {e}")
        
    return proxies

# ── Credit Helpers ───────────────────────────────────────────────────────────
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

# ── DM Helpers ───────────────────────────────────────────────────────────────
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

# ── Ubisoft Auth (Rotation + Debug) ──────────────────────────────────────────
async def _ubi_login(session: aiohttp.ClientSession, email: str, password: str, proxy: str):
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    
    for app_id in APP_IDS:
        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": app_id,
            "Authorization": f"Basic {token}",
            "User-Agent": CUSTOM_UA
        }
        
        print(f"[UBI] Trying AppID: {app_id}...")
        
        try:
            async with session.post(UBI_SESSIONS, headers=headers, json={}, proxy=proxy) as r:
                status = r.status
                data = await r.json(content_type=None)
                
                print(f"[UBI] Status: {status} | Response: {data}")
                
                if status in (403, 400) or "errorCode" in data:
                    continue
                    
                twofa = data.get("twoFactorAuthenticationTicket")
                if twofa:
                    headers_2fa = {**headers, "Authorization": f"ubi_2fa_v1 t={twofa}", "Ubi-2FACode": ""}
                    try:
                        async with session.post(UBI_SESSIONS, headers=headers_2fa, json={}, proxy=proxy) as r2:
                            data2 = await r2.json(content_type=None)
                            print(f"[UBI 2FA] Status: {r2.status} | Response: {data2}")
                            ticket = data2.get("ticket")
                            if ticket:
                                return ticket, data2.get("profileId"), None
                    except Exception:
                        pass
                    return None, None, "2FA_REQUIRED"

                ticket = data.get("ticket")
                pid = data.get("profileId") or data.get("userId")
                if ticket:
                    return ticket, pid, None
                    
                return None, None, f"BAD_CREDS: {data}"
                
        except Exception as e:
            print(f"[UBI] Network error on {app_id}: {e}")
            continue

    return None, None, "ALL_APPIDS_FAILED"

async def _ubi_submit_2fa(session: aiohttp.ClientSession, twofa_ticket: str, code: str, proxy: str):
    for app_id in APP_IDS:
        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": app_id,
            "Authorization": f"ubi_2fa_v1 t={twofa_ticket}",
            "Ubi-2FACode": code,
            "User-Agent": CUSTOM_UA
        }
        try:
            async with session.post(UBI_SESSIONS, headers=headers, json={}, proxy=proxy) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    return data.get("ticket"), data.get("profileId")
        except Exception:
            pass
    return None, None

# ── Xbox XSTS token chain ────────────────────────────────────────────────────
async def _xbox_xsts(session: aiohttp.ClientSession, ms_email: str, ms_pass: str, proxy: str):
    try:
        async with session.post(
            "https://login.live.com/oauth20_token.srf",
            data={"grant_type": "password", "client_id": "000000004C12AE6F", "scope": "service::user.auth.xboxlive.com::MBI_SSL", "username": ms_email, "password": ms_pass},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxy=proxy
        ) as r:
            ms = await r.json(content_type=None)
    except Exception as e:
        return None, f"MS_OAUTH_ERROR:{e}"

    access_token = ms.get("access_token")
    if not access_token:
        return None, f"MS_LOGIN_FAILED:{ms.get('error_description')}"

    try:
        async with session.post(
            "https://user.auth.xboxlive.com/user/authenticate",
            json={"Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": access_token}, "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"},
            headers={"Content-Type": "application/json", "x-xbl-contract-version": "0"},
            proxy=proxy
        ) as r:
            xbl = await r.json(content_type=None)
    except Exception as e:
        return None, f"XBL_AUTH_ERROR:{e}"

    xbl_token = xbl.get("Token")
    userhash = (xbl.get("DisplayClaims", {}).get("xui") or [{}])[0].get("uhs", "")
    if not xbl_token:
        return None, "XBL_TOKEN_MISSING"

    try:
        async with session.post(
            "https://xsts.auth.xboxlive.com/xsts/authorize",
            json={"Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]}, "RelyingParty": "http://xboxlive.com", "TokenType": "JWT"},
            headers={"Content-Type": "application/json", "x-xbl-contract-version": "1"},
            proxy=proxy
        ) as r:
            xsts = await r.json(content_type=None)
    except Exception as e:
        return None, f"XSTS_ERROR:{e}"

    xsts_token = xsts.get("Token")
    if not xsts_token:
        return None, f"XSTS_FAILED:XErr={xsts.get('XErr')}"

    return xsts_token, userhash

# ── Platform linking via Ubisoft API ─────────────────────────────────────────
async def _link_psn(session: aiohttp.ClientSession, ubi_ticket: str, psn_code: str, proxy: str):
    for app_id in APP_IDS:
        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": app_id,
            "Authorization": f"Ubi_v1 t={ubi_ticket}",
            "User-Agent": CUSTOM_UA
        }
        payload = {"platformType": "psn", "ticket": psn_code}
        async with session.post(UBI_LINK_PSN, headers=headers, json=payload, proxy=proxy) as r:
            if r.status != 403:
                return r.status, await r.text()
    return 403, "All AppIDs rejected for linking."

async def _link_xbl(session: aiohttp.ClientSession, ubi_ticket: str, xsts_token: str, userhash: str, proxy: str):
    for app_id in APP_IDS:
        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": app_id,
            "Authorization": f"Ubi_v1 t={ubi_ticket}",
            "User-Agent": CUSTOM_UA
        }
        payload = {"platformType": "xbox", "ticket": f"XBL3.0 x={userhash};{xsts_token}"}
        async with session.post(UBI_LINK_XBL, headers=headers, json=payload, proxy=proxy) as r:
            if r.status != 403:
                return r.status, await r.text()
    return 403, "All AppIDs rejected for linking."

# ── Master process_link ───────────────────────────────────────────────────────
async def process_link(bot, user, console: str, ubi_email: str, ubi_pass: str, console_cred: str):
    dm = await user.create_dm()
    proxies = load_proxies()
    proxy = random.choice(proxies) if proxies else None

    async with aiohttp.ClientSession() as session:
        ticket, pid, result = await _ubi_login(session, ubi_email, ubi_pass, proxy)

        if result == "2FA_REQUIRED":
            await _dm(user, "🔐 **2FA detected.** Reply with your 6-digit code within 2 minutes.")
            code = await _wait(bot, user, dm, timeout=120.0)
            if not code:
                return await _refund(user, "❌ Timed out waiting for 2FA code. (Credit refunded)")
            ticket, pid = await _ubi_submit_2fa(session, result, code, proxy)
            if not ticket:
                return await _refund(user, "❌ 2FA code rejected by Ubisoft. (Credit refunded)")
        elif not ticket:
            return await _refund(user, f"❌ Ubisoft login failed.\nDebug: `{result}`\n(Credit refunded)")

        await _dm(user, f"✅ Ubisoft login OK. Linking console…")

        if console == "ps":
            status, body = await _link_psn(session, ticket, console_cred, proxy)
            if status in (200, 201):
                await _dm(user, "✅ **Success!** PlayStation account linked.")
            else:
                await _refund(user, f"❌ PSN link failed (HTTP {status}).\n```{body[:200]}```\n(Credit refunded)")

        elif console == "xbox":
            ms_email, ms_pass = console_cred.split(":", 1)
            xsts_token, xres = await _xbox_xsts(session, ms_email, ms_pass, proxy)
            if not xsts_token:
                return await _refund(user, f"❌ Microsoft login failed: `{xres}`\n(Credit refunded)")

            status, body = await _link_xbl(session, ticket, xsts_token, xres, proxy)
            if status in (200, 201):
                await _dm(user, "✅ **Success!** Xbox account linked.")
            else:
                await _refund(user, f"❌ Xbox link failed (HTTP {status}).\n```{body[:200]}```\n(Credit refunded)")

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
            description=(
                "Pick the platform to link to your Ubisoft account.\n\n"
                "**NFA accounts link instantly — no browser, pure API.**\n"
                "2FA accounts will be asked for their code."
            ),
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
            dm = await interaction.user.create_dm()
            await dm.send("**Step 1 of 2 — Ubisoft credentials**\n`email:password`")
            raw_ubi = await _wait(self.bot, interaction.user, dm, 60)
            if not raw_ubi or ":" not in raw_ubi:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format. Refunded.")
            ubi_email, ubi_pass = raw_ubi.split(":", 1)

            await dm.send("**Step 2 of 2 — Microsoft credentials**\n`email:password`")
            raw_ms = await _wait(self.bot, interaction.user, dm, 60)
            if not raw_ms or ":" not in raw_ms:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format. Refunded.")

            await dm.send("⏳ Processing via API…")
            asyncio.create_task(process_link(self.bot, interaction.user, "xbox", ubi_email, ubi_pass, raw_ms))
        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await _dm(interaction.user, f"❌ Error: `{e}`. Refunded.")

    @discord.ui.button(label="PlayStation", style=discord.ButtonStyle.primary, emoji="🎮")
    async def ps_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ No credits.", ephemeral=True)
        await interaction.response.send_message("⏳ Check your DMs.", ephemeral=True)
        try:
            dm = await interaction.user.create_dm()
            await dm.send(f"**Step 1:** Go to: `{PSN_OAUTH_URL}`\n**Step 2:** Paste the redirect URL here.")
            url_raw = await _wait(self.bot, interaction.user, dm, 120)
            if not url_raw:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Timed out. Refunded.")
            match = re.search(r"code=([^&\s]+)", url_raw)
            if not match:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ No OAuth code found. Refunded.")
            psn_code = match.group(1)

            await dm.send("**Step 2 of 2 — Ubisoft credentials**\n`email:password`")
            raw_ubi = await _wait(self.bot, interaction.user, dm, 60)
            if not raw_ubi or ":" not in raw_ubi:
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid format. Refunded.")
            ubi_email, ubi_pass = raw_ubi.split(":", 1)

            await dm.send("⏳ Processing via API…")
            asyncio.create_task(process_link(self.bot, interaction.user, "ps", ubi_email, ubi_pass, psn_code))
        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await _dm(interaction.user, f"❌ Error: `{e}`. Refunded.")

async def setup(bot):
    await bot.add_cog(Links(bot))