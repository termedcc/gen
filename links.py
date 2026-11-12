"""
links.py — Account linking commands.
/linkxbox  /createkey  /link
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
from playwright.async_api import async_playwright

class Links(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def load_proxies(self):
        if os.path.exists("proxies.txt"):
            with open("proxies.txt", "r") as f:
                return [line.strip() for line in f if line.strip()]
        return []

    @app_commands.command(name="createkey", description="[Owner] Create a linking key.")
    @app_commands.describe(uses="How many times this key can be used")
    async def createkey(self, interaction: discord.Interaction, uses: int):
        if not await utils.owner_only(interaction): return
        
        key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
        
        keys = db.get_config("link_keys", {})
        keys[key] = uses
        db.set_config("link_keys", keys)
        
        embed = discord.Embed(title="🔑 Link Key Created", description=f"Key: `{key}`\nUses: **{uses}**", color=0x57F287)
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
                        return await interaction.followup.send("❌ Failed to contact Microsoft.", ephemeral=True)
                    
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

                    for _ in range(60): # Wait up to 5 minutes
                        await asyncio.sleep(interval)
                        async with session.post(token_url, data=token_data, proxy=proxy) as token_res:
                            if token_res.status == 200:
                                await msg.edit(content="✅ **Xbox account successfully authorized!**\n*(Note: You must link this on the Ubisoft website manually for now).*", embed=None)
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

    @app_commands.command(name="link", description="Link a console account via Playwright.")
    @app_commands.describe(
        key="Your link key", 
        console="Xbox or PlayStation", 
        ubi_email="Ubisoft Email", 
        ubi_pass="Ubisoft Password",
        console_cred="For Xbox: your Microsoft email:pass. For PS: your NPSSO cookie."
    )
    async def link(self, interaction: discord.Interaction, key: str, console: str, ubi_email: str, ubi_pass: str, console_cred: str):
        await interaction.response.defer(ephemeral=True)
        
        keys = db.get_config("link_keys", {})
        if key not in keys or keys[key] <= 0:
            return await interaction.followup.send("❌ Invalid or expired link key.", ephemeral=True)
            
        keys[key] -= 1
        if keys[key] <= 0:
            del keys[key]
        db.set_config("link_keys", keys)
        
        await interaction.followup.send("⏳ **Starting Link Process...**\nYou will receive a DM shortly if 2FA is required.", ephemeral=True)
        
        proxies = self.load_proxies()
        proxy = random.choice(proxies) if proxies else None

        try:
            async with async_playwright() as p:
                # Launch with stealth arguments and proxy
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox"
                ]
                
                launch_kwargs = {"headless": True, "args": launch_args}
                if proxy:
                    # Playwright requires proxy split into server, username, password
                    # Assuming format http://user:pass@ip:port
                    if "@" in proxy:
                        auth, server = proxy.split("@")
                        protocol, auth = auth.split("//")
                        user, pwd = auth.split(":")
                        launch_kwargs["proxy"] = {
                            "server": f"{protocol}://{server}",
                            "username": user,
                            "password": pwd
                        }
                    else:
                        launch_kwargs["proxy"] = {"server": proxy}

                browser = await p.chromium.launch(**launch_kwargs)
                
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080}
                )
                page = await context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                
                # Login to Ubisoft
                await page.goto("https://account.ubisoft.com/en-US/login", timeout=30000)
                await page.wait_for_selector('input[name="email"]', timeout=10000)
                await page.fill('input[name="email"]', ubi_email)
                await page.fill('input[name="password"]', ubi_pass)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(5000)
                
                if "login" in page.url.lower():
                    await browser.close()
                    return await interaction.user.send("❌ Failed to log in. Wrong password or 2FA required on Ubisoft account.")

                # Go to linking page
                await page.goto("https://account.ubisoft.com/en-US/account-linking", timeout=15000)
                await page.wait_for_timeout(3000)
                
                await browser.close()
                await interaction.user.send("✅ Ubisoft login successful. Manual linking may be required due to Microsoft/Sony iframes.")
                
        except Exception as e:
            await interaction.user.send(f"❌ Failed to link: `{str(e)}`")

async def setup(bot):
    await bot.add_cog(Links(bot))
