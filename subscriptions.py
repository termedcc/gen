"""
subscriptions.py — /subscribe /unsubscribe /subscription
"""

import time
import discord
from discord import app_commands
from discord.ext import commands

import database as db
import utils
from utils import TIER_RANK  # single source of truth — kept in sync with utils.has_active_sub

# Seconds-per-unit multipliers. "month" and "year" are approximate (30d / 365d)
# since calendar months/years don't have a fixed length — good enough for a
# subscription expiry timer.
UNIT_SECONDS = {
    "seconds": 1,
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
    "months": 2592000,   # 30 days
    "years": 31536000,   # 365 days
}

UNIT_CHOICES = [
    app_commands.Choice(name="Seconds", value="seconds"),
    app_commands.Choice(name="Minutes", value="minutes"),
    app_commands.Choice(name="Hours",   value="hours"),
    app_commands.Choice(name="Days",    value="days"),
    app_commands.Choice(name="Months",  value="months"),
    app_commands.Choice(name="Years",   value="years"),
]


class Subscriptions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="subscribe", description="[Owner] Grant a subscription tier to a user.")
    @app_commands.describe(
        user="Discord user to subscribe",
        tier="Subscription tier",
        amount="How many units the subscription lasts (0 = permanent)",
        unit="Unit of time for the amount above",
    )
    @app_commands.choices(
        tier=[
            app_commands.Choice(name="Free",    value="free"),
            app_commands.Choice(name="Free+",   value="free+"),
            app_commands.Choice(name="Premium", value="premium"),
        ],
        unit=UNIT_CHOICES,
    )
    async def subscribe(self, interaction: discord.Interaction,
                        user: discord.Member, tier: str, amount: int, unit: str):
        if not await utils.owner_only(interaction):
            return

        if amount < 0:
            embed = discord.Embed(
                color=0xED4245, title="❌ Invalid Amount",
                description="`amount` can't be negative. Use `0` for a permanent subscription.",
            )
            return await interaction.response.send_message(embeds=[embed], ephemeral=True)

        duration = amount * UNIT_SECONDS[unit] if amount > 0 else 0

        now = int(time.time())
        expires = now + duration if duration > 0 else 0
        db.update_user(str(user.id), {"subscription": tier, "sub_expires": expires})

        # Grant role if configured
        role_id = utils.get_category_role_id(tier)
        role_given = False
        if role_id:
            role = interaction.guild.get_role(int(role_id))
            if role:
                try:
                    await user.add_roles(role, reason="Subscription granted")
                    role_given = True
                except Exception:
                    pass

        expiry_str = utils.format_expires(expires)
        embed = discord.Embed(
            color=0x57F287,
            title="✅ Subscription Granted",
            description=f"{user.mention} now has **{tier}** access.",
        )
        duration_label = "Permanent" if duration == 0 else f"{amount} {unit}"
        embed.add_field(name="Duration", value=duration_label, inline=True)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        if role_given:
            embed.add_field(name="Role", value=f"<@&{role_id}> granted", inline=True)
        embed.set_footer(text="Generator")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embeds=[embed])

        # DM the user
        try:
            dm = discord.Embed(
                color=0x57F287,
                title=f"🎉 You've been subscribed to {tier}!",
                description=f"Expires: **{expiry_str}**\nUse `/generate` to get your accounts.",
            ).set_footer(text="Generator")
            await user.send(embeds=[dm])
        except Exception:
            pass

    @app_commands.command(name="unsubscribe", description="[Owner] Revoke a user's subscription.")
    @app_commands.describe(user="Discord user to unsubscribe")
    async def unsubscribe(self, interaction: discord.Interaction, user: discord.Member):
        if not await utils.owner_only(interaction):
            return

        u = db.get_user(str(user.id))
        old_tier = u.get("subscription", "none")

        # Remove role if configured
        if old_tier and old_tier != "none":
            role_id = utils.get_category_role_id(old_tier)
            if role_id:
                role = interaction.guild.get_role(int(role_id))
                if role and role in user.roles:
                    try:
                        await user.remove_roles(role, reason="Subscription revoked")
                    except Exception:
                        pass

        db.update_user(str(user.id), {"subscription": "none", "sub_expires": 0})
        embed = discord.Embed(
            color=0xED4245,
            title="✅ Subscription Revoked",
            description=f"{user.mention}'s **{old_tier}** subscription has been removed.",
        ).set_footer(text="Generator")
        await interaction.response.send_message(embeds=[embed])

    @app_commands.command(name="subscription", description="Check your (or another user's) subscription status.")
    @app_commands.describe(user="User to check (defaults to yourself)")
    async def subscription(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        u = db.get_user(str(target.id))

        tier = u.get("subscription", "none")
        expires = u.get("sub_expires", 0)
        expiry_str = utils.format_expires(expires)

        now = int(time.time())
        is_active = tier != "none" and (expires == 0 or expires > now)

        embed = discord.Embed(
            color=0x57F287 if is_active else 0xED4245,
            title=f"{'✅' if is_active else '❌'} Subscription — {target.display_name}",
        )
        embed.add_field(name="Tier",    value=tier.capitalize() if tier else "None", inline=True)
        embed.add_field(name="Status",  value="Active" if is_active else "Inactive", inline=True)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Generator")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embeds=[embed], ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Subscriptions(bot))