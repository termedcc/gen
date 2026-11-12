"""
links.py — Account linking commands.
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
import json
import database as db
import utils
from playwright.async_api import async_playwright

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
        
        # Add credits to user
        add_user_credits(str(interaction.user.id), credits_to_add)
        
        # Delete the key so it can't be reused
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
        # Device code flow doesn't use credits, but you can add a check here if you want
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

    @app_commands.command(name="link", description="Link a console account via Playwright.")
    @app_commands.describe(
        console="Xbox or PlayStation", 
        ubi_email="Ubisoft Email", 
        ubi_pass="Ubisoft Password",
        console_cred="Xbox: email:pass | PS: NPSSO token"
    )
    @app_commands.choices(console=[
        app_commands.Choice(name="Xbox", value="xbox"),
        app_commands.Choice(name="PlayStation", value="ps"),
    ])
    async def link(self, interaction: discord.Interaction, console: str, ubi_email: str, ubi_pass: str, console_cred: str):
        # Check balance first
        credits = get_user_credits(str(interaction.user.id))
        if credits <= 0:
            return await interaction.response.send_message("❌ You don't have any link credits. Use `/balance` to check and `/redeemkey` to add some.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        # Deduct credit
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.followup.send("❌ Error deducting credit.", ephemeral=True)
        
        await interaction.followup.send(f"⏳ **Starting Link Process...** (1 credit deducted, you have {credits-1} left)\nCheck your DMs.", ephemeral=True)
        
        proxies = self.load_proxies()
        proxy = random.choice(proxies) if proxies else None

        try:
            async with async_playwright() as p:
                launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                launch_kwargs = {"headless": True, "args": launch_args}
                
                if proxy:
                    if "@" in proxy:
                        auth, server = proxy.split("@")
                        protocol, auth = auth.split("//")
                        user, pwd = auth.split(":")
                        launch_kwargs["proxy"] = {"server": f"{protocol}://{server}", "username": user, "password": pwd}
                    else:
                        launch_kwargs["proxy"] = {"server": proxy}

                browser = await p.chromium.launch(**launch_kwargs)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                page = await context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                
                # 1. Login to Ubisoft
                await page.goto("https://account.ubisoft.com/en-US/login", timeout=30000)
                await page.wait_for_selector('input[name="email"]', timeout=15000)
                await page.fill('input[name="email"]', ubi_email)
                await page.fill('input[name="password"]', ubi_pass)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(5000)
                
                if "login" in page.url.lower():
                    await browser.close()
                    # Refund credit on failure
                    add_user_credits(str(interaction.user.id), 1)
                    return await interaction.user.send("❌ Failed to log in. Wrong password or 2FA required. (Credit refunded)")

                # 2. Go to Linking Page
                await page.goto("https://account.ubisoft.com/en-US/account-linking", timeout=15000)
                await page.wait_for_timeout(3000)

                # 3. Click the Link Button based on Console
                if console == "xbox":
                    try:
                        await page.click('button[data-testid="link-xbox"]', timeout=5000)
                    except:
                        await browser.close()
                        add_user_credits(str(interaction.user.id), 1)
                        return await interaction.user.send("❌ Could not find the Xbox Link button. (Credit refunded)")

                    async with context.expect_page() as new_page_info:
                        pass
                    ms_page = await new_page_info.value
                    await ms_page.wait_for_load_state()

                    ms_email, ms_pass = console_cred.split(":", 1)
                    await ms_page.fill('input[type="email"]', ms_email)
                    await ms_page.click('input[type="submit"]')
                    await ms_page.wait_for_timeout(2000)
                    await ms_page.fill('input[type="password"]', ms_pass)
                    await ms_page.click('input[type="submit"]')
                    await ms_page.wait_for_timeout(5000)
                    
                    if "identity/conditionalaccess" in ms_page.url or "proofs" in ms_page.url:
                        await interaction.user.send("🛑 **2FA Required.** Microsoft sent a code. Reply here with the code.")
                        
                        def check(m):
                            return m.author.id == interaction.user.id and m.channel.id == interaction.user.dm_channel.id
                        
                        try:
                            msg = await self.bot.wait_for('message', timeout=120.0, check=check)
                            await ms_page.fill('input[type="text"]', msg.content)
                            await ms_page.click('input[type="submit"]')
                            await ms_page.wait_for_timeout(5000)
                        except asyncio.TimeoutError:
                            await browser.close()
                            add_user_credits(str(interaction.user.id), 1)
                            return await interaction.user.send("❌ Timed out waiting for 2FA code. (Credit refunded)")

                    await page.wait_for_url("**/account-linking**", timeout=15000)
                    await interaction.user.send("✅ **Success!** Your Xbox account has been linked.")

                elif console == "ps":
                    try:
                        await page.click('button[data-testid="link-ps"]', timeout=5000)
                    except:
                        await browser.close()
                        add_user_credits(str(interaction.user.id), 1)
                        return await interaction.user.send("❌ Could not find the PS Link button. (Credit refunded)")

                    async with context.expect_page() as new_page_info:
                        pass
                    ps_page = await new_page_info.value
                    await ps_page.wait_for_load_state()
                    
                    await ps_page.wait_for_selector('input[name="username"]', timeout=15000)
                    await ps_page.fill('input[name="username"]', console_cred)
                    await ps_page.click('button[type="submit"]')
                    await ps_page.wait_for_timeout(5000)
                    
                    await page.wait_for_url("**/account-linking**", timeout=15000)
                    await interaction.user.send("✅ **Success!** Your PlayStation account has been linked.")

                await browser.close()

        except Exception as e:
            # Refund on exception
            add_user_credits(str(interaction.user.id), 1)
            await interaction.user.send(f"❌ Failed to link: `{str(e)}` (Credit refunded)")

async def setup(bot):
    await bot.add_cog(Links(bot))
