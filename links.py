"""
links.py — Account linking commands.
/linknow  /createkey  /bulkcreatekeys  /redeemkey  /balance  /link
"""

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
import random
import string
import os
import re
import database as db
import utils
from playwright.async_api import async_playwright

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

    @app_commands.command(name="bulkcreatekeys", description="[Owner] Generate multiple link keys at once.")
    @app_commands.describe(
        amount="How many keys to generate",
        uses="How many link credits each key should give"
    )
    async def bulkcreatekeys(self, interaction: discord.Interaction, amount: int, uses: int):
        if not await utils.owner_only(interaction): return
        
        if amount <= 0 or amount > 100:
            return await interaction.response.send_message("❌ You can only generate between 1 and 100 keys at a time.", ephemeral=True)
            
        if uses <= 0:
            return await interaction.response.send_message("❌ Uses must be greater than 0.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        
        keys = db.get_config("link_keys", {})
        generated_keys = []

        for _ in range(amount):
            # Generate key with the same format as single createkey
            key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            keys[key] = uses
            generated_keys.append(key)

        db.set_config("link_keys", keys)

        # Create a text file containing all the keys
        key_text = "\n".join(generated_keys)
        file = discord.File(io.BytesIO(key_text.encode()), filename=f"{amount}_keys.txt")

        embed = discord.Embed(
            title="🔑 Bulk Keys Generated",
            description=f"Successfully generated **{amount}** keys.\nEach key gives **{uses}** link credit(s).",
            color=0x57F287
        )
        embed.set_footer(text="You can copy them from the attached file.")
        
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

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

    async def process_link(self, bot, user, console, ubi_email, ubi_pass, console_cred):
        try:
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch(channel="chrome", headless=False)
                except Exception:
                    browser = await p.chromium.launch(headless=False)

                context = await browser.new_context()
                page = await context.new_page()
                
                await page.goto("https://account.ubisoft.com/en-US/login")
                await page.wait_for_selector('input[name="email"]', timeout=15000)
                await page.fill('input[name="email"]', ubi_email)
                await page.fill('input[name="password"]', ubi_pass)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(5000)
                
                if "login" in page.url.lower():
                    await browser.close()
                    add_user_credits(str(user.id), 1)
                    return await user.send("❌ Failed to log in to Ubisoft. Wrong password or 2FA required. (Credit refunded)")

                await page.goto("https://account.ubisoft.com/en-US/account-linking")
                await page.wait_for_timeout(3000)

                if console == "xbox":
                    try:
                        await page.click('button[data-testid="link-xbox"]', timeout=5000)
                    except:
                        await browser.close()
                        add_user_credits(str(user.id), 1)
                        return await user.send("❌ Could not find the Xbox Link button. (Credit refunded)")

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
                        await user.send("🛑 **2FA Required.** Microsoft sent a code. Reply here with the code.")
                        
                        def check(m):
                            return m.author.id == user.id and m.channel.id == user.dm_channel.id
                        
                        try:
                            msg = await bot.wait_for('message', timeout=120.0, check=check)
                            await ms_page.fill('input[type="text"]', msg.content)
                            await ms_page.click('input[type="submit"]')
                            await ms_page.wait_for_timeout(5000)
                        except asyncio.TimeoutError:
                            await browser.close()
                            add_user_credits(str(user.id), 1)
                            return await user.send("❌ Timed out waiting for 2FA code. (Credit refunded)")

                    await page.wait_for_url("**/account-linking**", timeout=15000)
                    await user.send("✅ **Success!** Your Xbox account has been linked.")

                elif console == "ps":
                    try:
                        await page.click('button[data-testid="link-ps"]', timeout=5000)
                    except:
                        await browser.close()
                        add_user_credits(str(user.id), 1)
                        return await user.send("❌ Could not find the PS Link button. (Credit refunded)")

                    async with context.expect_page() as new_page_info:
                        pass
                    ps_page = await new_page_info.value
                    await ps_page.wait_for_load_state()
                    
                    await ps_page.wait_for_selector('input[name="username"]', timeout=15000)
                    await ps_page.fill('input[name="username"]', console_cred) 
                    await ps_page.click('button[type="submit"]')
                    await ps_page.wait_for_timeout(5000)
                    
                    await page.wait_for_url("**/account-linking**", timeout=15000)
                    await user.send("✅ **Success!** Your PlayStation account has been linked.")

                await browser.close()

        except Exception as e:
            add_user_credits(str(user.id), 1)
            await user.send(f"❌ Failed to link: `{str(e)}` (Credit refunded)")

class ConsoleSelectView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Xbox", style=discord.ButtonStyle.success, emoji="🎮")
    async def xbox_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ You ran out of credits.", ephemeral=True)
        
        await interaction.response.send_message("⏳ Starting Xbox link process... Check your DMs.", ephemeral=True)
        
        try:
            dm = await interaction.user.send("**Step 1:** Reply with your Ubisoft `email:password`")
            
            def msg_check(m):
                return m.author == interaction.user and m.channel == dm.channel
                
            try:
                ubi_msg = await self.bot.wait_for("message", timeout=60.0, check=msg_check)
                ubi_email, ubi_pass = ubi_msg.content.split(":", 1)
            except Exception:
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Invalid format. (Credit refunded)")

            await interaction.user.send("**Step 2:** Reply with your Microsoft `email:password`")
            
            try:
                ms_msg = await self.bot.wait_for("message", timeout=60.0, check=msg_check)
                ms_cred = ms_msg.content
            except Exception:
                add_user_credits(str(interaction.user.id), 1)
                return await interaction.user.send("❌ Timed out. (Credit refunded)")

            await interaction.user.send("⏳ Processing your link... A Chrome window will open on the host PC.")
            
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
            dm = await interaction.user.send(
                "**Step 1:** Open Safari and go to this link:\n"
                "https://ca.account.sony.com/api/authz/v3/oauth/authorize?access_type=offline&client_id=09515159-7237-4370-9b40-3806e67c0891&redirect_uri=com.scee.psxandroid.scecompcall://redirect&response_type=code&scope=psn:mobile.v2.core%20psn:clientapp\n\n"
                "**Step 2:** Log in to your PSN account.\n\n"
                "**Step 3:** Once logged in, it will redirect you to a blank page. Look at the URL bar. Copy the entire URL and paste it here."
            )
            
            def msg_check(m):
                return m.author == interaction.user and m.channel == dm.channel
                
            try:
                url_msg = await self.bot.wait_for("message", timeout=120.0, check=msg_check)
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

            await interaction.user.send("⏳ Processing your link... A Chrome window will open on the host PC.")
            
            cog = self.bot.get_cog("Links")
            await cog.process_link(self.bot, interaction.user, "ps", ubi_email, ubi_pass, npsso_code)

        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            await interaction.user.send(f"❌ Error: {e} (Credit refunded)")

async def setup(bot):
    await bot.add_cog(Links(bot))