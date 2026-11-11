import discord
from discord.ext import commands
from discord import app_commands
from openai import OpenAI
import os
import sys

class AI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key="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"
        )
    
    @app_commands.command(name="ai", description="Ask the new GLM-5.2 AI model")
    @app_commands.describe(prompt="Your question or prompt")
    async def ai(self, interaction: discord.Interaction, prompt: str):
        """AI command using NVIDIA GLM-5.2 model"""
        await interaction.response.defer()
        
        try:
            # Call NVIDIA API with new GLM-5.2 model
            completion = self.client.chat.completions.create(
                model="z-ai/glm-5.2",
                messages=[{"role": "user", "content": prompt}],
                temperature=1,
                top_p=1,
                max_tokens=4096,
                seed=42,
                stream=True
            )
            
            # Collect response with better error handling
            full_response = ""
            
            for chunk in completion:
                # Check if chunk has choices
                if not getattr(chunk, "choices", None):
                    continue
                
                # Check if choices is empty or delta is None
                if len(chunk.choices) == 0 or getattr(chunk.choices[0], "delta", None) is None:
                    continue
                
                delta = chunk.choices[0].delta
                
                # Get content if available
                if getattr(delta, "content", None) is not None:
                    full_response += delta.content
            
            # Send full response (split into 2000 char chunks if needed)
            if not full_response or full_response.strip() == "":
                await interaction.followup.send("❌ No response from AI")
                return
            
            if len(full_response) > 2000:
                # Split into chunks and send each
                await interaction.followup.send("🤖 **GLM-5.2 AI Response:**")
                for i in range(0, len(full_response), 2000):
                    chunk = full_response[i:i+2000]
                    if chunk.strip():
                        await interaction.followup.send(chunk)
            else:
                # Send as single message
                await interaction.followup.send(f"🤖 **GLM-5.2 AI Response:**\n{full_response}")
        
        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            if len(error_msg) > 2000:
                error_msg = error_msg[:2000]
            await interaction.followup.send(error_msg)

async def setup(bot):
    await bot.add_cog(AI(bot))
