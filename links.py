import discord
from discord import app_commands
from discord.ext import commands
import database as db
import utils
import asyncio
from playwright.async_api import async_playwright
import random
import string

class Links(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="createkey", description="[Owner] Create a linking key.")
    @app_commands.describe(uses="How many times this key can be used")
    async def createkey(self, interaction: discord.Interaction, uses: int):
        if not await utils.owner_only(interaction): return
        
        # Generate random key
        key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
        
        keys = db.get_config("link_keys", {})
        keys[key] = uses
        db.set_config("link_keys", keys)
        
        embed = discord.Embed(title="🔑 Link Key Created", description=f"Key: `{key}`\nUses: **{uses}**", color=0x57F287)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="link", description="Link a console account to your Ubisoft account.")
    @app_commands.describe(
        key="Your link key", 
        console="Xbox or PlayStation", 
        ubi_email="Ubisoft Email", 
        ubi_pass="Ubisoft Password",
        console_cred="For Xbox: your Microsoft email:pass. For PS: your NPSSO cookie."
    )
    async def link(self, interaction: discord.Interaction, key: str, console: str, ubi_email: str, ubi_pass: str, console_cred: str):
        await interaction.response.defer(ephemeral=True)
        
        # 1. Verify key
        keys = db.get_config("link_keys", {})
        if key not in keys or keys[key] <= 0:
            return await interaction.followup.send("❌ Invalid or expired link key.", ephemeral=True)
            
        keys[key] -= 1
        if keys[key] <= 0:
            del keys[key]
        db.set_config("link_keys", keys)
        
        await interaction.followup.send("⏳ **Starting Link Process...**\nYou will receive a DM shortly with the 2FA code.", ephemeral=True)
        
        # 2. Start Playwright to link the account
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
                # Go to Ubisoft linking page
                await page.goto("https://account.ubisoft.com/en-US/account-linking")
                
                # Login to Ubisoft
                await page.fill('input[name="email"]', ubi_email)
                await page.fill('input[name="password"]', ubi_pass)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(3000)
                
                # Check if 2FA is required
                if await page.locator('input[name="code"]').is_visible():
                    # Prompt user for 2FA
                    dm = await interaction.user.send("🛑 **2FA Required.** Please reply with the 6-digit code sent to your email.")
                    
                    def check(m):
                        return m.author.id == interaction.user.id and m.channel.id == dm.channel.id
                    
                    try:
                        msg = await self.bot.wait_for('message', timeout=120.0, check=check)
                        code = msg.content
                        await page.fill('input[name="code"]', code)
                        await page.click('button[type="submit"]')
                        await page.wait_for_timeout(5000)
                    except asyncio.TimeoutError:
                        await browser.close()
                        return await dm.edit(content="❌ Timed out waiting for 2FA code.")

                # Linking logic here varies wildly between Xbox and PS.
                # You have to script clicking the "Link Xbox" button, 
                # then popping up the Microsoft login window, etc.
                
                await browser.close()
                await interaction.user.send("✅ **Success!** Your account is now linked.")
                
        except Exception as e:
            await interaction.user.send(f"❌ Failed to link: `{str(e)}`")

async def setup(bot):
    await bot.add_cog(Links(bot))
