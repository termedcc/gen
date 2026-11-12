"""
links.py — Account linking commands via pure API.
/linkxbox  /createkey  /redeemkey  /balance  /link
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

    @app_commands.command(name="linkxbox", description="Link an Xbox account using a device code.")
    async def linkxbox(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        CLIENT_ID = "0000000048093EE3" 
        SCOPE = "XboxLive.signin offline_access"
        url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
        data = {"client_id": CLIENT_ID, "scope": SCOPE}
        
        proxies = self.load_proxies()
        proxy = random.choice(proxies) if proxies else None

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data, proxy=proxy) as res:
                    if res.status != 200:
                        return await interaction.followup.send("❌ Failed to contact Microsoft. Proxy might be dead.", ephemeral=True)
                    
                    auth_data = await res.json()
                    user_code = auth_data["user_code"]
                    device_code = auth_data["device_code"]
                    verify_uri = auth_data["verification_uri"]
                    interval = auth_data.get("interval", 5)
                    
                    embed = discord.Embed(
                        color=0x57F287,
                        title="🔗 Link Your Xbox Account",
                        description=(
                            f"**1.** Go to: {verify_uri}\n"
                            f"**2.** Enter this code: `{user_code}`\n"
                            f"**3.** Log in to your Microsoft account and approve the request.\n\n"
                            f"*This message will update automatically once you're done.*"
                        )
                    )
                    msg = await interaction.followup.send(embed=embed, ephemeral=True)

                    token_url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
                    token_data = {
                        "client_id": CLIENT_ID,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code
                    }

                    for _ in range(60):
                        await asyncio.sleep(interval)
                        async with session.post(token_url, data=token_data, proxy=proxy) as token_res:
                            if token_res.status == 200:
                                await msg.edit(content="✅ **Xbox account successfully authorized!**", embed=None)
                                return
                            elif token_res.status == 400:
                                err = await token_res.json()
                                if err.get("error") == "authorization_pending":
                                    continue
                                else:
                                    break
                    
                    await msg.edit(content="❌ Timed out waiting for authorization.", embed=None)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @app_commands.command(name="link", description="Link a console account via API.")
    @app_commands.describe(
        console="Xbox or PlayStation", 
        ubi_email="Ubisoft Email", 
        ubi_pass="Ubisoft Password",
        console_cred="Xbox: Microsoft email:pass | PS: NPSSO token"
    )
    @app_commands.choices(console=[
        app_commands.Choice(name="Xbox", value="xbox"),
        app_commands.Choice(name="PlayStation", value="ps"),
    ])
    async def link(self, interaction: discord.Interaction, console: str, ubi_email: str, ubi_pass: str, console_cred: str):
        credits = get_user_credits(str(interaction.user.id))
        if credits <= 0:
            return await interaction.response.send_message("❌ You don't have any link credits.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.followup.send("❌ Error deducting credit.", ephemeral=True)
        
        await interaction.followup.send(f"⏳ **Starting Link Process...** (1 credit deducted)\nCheck your DMs.", ephemeral=True)
        
        proxies = self.load_proxies()
        proxy = random.choice(proxies) if proxies else None

        # tls_client session to bypass DataDome/Cloudflare
        session = tls_client.Session(client_identifier="chrome_120")
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}

        try:
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
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Failed to log in to Ubisoft. Check credentials. (Credit refunded)")

            ubi_token = login_res.json().get("ticket")
            ubi_session_id = login_res.json().get("sessionId")

            # 2. Link the Console
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
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ Failed to log in to Microsoft. 2FA required or wrong password. (Credit refunded)")
                    
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
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ Microsoft XBL auth failed. (Credit refunded)")
                    
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
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ Xbox XSTS auth failed (Account may be banned). (Credit refunded)")

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
                    await interaction.user.send("✅ **Success!** Your Xbox account has been linked.")
                else:
                    add_user_credits(str(interaction.user.id), 1)
                    await interaction.user.send(f"❌ Ubisoft rejected the link. (Status: {link_res.status_code}). (Credit refunded)")

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
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ Invalid NPSSO token. (Credit refunded)")
                    
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
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ PSN Token exchange failed. (Credit refunded)")
                    
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
                    await interaction.user.send("✅ **Success!** Your PlayStation account has been linked.")
                else:
                    add_user_credits(str(interaction.user.id), 1)
                    await interaction.user.send(f"❌ Ubisoft rejected the PSN link. (Status: {link_res.status_code}). (Credit refunded)")

        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await interaction.user.send(f"❌ Failed to link: `{str(e)}` (Credit refunded)")

async def setup(bot):
    await bot.add_cog(Links(bot))
