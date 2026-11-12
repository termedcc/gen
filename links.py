"""
links.py — Account linking commands.
/linknow  /createkey  /redeemkey  /balance
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import random
import string
import os
import database as db
import utils
import tls_client
import base64
import re

# Helper to manage credits
def get_user_credits(user_id: str) -> int:
    credits = db.get_config("user_credits", {})
    return credits.get(str(user_id), 0)

def add_user_credits(user_id: str, amount: int):
    credits = db.get_config("user_credits", {})
    credits[str(user_id)] = credits.get(str(user_id), 0) + amount
    db.set_config("user_credits", credits)

def remove_user_credits(user_id: str, amount: int) -> bool:
    credits = db.get_config("user_credits", {})
    current = credits.get(str(user_id), 0)
    if current >= amount:
        credits[str(user_id)] = current - amount
        db.set_config("user_credits", credits)
        return True
    return False

class Links(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def load_proxies(self):
        if os.path.exists("proxies.txt"):
            with open("proxies.txt", "r") as f:
                return [line.strip() for line in f if line.strip()]
        return []

    @app_commands.command(name="createkey", description="[Owner] Create a linking key for credits.")
    @app_commands.describe(uses="How many link credits this key gives")
    async def createkey(self, interaction: discord.Interaction, uses: int):
        if not await utils.owner_only(interaction): return
        
        key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
        
        keys = db.get_config("link_keys", {})
        keys[key] = uses
        db.set_config("link_keys", keys)
        
        embed = discord.Embed(title="🔑 Link Key Created", description=f"Key: `{key}`\nGives: **{uses}** link credits", color=0x57F287)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="redeemkey", description="Redeem a link key to add credits to your account.")
    @app_commands.describe(key="The link key you want to redeem")
    async def redeemkey(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        
        keys = db.get_config("link_keys", {})
        key = key.upper()
        
        if key not in keys:
            return await interaction.followup.send("❌ Invalid key.", ephemeral=True)
            
        credits_to_add = keys[key]
        add_user_credits(str(interaction.user.id), credits_to_add)
        
        del keys[key]
        db.set_config("link_keys", keys)
        
        embed = discord.Embed(
            color=0x57F287, 
            title="✅ Key Redeemed", 
            description=f"You have successfully added **{credits_to_add}** link credits to your account!"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="balance", description="Check how many link credits you have.")
    async def balance(self, interaction: discord.Interaction):
        credits = get_user_credits(str(interaction.user.id))
        
        embed = discord.Embed(
            color=0x5865F2,
            title="🎟️ Your Balance",
            description=f"You currently have **{credits}** link credits."
        )
        embed.set_footer(text="Use /redeemkey to add more credits.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # =========================================================================
    # THE NEW /linknow COMMAND WITH BUTTONS
    # =========================================================================
    @app_commands.command(name="linknow", description="Link a console account to your Ubisoft account.")
    async def linknow(self, interaction: discord.Interaction):
        credits = get_user_credits(str(interaction.user.id))
        if credits <= 0:
            return await interaction.response.send_message("❌ You don't have any link credits. Use `/balance` to check and `/redeemkey` to add some.", ephemeral=True)

        embed = discord.Embed(
            color=0x5865F2,
            title="🔗 Choose Your Console",
            description="Select the platform you want to link to your Ubisoft account.\n\n**⚠️ You must be logged into this console account on your phone's browser (Safari/Chrome) before starting.**"
        )
        view = ConsoleSelectView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # Helper function for the actual linking logic
    async def process_link(self, bot, user, console, ubi_email, ubi_pass, console_cred):
        proxies = self.load_proxies()
        proxy = random.choice(proxies) if proxies else None
        session = tls_client.Session(client_identifier="chrome_120")
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}

        # 1. Login to Ubisoft
        b64_auth = base64.b64encode(f"{ubi_email}:{ubi_pass}".encode('utf-8')).decode('utf-8')
        login_url = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": "314d4fef-e568-454a-ae06-43e3bece12a6",
            "Authorization": f"Basic {b64_auth}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        
        login_res = session.post(login_url, headers=headers, json={"rememberMe": True})
        
        if login_res.status_code != 200:
            add_user_credits(str(user.id), 1)
            return await user.send("❌ Failed to log in to Ubisoft. Wrong password or 2FA required. (Credit refunded)")

        ubi_token = login_res.json().get("ticket")
        ubi_session_id = login_res.json().get("sessionId")

        # 2. Console Specific Linking
        if console == "xbox":
            ms_email, ms_pass = console_cred.split(":", 1)
            
            # Microsoft OAuth
            ms_url = "https://login.live.com/oauth20_token.srf"
            ms_data = {
                "client_id": "0000000048093EE3",
                "grant_type": "password",
                "username": ms_email,
                "password": ms_pass,
                "scope": "XboxLive.signin"
            }
            ms_res = session.post(ms_url, data=ms_data)
            
            if ms_res.status_code != 200:
                add_user_credits(str(user.id), 1)
                return await user.send("❌ Failed to log in to Microsoft. Check your email/pass. (Credit refunded)")
                
            ms_token = ms_res.json().get("access_token")

            # Xbox Live Auth
            xbl_url = "https://user.auth.xboxlive.com/user/authenticate"
            xbl_headers = {"x-xbl-contract-version": "1", "Content-Type": "application/json"}
            xbl_data = {
                "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com", "RpsTicket": f"d={ms_token}"},
                "RelyingParty": "http://auth.xboxlive.com",
                "TokenType": "JWT"
            }
            xbl_res = session.post(xbl_url, headers=xbl_headers, json=xbl_data)
            if xbl_res.status_code != 200:
                add_user_credits(str(user.id), 1)
                return await user.send("❌ Microsoft XBL auth failed. (Credit refunded)")
                
            xbl_token = xbl_res.json().get("Token")
            uhs = xbl_res.json().get("DisplayClaims").get("xui")[0].get("uhs")

            # XSTS Auth
            xsts_url = "https://xsts.auth.xboxlive.com/xsts/authorize"
            xsts_data = {
                "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
                "RelyingParty": "http://xboxlive.com",
                "TokenType": "JWT"
            }
            xsts_res = session.post(xsts_url, headers=xbl_headers, json=xsts_data)
            if xsts_res.status_code != 200:
                add_user_credits(str(user.id), 1)
                return await user.send("❌ Xbox XSTS auth failed. (Credit refunded)")

            xsts_token = xsts_res.json().get("Token")

            # Ubisoft Link
            link_url = "https://public-ubiservices.ubi.com/v1/profiles/links"
            link_headers = {
                "Authorization": f"Ubi_v1 t={ubi_token}",
                "Ubi-SessionId": ubi_session_id,
                "Ubi-AppId": "314d4fef-e568-454a-ae06-43e3bece12a6",
                "Content-Type": "application/json"
            }
            link_payload = {
                "platformType": "xbox",
                "platformId": uhs,
                "ticket": xsts_token
            }

            link_res = session.post(link_url, headers=link_headers, json=link_payload)
            
            if link_res.status_code in (200, 201):
                await user.send("✅ **Success!** Your Xbox account has been linked.")
            else:
                add_user_credits(str(user.id), 1)
                await user.send(f"❌ Ubisoft rejected the link. (Status: {link_res.status_code}). (Credit refunded)")

        elif console == "ps":
            # PlayStation NPSSO exchange
            ps_url = "https://ca.account.sony.com/api/authz/v3/oauth/authorize"
            ps_params = {
                "access_type": "offline",
                "client_id": "09515159-7237-4370-9b40-3806e67c0891",
                "redirect_uri": "com.scee.psxandroid.scecompcall://redirect",
                "response_type": "code",
                "scope": "psn:mobile.v2.core psn:clientapp"
            }
            ps_cookies = {"npsso": console_cred}
            
            ps_res = session.get(ps_url, params=ps_params, cookies=ps_cookies, allow_redirects=False)
            
            if "Location" not in ps_res.headers:
                add_user_credits(str(user.id), 1)
                return await user.send("❌ Invalid NPSSO token. (Credit refunded)")
                
            redirect_url = ps_res.headers["Location"]
            auth_code = redirect_url.split("code=")[1].split("&")[0]

            # PSN Token Exchange
            ps_token_url = "https://ca.account.sony.com/api/authz/v3/oauth/token"
            ps_token_data = {
                "code": auth_code,
                "redirect_uri": "com.scee.psxandroid.scecompcall://redirect",
                "grant_type": "authorization_code",
                "token_format": "jwt"
            }
            ps_token_res = session.post(ps_token_url, data=ps_token_data)
            
            if ps_token_res.status_code != 200:
                add_user_credits(str(user.id), 1)
                return await user.send("❌ PSN Token exchange failed. (Credit refunded)")
                
            ps_token = ps_token_res.json().get("access_token")

            # Ubisoft Link
            link_url = "https://public-ubiservices.ubi.com/v1/profiles/links"
            link_headers = {
                "Authorization": f"Ubi_v1 t={ubi_token}",
                "Ubi-SessionId": ubi_session_id,
                "Ubi-AppId": "314d4fef-e568-454a-ae06-43e3bece12a6",
                "Content-Type": "application/json"
            }
            link_payload = {
                "platformType": "psn",
                "ticket": ps_token
            }

            link_res = session.post(link_url, headers=link_headers, json=link_payload)
            
            if link_res.status_code in (200, 201):
                await user.send("✅ **Success!** Your PlayStation account has been linked.")
            else:
                add_user_credits(str(user.id), 1)
                await user.send(f"❌ Ubisoft rejected the PSN link. (Status: {link_res.status_code}). (Credit refunded)")

# =========================================================================
# BUTTON VIEWS AND MODALS
# =========================================================================

class ConsoleSelectView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Xbox", style=discord.ButtonStyle.success, emoji="🎮")
    async def xbox_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Deduct credit
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ You ran out of credits.", ephemeral=True)
        
        await interaction.response.send_message("⏳ Starting Xbox link process... Check your DMs.", ephemeral=True)
        
        try:
            dm = await interaction.user.send("**Step 1:** Do you have access to the email for the Microsoft account?")
            await dm.add_reaction("✅")
            await dm.add_reaction("❌")
            
            def check(reaction, user):
                return user == interaction.user and str(reaction.emoji) in ["✅", "❌"] and reaction.message.id == dm.id
                
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=60.0, check=check)
                if str(reaction.emoji) == "❌":
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ Link cancelled. (Credit refunded)")
            except asyncio.TimeoutError:
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Timed out. (Credit refunded)")

            await interaction.user.send("**Step 2:** Reply with your Ubisoft `email:password`")
            
            def msg_check(m):
                return m.author == interaction.user and m.channel == dm.channel
                
            try:
                ubi_msg = await self.bot.wait_for("message", timeout=60.0, check=msg_check)
                ubi_email, ubi_pass = ubi_msg.content.split(":", 1)
            except Exception:
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Invalid format. (Credit refunded)")

            await interaction.user.send("**Step 3:** Reply with your Microsoft `email:password`")
            
            try:
                ms_msg = await self.bot.wait_for("message", timeout=60.0, check=msg_check)
                ms_cred = ms_msg.content
            except Exception:
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Timed out. (Credit refunded)")

            await interaction.user.send("⏳ Processing your link... This takes 5 seconds.")
            
            cog = self.bot.get_cog("Links")
            await cog.process_link(self.bot, interaction.user, "xbox", ubi_email, ubi_pass, ms_cred)

        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await interaction.user.send(f"❌ Error: {e} (Credit refunded)")

    @discord.ui.button(label="PlayStation", style=discord.ButtonStyle.primary, emoji="🎮")
    async def ps_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ You ran out of credits.", ephemeral=True)
        
        await interaction.response.send_message("⏳ Starting PlayStation link process... Check your DMs.", ephemeral=True)
        
        try:
            dm = await interaction.user.send("**Step 1:** Open Safari and go to this link:\nhttps://ca.account.sony.com/api/authz/v3/oauth/authorize?access_type=offline&client_id=09515159-7237-4370-9b40-3806e67c0891&redirect_uri=com.scee.psxandroid.scecompcall://redirect&response_type=code&scope=psn:mobile.v2.core%20psn:clientapp\n\n**Step 2:** Log in to your PSN account.\n\n**Step 3:** Once logged in, it will redirect you to a blank page. Look at the URL bar. Copy the entire URL and paste it here.")
            
            def msg_check(m):
                return m.author == interaction.user and m.channel == dm.channel
                
            try:
                url_msg = await self.bot.wait_for("message", timeout=120.0, check=msg_check)
                # Extract the code from the redirect URL
                match = re.search(r"code=([^&]+)", url_msg.content)
                if not match:
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ Could not find the code in the URL. (Credit refunded)")
                npsso_code = match.group(1)
            except Exception:
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Timed out. (Credit refunded)")

            await interaction.user.send("**Step 4:** Reply with your Ubisoft `email:password`")
            
            try:
                ubi_msg = await self.bot.wait_for("message", timeout=60.0, check=msg_check)
                ubi_email, ubi_pass = ubi_msg.content.split(":", 1)
            except Exception:
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Invalid format. (Credit refunded)")

            await interaction.user.send("⏳ Processing your link... This takes 5 seconds.")
            
            cog = self.bot.get_cog("Links")
            await cog.process_link(self.bot, interaction.user, "ps", ubi_email, ubi_pass, npsso_code)

        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await interaction.user.send(f"❌ Error: {e} (Credit refunded)")

async def setup(bot):
    await bot.add_cog(Links(bot))