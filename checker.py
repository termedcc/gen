import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import base64
import utils

UBI_APP_ID = "39b0e8b8-33df-41da-a9e1-b459f22dbcf2" # If this is dead, we will replace it

class Checker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="check", description="Check if an email:pass account is valid.")
    @app_commands.describe(account="Format: email:password")
    async def check(self, interaction: discord.Interaction, account: str):
        # Allow owner to test
        if not utils.is_owner(str(interaction.user.id)):
            return await interaction.response.send_message("❌ Owner only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if ":" not in account:
            return await interaction.followup.send("❌ Invalid format. Use `email:password`.", ephemeral=True)

        email, password = account.split(":", 1)
        b64_auth = base64.b64encode(f"{email}:{password}".encode('utf-8')).decode('utf-8')

        headers = {
            "Content-Type": "application/json",
            "Ubi-AppId": UBI_APP_ID,
            "Authorization": f"Basic {b64_auth}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://public-ubiservices.ubi.com/v3/profiles/sessions", headers=headers, json={}, timeout=5) as res:
                    if res.status == 200:
                        data = await res.json()
                        embed = discord.Embed(color=0x57F287, title="✅ Valid Account", description=f"**Email:** `{email}`\n**Profile ID:** `{data.get('profileId')}`")
                        await interaction.followup.send(embed=embed, ephemeral=True)
                    elif res.status == 401:
                        await interaction.followup.send("❌ **Invalid Credentials** (Wrong password or 2FA).", ephemeral=True)
                    else:
                        await interaction.followup.send(f"❌ **Error**: HTTP {res.status}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Network error: {str(e)}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Checker(bot))
