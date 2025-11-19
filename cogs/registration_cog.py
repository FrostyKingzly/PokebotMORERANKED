"""Registration Cog - Handles /register command and new trainer setup"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from ui.embeds import EmbedBuilder
from ui.buttons import (RegistrationView, StarterSelectView, 
                       SocialStatsView, ConfirmationView)
from models import Pokemon


class RegistrationModal(discord.ui.Modal, title="Trainer Registration"):
    """Modal for collecting trainer name"""
    
    trainer_name = discord.ui.TextInput(
        label="Trainer Name",
        placeholder="Enter your trainer name...",
        required=True,
        max_length=20
    )
    
    avatar_url = discord.ui.TextInput(
        label="Avatar URL (Optional)",
        placeholder="Paste an image URL or leave blank...",
        required=False,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        # Store data temporarily on the client
        interaction.client.temp_registration_data = {
            'user_id': interaction.user.id,
            'trainer_name': self.trainer_name.value,
            'avatar_url': self.avatar_url.value if self.avatar_url.value else None
        }

        # Acknowledge the modal to avoid 'Unknown interaction' errors
        await interaction.response.defer(ephemeral=True)

        # Small confirmation message
        await interaction.followup.send(
            "✅ Name saved! Now let's choose your starter Pokémon...",
            ephemeral=True
        )

        # Move to starter selection
        await self.start_starter_selection(interaction)

    async def start_starter_selection(self, interaction: discord.Interaction):
        """Start the starter Pokemon selection"""
        # Create starter selection embed
        embed = discord.Embed(
            title="🎯 Choose Your Starter!",
            description=(
                "Choose literally any Pokemon from the full regional Dex!\n\n"
                "Every starter begins at Level 5, no matter which species you pick."
            ),
            color=discord.Color.green()
        )

        # Create a future for the selection flow (used by StarterSelectView/DexNumberModal)
        loop = asyncio.get_event_loop()
        selection_future = loop.create_future()

        # Create starter view
        view = StarterSelectView(interaction.client.species_db, selection_future)

        # Send follow-up
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        # Wait for either a selection or timeout
        await selection_future

        # Check the selected species on the view
        if view.selected_species:
            starter_data = view.selected_species
            interaction.client.temp_registration_data['starter_species'] = starter_data

            # Move to social stats selection
            await self.start_social_stats_selection(interaction)
        else:
            await interaction.followup.send(
                "❌ You didn't pick a starter in time. Use `/register` to try again.",
                ephemeral=True
            )

    async def start_social_stats_selection(self, interaction: discord.Interaction):
        """Start social stats selection"""
        if 'starter_species' not in interaction.client.temp_registration_data:
            await interaction.followup.send(
                "❌ Starter selection missing. Please run `/register` again.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="✨ Choose Your Social Stats",
            description=(
                "Every trainer has 5 social stats:\n\n"
                "**Heart** - Empathy & compassion\n"
                "**Insight** - Perception & intellect\n"
                "**Charisma** - Confidence & Influence\n"
                "**Fortitude** - Physical grit & stamina\n"
                "**Will** - Determination & inner strength\n\n"
                "Choose one **Boon** (Rank 2) and one **Bane** (Rank 0).\n"
                "The other three will start at Rank 1."
            ),
            color=discord.Color.blue()
        )
        
        view = SocialStatsView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        
        # Wait for selections
        await view.wait()
        
        if view.boon_stat and view.bane_stat:
            # Store social stat choices
            interaction.client.temp_registration_data['boon_stat'] = view.boon_stat
            interaction.client.temp_registration_data['bane_stat'] = view.bane_stat
            
            # Show summary and confirm
            await self.show_registration_summary(interaction)
    
    async def show_registration_summary(self, interaction: discord.Interaction):
        """Show final summary and confirmation"""
        data = interaction.client.temp_registration_data
        starter_species = data.get('starter_species')

        if not starter_species:
            await interaction.followup.send(
                "❌ Starter selection missing. Please run `/register` again.",
                ephemeral=True
            )
            return

        embed = EmbedBuilder.registration_summary(
            trainer_name=data['trainer_name'],
            starter_species=starter_species['name'],
            boon_stat=data['boon_stat'],
            bane_stat=data['bane_stat'],
            avatar_url=data.get('avatar_url')
        )
        
        view = ConfirmationView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        
        # Wait for confirmation
        await view.wait()
        
        if view.value:
            # Create the trainer!
            await self.complete_registration(interaction)
        else:
            await interaction.followup.send(
                "❌ Registration cancelled. Use `/register` to start over.",
                ephemeral=True
            )
    
    async def complete_registration(self, interaction: discord.Interaction):
        """Complete registration and create trainer + starter"""
        data = interaction.client.temp_registration_data
        
        # Create trainer profile
        success = interaction.client.player_manager.create_player(
            discord_user_id=data['user_id'],
            trainer_name=data['trainer_name'],
            avatar_url=data.get('avatar_url'),
            boon_stat=data['boon_stat'],
            bane_stat=data['bane_stat']
        )
        
        if not success:
            embed = EmbedBuilder.error(
                "Registration Failed",
                "You already have a trainer profile! Use `/menu` to continue your journey."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        
        # Create starter Pokemon
        starter_species = data['starter_species']
        starter = Pokemon(
            species_data=starter_species,
            level=5,
            owner_discord_id=data['user_id']
        )
        
        # Add to party
        interaction.client.player_manager.add_pokemon_to_party(starter)
        
        # Add to Pokedex
        interaction.client.player_manager.add_pokedex_seen(
            data['user_id'],
            starter_species['dex_number']
        )
        
        # Success message
        embed = discord.Embed(
            title="🎉 Registration Complete!",
            description=(
                f"Welcome to (uhhh idk island name), **{data['trainer_name']}**!\n\n"
                f"Your adventure begins with **{starter_species['name']}** at your side.\n\n"
                f"Use `/menu` to access all features and start your journey!"
            ),
            color=discord.Color.gold()
        )
        
        if data.get('avatar_url'):
            embed.set_thumbnail(url=data['avatar_url'])
        
        await interaction.followup.send(embed=embed, ephemeral=False)
        
        # Clean up temp data
        if hasattr(interaction.client, 'temp_registration_data'):
            del interaction.client.temp_registration_data


class RegistrationCog(commands.Cog):
    """Handles trainer registration"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @app_commands.command(name="register", description="Create your trainer profile and begin your journey")
    async def register(self, interaction: discord.Interaction):
        """Register as a new trainer"""
        # Defer immediately to avoid interaction timeout / Unknown interaction issues
        await interaction.response.defer(ephemeral=True)

        # Check if already registered
        if self.bot.player_manager.player_exists(interaction.user.id):
            embed = EmbedBuilder.error(
                "Already Registered",
                "You already have a trainer profile! Use `/menu` to continue your journey."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Show welcome message with begin button
        embed = EmbedBuilder.registration_welcome()
        view = RegistrationView()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    """Setup function for loading the cog"""
    await bot.add_cog(RegistrationCog(bot))