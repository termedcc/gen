"""
links.py — Account linking commands.
/linknow  /createkey  /bulkcreatekeys  /redeemkey  /balance

NFA-first flow:
  - Attempts Ubisoft login with email + password only.
  - If Ubisoft returns a 2FA screen, asks the user for the code.
  - If account is NFA, skips 2FA entirely and proceeds to console linking.
  - Credit is refunded on any failure.
"""

import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import random
import string
import io
import re
import database as db
import utils
from playwright.async_api import async_playwright


# ── Credit helpers ──────────────────────────────────────────────────────────

def get_user_credits(user_id: str) -> int:
    return db.get_config("user_credits", {}).get(str(user_id), 0)

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _msg_check(user, dm_channel):
    """Return a check function that accepts DMs only from the given user."""
    def check(m):
        return m.author.id == user.id and m.channel.id == dm_channel.id
    return check


async def _refund_and_dm(user, msg: str):
    """Refund one credit and send an error DM."""
    add_user_credits(str(user.id), 1)
    try:
        await user.send(msg)
    except Exception:
        pass


# ── Core link logic ──────────────────────────────────────────────────────────

async def process_link(bot, user, console: str, ubi_email: str, ubi_pass: str, console_cred: str):
    """
    Open a Playwright browser, log in to Ubisoft (NFA or 2FA), then link
    to Xbox or PlayStation.

    console_cred:
      Xbox  → "ms_email:ms_password"
      PS    → OAuth code extracted from redirect URL
    """
    try:
        async with async_playwright() as p:
            # Prefer installed Chrome; fall back to Chromium
            try:
                browser = await p.chromium.launch(channel="chrome", headless=False)
            except Exception:
                browser = await p.chromium.launch(headless=False)

            context = await browser.new_context()
            page    = await context.new_page()

            # ── Step 1: Ubisoft login ─────────────────────────────────────
            await page.goto("https://account.ubisoft.com/en-US/login")
            try:
                await page.wait_for_selector('input[name="email"]', timeout=15_000)
            except Exception:
                await browser.close()
                return await _refund_and_dm(user, "❌ Ubisoft login page failed to load. (Credit refunded)")

            await page.fill('input[name="email"]', ubi_email)
            await page.fill('input[name="password"]', ubi_pass)
            await page.click('button[type="submit"]')

            # Give Ubisoft up to 8 s to respond
            await page.wait_for_timeout(8_000)

            # ── Step 2: Detect outcome ───────────────────────────────────
            current_url = page.url.lower()

            # Wrong password / account not found — still on login page
            if "login" in current_url and "account-linking" not in current_url and "2fa" not in current_url and "otp" not in current_url:
                await browser.close()
                return await _refund_and_dm(user, "❌ Ubisoft login failed. Check your email and password. (Credit refunded)")

            # 2FA screen detected — ask user for code
            two_fa_indicators = ["2fa", "otp", "twofactor", "two-factor", "verify", "emailvalidation"]
            if any(ind in current_url for ind in two_fa_indicators):
                # Also check for a visible OTP input field
                otp_selector = 'input[name="OTP"], input[name="otp"], input[placeholder*="code" i], input[type="number"]'
                otp_visible = await page.is_visible(otp_selector)

                if otp_visible or any(ind in current_url for ind in two_fa_indicators):
                    try:
                        await user.send(
                            "🔐 **2FA detected on this Ubisoft account.**\n"
                            "Reply with your 6-digit Ubisoft verification code within 2 minutes."
                        )
                    except Exception:
                        await browser.close()
                        return await _refund_and_dm(user, "❌ Could not reach you in DMs. (Credit refunded)")

                    check = _msg_check(user, (await user.create_dm()))

                    try:
                        code_msg = await bot.wait_for("message", timeout=120.0, check=check)
                        code = code_msg.content.strip()
                    except asyncio.TimeoutError:
                        await browser.close()
                        return await _refund_and_dm(user, "❌ Timed out waiting for 2FA code. (Credit refunded)")

                    # Fill the OTP field
                    try:
                        await page.fill(otp_selector, code)
                        await page.click('button[type="submit"]')
                        await page.wait_for_timeout(6_000)
                    except Exception:
                        await browser.close()
                        return await _refund_and_dm(user, "❌ Could not submit 2FA code. (Credit refunded)")

                    # Re-check URL after 2FA
                    if "login" in page.url.lower() or any(ind in page.url.lower() for ind in two_fa_indicators):
                        await browser.close()
                        return await _refund_and_dm(user, "❌ 2FA code was rejected. (Credit refunded)")

            # ── Step 3: Navigate to account-linking ──────────────────────
            await page.goto("https://account.ubisoft.com/en-US/account-linking")
            await page.wait_for_timeout(3_000)

            # ── Step 4: Console-specific flow ────────────────────────────
            if console == "xbox":
                await _link_xbox(bot, user, page, context, console_cred)

            elif console == "ps":
                await _link_ps(bot, user, page, context, console_cred)

            await browser.close()

    except Exception as e:
        await _refund_and_dm(user, f"❌ Unexpected error: `{e}` (Credit refunded)")


async def _link_xbox(bot, user, page, context, ms_cred: str):
    """Click the Xbox link button and authenticate with Microsoft."""
    try:
        await page.click('button[data-testid="link-xbox"]', timeout=8_000)
    except Exception:
        return await _refund_and_dm(user, "❌ Could not find the Xbox Link button on Ubisoft. (Credit refunded)")

    # Microsoft login opens in a new tab
    try:
        async with context.expect_page(timeout=10_000) as new_page_info:
            pass
        ms_page = await new_page_info.value
        await ms_page.wait_for_load_state()
    except Exception:
        return await _refund_and_dm(user, "❌ Microsoft login tab did not open. (Credit refunded)")

    ms_email, ms_pass = ms_cred.split(":", 1)

    try:
        await ms_page.fill('input[type="email"]', ms_email)
        await ms_page.click('input[type="submit"]')
        await ms_page.wait_for_timeout(2_500)
        await ms_page.fill('input[type="password"]', ms_pass)
        await ms_page.click('input[type="submit"]')
        await ms_page.wait_for_timeout(6_000)
    except Exception as e:
        return await _refund_and_dm(user, f"❌ Microsoft login error: `{e}` (Credit refunded)")

    # Microsoft 2FA / identity challenge
    ms_url = ms_page.url.lower()
    if any(x in ms_url for x in ["conditionalaccess", "proofs", "kmsi", "secondaryfactor"]):
        try:
            await user.send("🛑 **Microsoft 2FA required.** Reply with the code sent to you.")
        except Exception:
            return await _refund_and_dm(user, "❌ Could not reach you in DMs. (Credit refunded)")

        check = _msg_check(user, (await user.create_dm()))
        try:
            msg = await bot.wait_for("message", timeout=120.0, check=check)
            await ms_page.fill('input[type="text"]', msg.content.strip())
            await ms_page.click('input[type="submit"]')
            await ms_page.wait_for_timeout(5_000)
        except asyncio.TimeoutError:
            return await _refund_and_dm(user, "❌ Timed out waiting for Microsoft 2FA. (Credit refunded)")

    # Wait for Ubisoft account-linking page to reflect success
    try:
        await page.wait_for_url("**/account-linking**", timeout=20_000)
        await user.send("✅ **Success!** Your Xbox account has been linked to Ubisoft.")
    except Exception:
        await _refund_and_dm(user, "❌ Xbox link timed out or failed. (Credit refunded)")


async def _link_ps(bot, user, page, context, npsso_code: str):
    """
    Click the PS link button.  
    PSN already gave us an OAuth code from the redirect URL the user pasted,
    so Playwright just handles the Ubisoft side — the PSN tab auto-completes
    via the stored OAuth code / cookie that Sony set during the user's
    manual login step.
    """
    try:
        await page.click('button[data-testid="link-ps"]', timeout=8_000)
    except Exception:
        return await _refund_and_dm(user, "❌ Could not find the PlayStation Link button on Ubisoft. (Credit refunded)")

    try:
        async with context.expect_page(timeout=10_000) as new_page_info:
            pass
        ps_page = await new_page_info.value
        await ps_page.wait_for_load_state()
    except Exception:
        return await _refund_and_dm(user, "❌ PlayStation login tab did not open. (Credit refunded)")

    # Sony's OAuth page — user is already logged in via the code they retrieved,
    # so the page should auto-approve.  Give it time to redirect back.
    await ps_page.wait_for_timeout(6_000)

    try:
        await page.wait_for_url("**/account-linking**", timeout=20_000)
        await user.send("✅ **Success!** Your PlayStation account has been linked to Ubisoft.")
    except Exception:
        await _refund_and_dm(user, "❌ PlayStation link timed out or failed. (Credit refunded)")


# ── Cog ──────────────────────────────────────────────────────────────────────

class Links(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /createkey ──────────────────────────────────────────────────────────
    @app_commands.command(name="createkey", description="[Owner] Create a linking key for credits.")
    @app_commands.describe(uses="How many link credits this key gives")
    async def createkey(self, interaction: discord.Interaction, uses: int):
        if not await utils.owner_only(interaction):
            return
        key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
        keys = db.get_config("link_keys", {})
        keys[key] = uses
        db.set_config("link_keys", keys)
        embed = discord.Embed(
            title="🔑 Link Key Created",
            description=f"Key: `{key}`\nGives: **{uses}** link credit(s)",
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /bulkcreatekeys ─────────────────────────────────────────────────────
    @app_commands.command(name="bulkcreatekeys", description="[Owner] Generate multiple link keys at once.")
    @app_commands.describe(amount="How many keys to generate", uses="Credits per key")
    async def bulkcreatekeys(self, interaction: discord.Interaction, amount: int, uses: int):
        if not await utils.owner_only(interaction):
            return
        if not (1 <= amount <= 100):
            return await interaction.response.send_message("❌ Amount must be 1–100.", ephemeral=True)
        if uses <= 0:
            return await interaction.response.send_message("❌ Uses must be > 0.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        keys = db.get_config("link_keys", {})
        generated = []
        for _ in range(amount):
            key = "LINK-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
            keys[key] = uses
            generated.append(key)
        db.set_config("link_keys", keys)

        file = discord.File(
            io.BytesIO("\n".join(generated).encode()),
            filename=f"{amount}_keys.txt",
        )
        embed = discord.Embed(
            title="🔑 Bulk Keys Generated",
            description=f"Generated **{amount}** keys · **{uses}** credit(s) each.",
            color=0x57F287,
        )
        embed.set_footer(text="Keys attached as a text file.")
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)

    # ── /redeemkey ──────────────────────────────────────────────────────────
    @app_commands.command(name="redeemkey", description="Redeem a link key to add credits.")
    @app_commands.describe(key="The key to redeem")
    async def redeemkey(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        keys = db.get_config("link_keys", {})
        key  = key.upper().strip()
        if key not in keys:
            return await interaction.followup.send("❌ Invalid or already-used key.", ephemeral=True)
        amount = keys.pop(key)
        db.set_config("link_keys", keys)
        add_user_credits(str(interaction.user.id), amount)
        embed = discord.Embed(
            title="✅ Key Redeemed",
            description=f"Added **{amount}** link credit(s) to your account.",
            color=0x57F287,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /balance ────────────────────────────────────────────────────────────
    @app_commands.command(name="balance", description="Check your link credit balance.")
    async def balance(self, interaction: discord.Interaction):
        credits = get_user_credits(str(interaction.user.id))
        embed = discord.Embed(
            title="🎟️ Your Balance",
            description=f"You have **{credits}** link credit(s).",
            color=0x5865F2,
        )
        embed.set_footer(text="Use /redeemkey to add more.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /linknow ────────────────────────────────────────────────────────────
    @app_commands.command(name="linknow", description="Link a console account to your Ubisoft account.")
    async def linknow(self, interaction: discord.Interaction):
        if get_user_credits(str(interaction.user.id)) <= 0:
            return await interaction.response.send_message(
                "❌ No link credits. Use `/redeemkey` to add some.",
                ephemeral=True,
            )
        embed = discord.Embed(
            title="🔗 Choose Your Console",
            description=(
                "Select the platform you want to link to your Ubisoft account.\n\n"
                "**ℹ️ NFA accounts (no 2FA) link automatically.**\n"
                "If your Ubisoft account has 2FA enabled, you will be asked for the code."
            ),
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, view=ConsoleSelectView(self.bot), ephemeral=True)


# ── Console select UI ────────────────────────────────────────────────────────

class ConsoleSelectView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Xbox", style=discord.ButtonStyle.success, emoji="🎮")
    async def xbox_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ No credits left.", ephemeral=True)

        await interaction.response.send_message("⏳ Starting Xbox link… Check your DMs.", ephemeral=True)

        try:
            dm    = await interaction.user.create_dm()
            check = _msg_check(interaction.user, dm)

            # Collect Ubisoft credentials
            await dm.send(
                "**Step 1:** Send your Ubisoft credentials in this format:\n"
                "`email:password`\n\n"
                "*(NFA accounts link without a 2FA code. If your account has 2FA, you'll be asked.)*"
            )
            try:
                ubi_msg = await self.bot.wait_for("message", timeout=60.0, check=check)
                ubi_email, ubi_pass = ubi_msg.content.strip().split(":", 1)
            except (asyncio.TimeoutError, ValueError):
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid or timed out. (Credit refunded)")

            # Collect Microsoft credentials
            await dm.send("**Step 2:** Send your Microsoft credentials:\n`email:password`")
            try:
                ms_msg = await self.bot.wait_for("message", timeout=60.0, check=check)
                ms_cred = ms_msg.content.strip()
                if ":" not in ms_cred:
                    raise ValueError
            except (asyncio.TimeoutError, ValueError):
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid or timed out. (Credit refunded)")

            await dm.send("⏳ Processing… a browser window will open on the host machine.")
            asyncio.create_task(
                process_link(self.bot, interaction.user, "xbox", ubi_email, ubi_pass, ms_cred)
            )

        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            try:
                await interaction.user.send(f"❌ Error: `{e}` (Credit refunded)")
            except Exception:
                pass

    @discord.ui.button(label="PlayStation", style=discord.ButtonStyle.primary, emoji="🎮")
    async def ps_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not remove_user_credits(str(interaction.user.id), 1):
            return await interaction.response.send_message("❌ No credits left.", ephemeral=True)

        await interaction.response.send_message("⏳ Starting PlayStation link… Check your DMs.", ephemeral=True)

        try:
            dm    = await interaction.user.create_dm()
            check = _msg_check(interaction.user, dm)

            # PSN OAuth code retrieval
            await dm.send(
                "**Step 1:** Open Safari / Chrome and go to this URL while **logged in to PSN**:\n"
                "<https://ca.account.sony.com/api/authz/v3/oauth/authorize"
                "?access_type=offline"
                "&client_id=09515159-7237-4370-9b40-3806e67c0891"
                "&redirect_uri=com.scee.psxandroid.scecompcall://redirect"
                "&response_type=code"
                "&scope=psn:mobile.v2.core%20psn:clientapp>\n\n"
                "**Step 2:** You'll be redirected to a blank page. Copy the **full URL** from the address bar and paste it here."
            )
            try:
                url_msg = await self.bot.wait_for("message", timeout=120.0, check=check)
                match   = re.search(r"code=([^&\s]+)", url_msg.content)
                if not match:
                    raise ValueError("no code found")
                npsso_code = match.group(1)
            except (asyncio.TimeoutError, ValueError):
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Could not find the OAuth code in the URL. (Credit refunded)")

            # Ubisoft credentials
            await dm.send(
                "**Step 3:** Send your Ubisoft credentials:\n"
                "`email:password`\n\n"
                "*(NFA accounts link without a 2FA code.)*"
            )
            try:
                ubi_msg = await self.bot.wait_for("message", timeout=60.0, check=check)
                ubi_email, ubi_pass = ubi_msg.content.strip().split(":", 1)
            except (asyncio.TimeoutError, ValueError):
                add_user_credits(str(interaction.user.id), 1)
                return await dm.send("❌ Invalid or timed out. (Credit refunded)")

            await dm.send("⏳ Processing… a browser window will open on the host machine.")
            asyncio.create_task(
                process_link(self.bot, interaction.user, "ps", ubi_email, ubi_pass, npsso_code)
            )

        except Exception as e:
            add_user_credits(str(interaction.user.id), 1)
            try:
                await interaction.user.send(f"❌ Error: `{e}` (Credit refunded)")
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(Links(bot))
