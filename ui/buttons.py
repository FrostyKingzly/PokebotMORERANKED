"""Button Views - Interactive Discord UI components"""

import logging

import discord
from discord.ui import Button, View, Select
from typing import Optional, List, Dict, Any

try:
    from cogs.pokemon_management_cog import PokemonActionsView as ManagementPokemonActionsView
except Exception:  # pragma: no cover - best effort import guard for runtime safety
    logging.getLogger(__name__).warning(
        "PokemonActionsView could not be imported; fallback view will be used for party details.",
        exc_info=True,
    )
    ManagementPokemonActionsView = None

try:
    from battle_engine_v2 import BattleFormat, BattleType
except Exception:
    BattleFormat = None
    BattleType = None


def reconstruct_pokemon_from_data(poke_data: dict, species_data: dict):
    """Rebuild a Pokemon instance from persisted party data."""
    from models import Pokemon
    import json

    # Build IVs dict from database fields
    ivs = {
        'hp': poke_data.get('iv_hp', 31),
        'attack': poke_data.get('iv_attack', 31),
        'defense': poke_data.get('iv_defense', 31),
        'sp_attack': poke_data.get('iv_sp_attack', 31),
        'sp_defense': poke_data.get('iv_sp_defense', 31),
        'speed': poke_data.get('iv_speed', 31)
    }

    # Moves are already deserialized by get_trainer_party but guard just in case
    moves_data = poke_data.get('moves', [])
    if isinstance(moves_data, str):
        moves_data = json.loads(moves_data)

    # Create Pokemon with empty moves list (to prevent auto-generation)
    pokemon = Pokemon(
        species_data=species_data,
        level=poke_data['level'],
        owner_discord_id=poke_data['owner_discord_id'],
        nature=poke_data['nature'],
        ability=poke_data['ability'],
        moves=[],
        ivs=ivs,
        is_shiny=bool(poke_data.get('is_shiny', 0))
    )

    # Immediately override moves with database data (preserves PP tracking)
    pokemon.moves = moves_data if moves_data else []

    # Set pokemon_id as attribute (not in constructor)
    pokemon.pokemon_id = poke_data.get('pokemon_id')

    # Set EVs (Pokemon starts with all 0, so update from database)
    pokemon.evs = {
        'hp': poke_data.get('ev_hp', 0),
        'attack': poke_data.get('ev_attack', 0),
        'defense': poke_data.get('ev_defense', 0),
        'sp_attack': poke_data.get('ev_sp_attack', 0),
        'sp_defense': poke_data.get('ev_sp_defense', 0),
        'speed': poke_data.get('ev_speed', 0)
    }

    # Recalculate stats with EVs (in case EVs were trained)
    pokemon._calculate_stats()

    # Now set current HP from database (after stats are calculated)
    pokemon.current_hp = poke_data['current_hp']

    # Set other attributes
    pokemon.gender = poke_data.get('gender')
    pokemon.nickname = poke_data.get('nickname')
    pokemon.held_item = poke_data.get('held_item')
    pokemon.status_condition = poke_data.get('status_condition')
    pokemon.friendship = poke_data.get('friendship', 70)

    # Additional attributes that might be in database
    if 'exp' in poke_data:
        pokemon.exp = poke_data['exp']
    if 'bond_level' in poke_data:
        pokemon.bond_level = poke_data['bond_level']
    if 'tera_type' in poke_data:
        pokemon.tera_type = poke_data['tera_type']

    return pokemon


class MainMenuView(View):
    """Main menu button interface"""
    
    def __init__(self, bot):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
    
    @discord.ui.button(label="👥 Party", style=discord.ButtonStyle.primary, row=0)
    async def party_button(self, interaction: discord.Interaction, button: Button):
        """View party Pokemon with management options"""
        from ui.embeds import EmbedBuilder

        # Get player's party
        party = self.bot.player_manager.get_party(interaction.user.id)

        if not party:
            await interaction.response.send_message(
                "Your party is empty! This shouldn't happen - contact an admin.",
                ephemeral=True
            )
            return

        trainer = self.bot.player_manager.get_player(interaction.user.id)
        current_location_id = getattr(trainer, 'current_location_id', None) if trainer else None
        location_manager = getattr(self.bot, 'location_manager', None)
        can_heal_party = bool(
            location_manager
            and current_location_id
            and location_manager.has_pokemon_center(current_location_id)
        )

        # Show party management view
        embed = EmbedBuilder.party_view(party, self.bot.species_db)
        view = PartyManagementView(self.bot, party, can_heal_party=can_heal_party)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label="📦 Boxes", style=discord.ButtonStyle.primary, row=0)
    async def boxes_button(self, interaction: discord.Interaction, button: Button):
        """View stored Pokemon"""
        from ui.embeds import EmbedBuilder
        
        # Get boxed Pokemon
        boxes = self.bot.player_manager.get_boxes(interaction.user.id)
        
        if not boxes:
            await interaction.response.send_message(
                "📦 Your storage boxes are empty! Catch more Pokémon to fill them up. Catch more Pokémon to fill them up.",
                ephemeral=True
            )
            return
        
        # Show box view
        embed = EmbedBuilder.box_view(boxes, self.bot.species_db, page=0, total_pages=max(1, (len(boxes) + 29) // 30))
        view = BoxManagementView(self.bot, boxes, page=0)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label="🎒 Bag", style=discord.ButtonStyle.primary, row=0)
    async def bag_button(self, interaction: discord.Interaction, button: Button):
        """Open bag/inventory"""
        from ui.embeds import EmbedBuilder
        
        # Get player's inventory
        inventory = self.bot.player_manager.get_inventory(interaction.user.id)
        
        if not inventory:
            # Give starter items if empty
            starter_items = {
                'potion': 5,
                'poke_ball': 10,
                'antidote': 3,
                'paralyze_heal': 3
            }
            
            for item_id, qty in starter_items.items():
                self.bot.player_manager.add_item(interaction.user.id, item_id, qty)
            
            inventory = self.bot.player_manager.get_inventory(interaction.user.id)
        
        embed = EmbedBuilder.bag_view(inventory, self.bot.items_db)
        view = BagView(self.bot, inventory)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    @discord.ui.button(label="⚔️ Wild Encounter", style=discord.ButtonStyle.success, row=1)
    async def encounter_button(self, interaction: discord.Interaction, button: Button):
        """Roll wild encounters at current location"""
        from ui.embeds import EmbedBuilder
        
        # Get player's current location
        trainer = self.bot.player_manager.get_player(interaction.user.id)
        current_location_id = trainer.current_location_id

        # Ensure the interaction is happening in the correct location channel
        channel_location_id = self.bot.location_manager.get_location_by_channel(interaction.channel_id)
        if not channel_location_id:
            await interaction.response.send_message(
                "⚠️ This channel hasn't been linked to a location yet. Use /set_location here first.",
                ephemeral=True
            )
            return

        if channel_location_id != current_location_id:
            channel_location_name = self.bot.location_manager.get_location_name(channel_location_id)
            current_location_name = self.bot.location_manager.get_location_name(current_location_id)
            await interaction.response.send_message(
                (
                    f"⚠️ This channel is linked to **{channel_location_name}**, but you're currently at "
                    f"**{current_location_name}**. Travel to that location and use its channel for wild encounters."
                ),
                ephemeral=True
            )
            return

        # Get location data
        location = self.bot.location_manager.get_location(current_location_id)
        if not location:
            await interaction.response.send_message(
                "❌ This location has no wild encounters!",
                ephemeral=True
            )
            return
        
        # Check if location has encounters
        if not location.get('encounters'):
            await interaction.response.send_message(
                f"❌ {location.get('name', 'This location')} has no wild Pokémon!",
                ephemeral=True
            )
            return
        
        # Defer response for rolling encounters
        await interaction.response.defer(ephemeral=True)
        
        # Roll 10 encounters
        encounters = self.bot.location_manager.roll_multiple_encounters(
            current_location_id,
            10,
            self.bot.species_db
        )
        
        if not encounters:
            await interaction.followup.send(
                "❌ Failed to generate encounters. Try again!",
                ephemeral=True
            )
            return
        
        # Show encounter selection view
        embed = EmbedBuilder.encounter_roll(encounters, location)
        view = EncounterSelectView(
            self.bot,
            encounters,
            location,
            interaction.user.id,
            current_location_id
        )
        
        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True
        )
    
    @discord.ui.button(label="🧭 Travel", style=discord.ButtonStyle.secondary, row=1)
    async def travel_button(self, interaction: discord.Interaction, button: Button):
        """Travel to new location"""
        from ui.embeds import EmbedBuilder
        
        # Get player's current location
        trainer = self.bot.player_manager.get_player(interaction.user.id)
        current_location_id = trainer.current_location_id
        
        # Get all locations
        all_locations = self.bot.location_manager.get_all_locations()
        
        if not all_locations or len(all_locations) <= 1:
            await interaction.response.send_message(
                "🧭 No other locations available to travel to!",
                ephemeral=True
            )
            return
        
        # Show travel selection
        embed = EmbedBuilder.travel_select(all_locations, current_location_id)
        view = TravelSelectView(self.bot, all_locations, current_location_id)
        
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )
    
    @discord.ui.button(label="🛒 Shop", style=discord.ButtonStyle.secondary, row=1)
    async def shop_button(self, interaction: discord.Interaction, button: Button):
        """Open shop"""
        shop_cog = self.bot.get_cog("ShopCog")
        if not shop_cog:
            await interaction.response.send_message(
                "❌ The shop system is not available right now.",
                ephemeral=True
            )
            return

        await shop_cog.open_shop_for_user(interaction)
    
    @discord.ui.button(label="📘 Pokédex", style=discord.ButtonStyle.secondary, row=1)
    async def pokedex_button(self, interaction: discord.Interaction, button: Button):
        """View Pokedex"""
        await interaction.response.send_message(
            "📘 Pokédex coming soon!",
            ephemeral=True
        )
    
    @discord.ui.button(label="🧑‍🎓 Trainer Card", style=discord.ButtonStyle.secondary, row=2)
    async def trainer_card_button(self, interaction: discord.Interaction, button: Button):
        """View trainer card"""
        from ui.embeds import EmbedBuilder
        
        trainer = self.bot.player_manager.get_player(interaction.user.id)
        party = self.bot.player_manager.get_party(interaction.user.id)
        total_pokemon = len(self.bot.player_manager.get_all_pokemon(interaction.user.id))
        pokedex = self.bot.player_manager.get_pokedex(interaction.user.id)
        
        embed = EmbedBuilder.trainer_card(
            trainer,
            party_count=len(party),
            total_pokemon=total_pokemon,
            pokedex_seen=len(pokedex)
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="⚔️ Battle", style=discord.ButtonStyle.danger, row=2)
    async def battle_button(self, interaction: discord.Interaction, button: Button):
        """Battle options"""
        from ui.embeds import EmbedBuilder
        
        # Get player's current location
        trainer = self.bot.player_manager.get_player(interaction.user.id)
        current_location_id = trainer.current_location_id
        location = self.bot.location_manager.get_location(current_location_id)

        available_pvp = None
        if location:
            try:
                players_here = self.bot.player_manager.get_players_in_location(
                    current_location_id,
                    exclude_user_id=interaction.user.id
                )
            except AttributeError:
                players_here = []
            battle_cog = self.bot.get_cog('BattleCog')
            busy_ids = set(battle_cog.user_battles.keys()) if battle_cog else set()
            available_pvp = len([
                p for p in players_here
                if getattr(p, 'discord_user_id', None) not in busy_ids
            ])

        # Show battle menu
        embed = EmbedBuilder.battle_menu(location, available_pvp=available_pvp)
        view = BattleMenuView(self.bot, location)

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )


class RegistrationView(View):
    """Registration flow buttons"""
    
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="Begin Registration", style=discord.ButtonStyle.success)
    async def begin_button(self, interaction: discord.Interaction, button: Button):
        """Start registration process"""
        # Import here to avoid circular imports
        from cogs.registration_cog import RegistrationModal

        modal = RegistrationModal()
        try:
            # First and only response to this interaction
            await interaction.response.send_modal(modal)
        except discord.NotFound:
            # Interaction token expired (button too old); user will need to run /register again
            pass

class StarterSelectView(View):
    """Starter Pokemon selection with pagination and manual entry"""

    def __init__(self, species_db, selection_future, page: int = 0):
        super().__init__(timeout=300)
        self.species_db = species_db
        self.selection_future = selection_future
        self.page = page
        self.starters = species_db.get_all_starters()
        self.selected_species = None
        self.per_page = 25
        self.total_pages = max(1, (len(self.starters) + self.per_page - 1) // self.per_page)
        self.message: Optional[discord.Message] = None

        self._rebuild_components()

    def _rebuild_components(self):
        """(Re)build the select menu and buttons"""
        self.clear_items()
        self.add_item(self._build_starter_select())

        manual_button = Button(
            label="Enter Dex #",
            style=discord.ButtonStyle.primary,
            row=1
        )
        manual_button.callback = self.prompt_dex_number
        self.add_item(manual_button)

        if self.total_pages > 1:
            self._add_navigation_buttons()

    def _build_starter_select(self) -> Select:
        start_idx = self.page * self.per_page
        end_idx = min(start_idx + self.per_page, len(self.starters))
        page_starters = self.starters[start_idx:end_idx]

        options = []
        for species in page_starters:
            types = "/".join([t.title() for t in species['types']])
            label = f"#{species['dex_number']:03d} - {species['name']}"
            description = f"Type: {types}"

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(species['dex_number']),
                    description=description[:100]
                )
            )

        select = Select(
            placeholder="Choose your starter Pokémon...",
            options=options,
            custom_id="starter_select"
        )
        select.callback = self.starter_callback
        return select

    def _add_navigation_buttons(self):
        prev_button = Button(
            label="< Previous",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page == 0),
            row=2
        )
        prev_button.callback = self.prev_page
        self.add_item(prev_button)

        page_button = Button(
            label=f"Page {self.page + 1}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            row=2
        )
        self.add_item(page_button)

        next_button = Button(
            label="Next >",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.total_pages - 1),
            row=2
        )
        next_button.callback = self.next_page
        self.add_item(next_button)

    async def starter_callback(self, interaction: discord.Interaction):
        """Handle starter selection"""
        dex_number = int(interaction.data['values'][0])
        species = self.species_db.get_species(dex_number)

        if not species:
            await interaction.response.send_message(
                "❌ Something went wrong fetching that Pokemon. Please pick again.",
                ephemeral=True
            )
            return

        self.selected_species = species
        self.stop()

        # Show confirmation
        await interaction.response.send_message(
            f"✅ You selected **{species['name']}**! Processing your registration...",
            ephemeral=True
        )

        await self._finalize(interaction.message)

    async def prev_page(self, interaction: discord.Interaction):
        """Go to previous page"""
        if self.selection_future.done():
            await interaction.response.send_message(
                "Starter selection is locked in. Continue with the next step!",
                ephemeral=True
            )
            return

        if self.page > 0:
            self.page -= 1
            new_view = StarterSelectView(self.species_db, self.selection_future, self.page)
            new_view.message = interaction.message
            await interaction.response.edit_message(view=new_view)
            self.stop()

    async def next_page(self, interaction: discord.Interaction):
        """Go to next page"""
        if self.selection_future.done():
            await interaction.response.send_message(
                "Starter selection is locked in. Continue with the next step!",
                ephemeral=True
            )
            return

        if self.page < self.total_pages - 1:
            self.page += 1
            new_view = StarterSelectView(self.species_db, self.selection_future, self.page)
            new_view.message = interaction.message
            await interaction.response.edit_message(view=new_view)
            self.stop()

    async def prompt_dex_number(self, interaction: discord.Interaction):
        if self.selection_future.done():
            await interaction.response.send_message(
                "Starter selection is already complete.",
                ephemeral=True
            )
            return

        modal = DexNumberModal(self)
        await interaction.response.send_modal(modal)

    async def _finalize(self, message: Optional[discord.Message]):
        """Disable the view once a starter has been chosen"""
        self.stop()
        if not message and hasattr(self, "message"):
            message = self.message

        if not message:
            return

        try:
            await message.edit(view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if not self.selection_future.done():
            self.selection_future.set_result(None)

        if self.message:
            for child in self.children:
                child.disabled = True
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class DexNumberModal(discord.ui.Modal, title="Enter Pokédex Number"):
    """Modal that lets trainers jump directly to a Dex number"""

    dex_number = discord.ui.TextInput(
        label="Pokédex #",
        placeholder="Enter a number like 1 or 025",
        max_length=4
    )

    def __init__(self, starter_view: StarterSelectView):
        super().__init__(timeout=None)
        self.starter_view = starter_view

    async def on_submit(self, interaction: discord.Interaction):
        if self.starter_view.selection_future.done():
            await interaction.response.send_message(
                "Starter selection already completed.",
                ephemeral=True
            )
            return

        raw_value = self.dex_number.value.strip().lstrip('#')
        if not raw_value.isdigit():
            await interaction.response.send_message(
                "❌ Please provide a valid Pokédex number (digits only).",
                ephemeral=True
            )
            return

        species = self.starter_view.species_db.get_species(int(raw_value))
        if not species:
            await interaction.response.send_message(
                "❌ That Pokédex number isn't in this region. Try again!",
                ephemeral=True
            )
            return

        self.starter_view.selected_species = species
        if not self.starter_view.selection_future.done():
            self.starter_view.selection_future.set_result(species)

        await interaction.response.send_message(
            f"✅ You selected **{species['name']}**! Processing your registration...",
            ephemeral=True
        )

        await self.starter_view._finalize(self.starter_view.message)


class SocialStatsView(View):
    """Social stats boon/bane selection"""
    
    def __init__(self):
        super().__init__(timeout=300)
        self.boon_stat = None
        self.bane_stat = None
        
        # Add boon select
        boon_options = [
            discord.SelectOption(label="Heart", value="heart",
                               description="Empathy & compassion for people and Pokémon"),
            discord.SelectOption(label="Insight", value="insight",
                               description="Perception, research, and tactical thinking"),
            discord.SelectOption(label="Charisma", value="charisma",
                               description="Confidence, influence, and negotiations"),
            discord.SelectOption(label="Fortitude", value="fortitude",
                               description="Physical grit, travel, and athletic feats"),
            discord.SelectOption(label="Will", value="will",
                               description="Determination and inner strength"),
        ]
        
        boon_select = Select(
            placeholder="Choose your BOON stat (starts at Rank 2)...",
            options=boon_options,
            custom_id="boon_select"
        )
        boon_select.callback = self.boon_callback
        self.add_item(boon_select)
        
        # Add bane select
        bane_select = Select(
            placeholder="Choose your BANE stat (starts at Rank 0)...",
            options=boon_options,
            custom_id="bane_select"
        )
        bane_select.callback = self.bane_callback
        self.add_item(bane_select)
    
    async def boon_callback(self, interaction: discord.Interaction):
        """Handle boon selection"""
        self.boon_stat = interaction.data['values'][0]
        await interaction.response.send_message(
            f"✔ **{self.boon_stat.title()}** will be your strength! (Rank 2)",
            ephemeral=True
        )
        
        # Check if both selections are complete
        if self.boon_stat and self.bane_stat:
            self.stop()
    
    async def bane_callback(self, interaction: discord.Interaction):
        """Handle bane selection"""
        self.bane_stat = interaction.data['values'][0]
        
        if self.boon_stat == self.bane_stat:
            await interaction.response.send_message(
                "❌ You cannot choose the same stat as both Boon and Bane!",
                ephemeral=True
            )
            self.bane_stat = None  # Reset bane selection
            return
        
        await interaction.response.send_message(
            f"✔ **{self.bane_stat.title()}** will be your weakness. (Rank 0)\n\n"
            f"Moving to confirmation...",
            ephemeral=True
        )
        
        # Check if both selections are complete
        if self.boon_stat and self.bane_stat:
            self.stop()


class ConfirmationView(View):
    """Generic confirmation buttons"""
    
    def __init__(self):
        super().__init__(timeout=60)
        self.value = None
    
    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: Button):
        """Confirm action"""
        self.value = True
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        """Cancel action"""
        self.value = False
        await interaction.response.defer()
        self.stop()


class PokemonDetailsFallbackView(View):
    """Simple dismiss-only view when management cog isn't available."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def close_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(view=None)


class PartyManagementView(View):
    """Party management interface"""

    def __init__(self, bot, party: list, *, can_heal_party: bool = False):
        super().__init__(timeout=300)
        self.bot = bot
        self.party = party
        self.can_heal_party = can_heal_party

        # Add Pokemon select menu
        options = []
        for i, poke in enumerate(party, 1):
            species_data = bot.species_db.get_species(poke['species_dex_number'])
            name = poke.get('nickname') or species_data['name']
            
            label = f"Slot {i}: {name} (Lv. {poke['level']})"
            description = f"HP: {poke['current_hp']}/{poke['max_hp']}"
            
            # Add held item if present
            if poke.get('held_item'):
                item_data = bot.items_db.get_item(poke['held_item'])
                if item_data:
                    description += f" | Holding: {item_data['name']}"
            
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(poke['pokemon_id']),
                    description=description[:100]
                )
            )
        
        select = Select(
            placeholder="Select a Pokémon to manage...",
            options=options,
            custom_id="party_select"
        )
        select.callback = self.pokemon_callback
        self.add_item(select)
        

        # Add Move to Box button
        move_box_button = Button(
            label="📦 Move to Box",
            style=discord.ButtonStyle.secondary,
            custom_id="move_to_box",
            row=1
        )
        move_box_button.callback = self.move_to_box_callback
        self.add_item(move_box_button)

        # Add Swap Positions button
        swap_button = Button(
            label="🔄 Swap",
            style=discord.ButtonStyle.primary,
            custom_id="swap_party",
            row=1,
        )
        swap_button.callback = self.swap_party_callback
        self.add_item(swap_button)

        # Add Reorder button
        reorder_button = Button(
            label="↕️ Reorder",
            style=discord.ButtonStyle.secondary,
            custom_id="reorder_party",
            row=1,
        )
        reorder_button.callback = self.reorder_party_callback
        self.add_item(reorder_button)

        if self.can_heal_party:
            heal_button = Button(
                label="🩺 Heal Party",
                style=discord.ButtonStyle.success,
                custom_id="heal_party",
                row=1,
            )
            heal_button.callback = self.heal_party_callback
            self.add_item(heal_button)
    
    async def pokemon_callback(self, interaction: discord.Interaction):
        """Show detailed Pokemon info"""
        from ui.embeds import EmbedBuilder
        
        # pokemon_id values can be UUID strings, so avoid forcing an int cast
        selected_value = interaction.data['values'][0]
        
        # Find the Pokemon in party
        pokemon_data = None
        for poke in self.party:
            if str(poke.get('pokemon_id')) == str(selected_value):
                pokemon_data = poke
                break

        if not pokemon_data:
            await interaction.response.send_message(
                "❌ Pokémon not found!",
                ephemeral=True
            )
            return

        # Get species data and move details for the embed
        species_data = self.bot.species_db.get_species(
            pokemon_data['species_dex_number']
        )

        move_data_list = []
        for move in pokemon_data.get('moves', []):
            move_data = self.bot.moves_db.get_move(move['move_id'])
            if move_data:
                move_data_list.append(move_data)

        # Show detailed view with the comprehensive summary embed
        embed = EmbedBuilder.pokemon_summary(
            pokemon_data,
            species_data,
            move_data_list
        )
        # Prefer the management cog's actions view if it's available; otherwise use a simple fallback view.
        if ManagementPokemonActionsView is not None:
            view = ManagementPokemonActionsView(self.bot, pokemon_data, species_data)
        else:
            view = PokemonDetailsFallbackView()

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    

    async def swap_party_callback(self, interaction: discord.Interaction):
        """Open a small view to choose two Pokémon to swap positions."""
        view = PartySwapView(self.bot, interaction.user.id)
        await interaction.response.send_message(
            "Select two Pokémon to swap their positions.",
            view=view,
            ephemeral=True,
        )

    async def reorder_party_callback(self, interaction: discord.Interaction):
        """Open a guided view to set a full custom party order."""
        view = PartyReorderView(self.bot, interaction.user.id)
        await interaction.response.send_message(
            "Let's reorder your party. Choose which Pokémon should be first, then second, and so on.",
            view=view,
            ephemeral=True,
        )
    async def move_to_box_callback(self, interaction: discord.Interaction):
        """Show interface to move Pokemon to box"""
        await interaction.response.send_message(
            "Select a Pokémon from the dropdown first, then use the detailed view to move it to the box.",
            ephemeral=True
        )

    async def heal_party_callback(self, interaction: discord.Interaction):
        """Heal the player's party when they're standing near a Pokémon Center."""
        trainer = self.bot.player_manager.get_player(interaction.user.id)
        location_manager = getattr(self.bot, 'location_manager', None)
        current_location_id = getattr(trainer, 'current_location_id', None) if trainer else None

        can_heal_here = bool(
            self.can_heal_party
            and location_manager
            and current_location_id
            and location_manager.has_pokemon_center(current_location_id)
        )

        if not can_heal_here:
            await interaction.response.send_message(
                "There's no Pokémon Center nearby. Travel to one to heal for free!",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        healed = self.bot.player_manager.heal_party(interaction.user.id)
        self.party = self.bot.player_manager.get_party(interaction.user.id)

        from ui.embeds import EmbedBuilder

        embed = EmbedBuilder.party_view(self.party, self.bot.species_db)
        await interaction.edit_original_response(embed=embed, view=self)

        if healed:
            message = "Nurse Joy restored your entire party!"
        else:
            message = "All of your Pokémon are already in perfect condition."

        await interaction.followup.send(message, ephemeral=True)




class PartySwapView(View):
    """Ephemeral view used to choose two Pokémon to swap positions."""
    def __init__(self, bot, discord_user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.discord_user_id = discord_user_id

        party = self.bot.player_manager.get_party(discord_user_id)
        options = []
        for i, poke in enumerate(party, 1):
            species = self.bot.species_db.get_species(poke['species_dex_number'])
            name = poke.get('nickname') or (species['name'] if species else "Pokemon")
            label = f"Slot {i}: {name} (Lv. {poke['level']})"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(poke['pokemon_id']),
                )
            )

        select = Select(
            placeholder="Choose two Pokémon to swap",
            min_values=2,
            max_values=2,
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        if len(values) != 2:
            await interaction.response.send_message(
                "Please choose exactly two Pokémon.",
                ephemeral=True,
            )
            return

        pid1, pid2 = values[0], values[1]
        success, message = self.bot.player_manager.swap_party_positions(
            self.discord_user_id, pid1, pid2
        )
        if not success:
            await interaction.response.send_message(message, ephemeral=True)
            return

        # Refresh the main party view embed for the user
        from ui.embeds import EmbedBuilder

        party = self.bot.player_manager.get_party(self.discord_user_id)
        embed = EmbedBuilder.party_view(party, self.bot.species_db)
        try:
            await interaction.response.edit_message(
                content="Party order updated.",
                embed=embed,
                view=None,
            )
        except Exception:
            await interaction.response.send_message(
                "Party order updated!", ephemeral=True
            )


class PartyReorderView(View):
    """Ephemeral view to set a full custom party order step by step."""
    def __init__(self, bot, discord_user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.discord_user_id = discord_user_id
        self.selected_ids: list[str] = []

        self._build_select()

    def _build_select(self):
        self.clear_items()
        party = self.bot.player_manager.get_party(self.discord_user_id)
        remaining = [p for p in party if str(p['pokemon_id']) not in self.selected_ids]

        options = []
        for i, poke in enumerate(remaining, 1):
            species = self.bot.species_db.get_species(poke['species_dex_number'])
            name = poke.get('nickname') or (species['name'] if species else "Pokemon")
            label = f"{name} (Lv. {poke['level']})"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(poke['pokemon_id']),
                )
            )

        select = Select(
            placeholder=f"Choose Pokémon for position {len(self.selected_ids)+1}",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        if not values:
            await interaction.response.send_message(
                "Please pick a Pokémon.",
                ephemeral=True,
            )
            return

        chosen_id = values[0]
        if chosen_id in self.selected_ids:
            await interaction.response.send_message(
                "You've already placed that Pokémon in the order.",
                ephemeral=True,
            )
            return

        self.selected_ids.append(chosen_id)

        # Check if we're done (all party Pokémon have been ordered)
        party = self.bot.player_manager.get_party(self.discord_user_id)
        if len(self.selected_ids) >= len(party):
            success, message = self.bot.player_manager.reorder_party(
                self.discord_user_id, self.selected_ids
            )
            if not success:
                await interaction.response.send_message(message, ephemeral=True)
                return

            from ui.embeds import EmbedBuilder
            new_party = self.bot.player_manager.get_party(self.discord_user_id)
            embed = EmbedBuilder.party_view(new_party, self.bot.species_db)
            try:
                await interaction.response.edit_message(
                    content="Party order updated.",
                    embed=embed,
                    view=None,
                )
            except Exception:
                await interaction.response.send_message(
                    "Party order updated!", ephemeral=True
                )
            return

        # Otherwise, rebuild select for the next slot
        self._build_select()
        await interaction.response.edit_message(view=self)

class BoxManagementView(View):
    """Box management interface with pagination"""
    
    def __init__(self, bot, boxes: list, page: int = 0):
        super().__init__(timeout=300)
        self.bot = bot
        self.boxes = boxes
        self.page = page
        self.items_per_page = 30
        self.total_pages = max(1, (len(boxes) + self.items_per_page - 1) // self.items_per_page)
        
        # Calculate page range
        start_idx = page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, len(boxes))
        page_boxes = boxes[start_idx:end_idx]
        
        # Add Pokemon select menu (max 25 options)
        options = []
        for i, poke in enumerate(page_boxes[:25], start_idx + 1):
            species_data = bot.species_db.get_species(poke['species_dex_number'])
            name = poke.get('nickname') or species_data['name']
            
            label = f"#{i}: {name} (Lv. {poke['level']})"
            description = f"HP: {poke['current_hp']}/{poke['max_hp']}"
            
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(poke['pokemon_id']),
                    description=description[:100]
                )
            )
        
        if options:
            select = Select(
                placeholder="Select a Pokémon to manage...",
                options=options,
                custom_id="box_select"
            )
            select.callback = self.pokemon_callback
            self.add_item(select)
        
        # Add pagination if needed
        if self.total_pages > 1:
            self.add_navigation_buttons()
    
    def add_navigation_buttons(self):
        """Add page navigation"""
        # Previous button
        prev_button = Button(
            label="â—€ Previous",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page == 0),
            row=1
        )
        prev_button.callback = self.prev_page
        self.add_item(prev_button)
        
        # Page indicator
        page_button = Button(
            label=f"Page {self.page + 1}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            row=1
        )
        self.add_item(page_button)
        
        # Next button
        next_button = Button(
            label="Next â–¶",
            style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.total_pages - 1),
            row=1
        )
        next_button.callback = self.next_page
        self.add_item(next_button)
    
    async def pokemon_callback(self, interaction: discord.Interaction):
        """Show detailed Pokemon info"""
        from ui.embeds import EmbedBuilder

        # The select stores the Pokémon's unique ID (string / UUID) as its value
        selected_value = interaction.data["values"][0]

        # Find the Pokémon in all boxes
        pokemon_data = None
        for poke in self.boxes:
            # pokemon_id is stored as a UUID-like string, so compare as strings
            if str(poke.get("pokemon_id")) == str(selected_value):
                pokemon_data = poke
                break

        if not pokemon_data:
            await interaction.response.send_message(
                "❌ Pokémon not found!",
                ephemeral=True,
            )
            return

        # Get species data
        species_data = self.bot.species_db.get_species(
            pokemon_data["species_dex_number"]
        )

        # Build and send the Pokémon summary embed with an actions view (e.g., Add to Party)
        embed = EmbedBuilder.pokemon_summary(pokemon_data, species_data)
        view = BoxPokemonActionsView(self.bot, pokemon_data)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    
    async def prev_page(self, interaction: discord.Interaction):
        """Go to previous page"""
        from ui.embeds import EmbedBuilder
        
        if self.page > 0:
            self.page -= 1
            embed = EmbedBuilder.box_view(self.boxes, self.bot.species_db, self.page, self.total_pages)
            new_view = BoxManagementView(self.bot, self.boxes, self.page)
            await interaction.response.edit_message(embed=embed, view=new_view)
    
    async def next_page(self, interaction: discord.Interaction):
        """Go to next page"""
        from ui.embeds import EmbedBuilder
        
        if self.page < self.total_pages - 1:
            self.page += 1
            embed = EmbedBuilder.box_view(self.boxes, self.bot.species_db, self.page, self.total_pages)
            new_view = BoxManagementView(self.bot, self.boxes, self.page)
            await interaction.response.edit_message(embed=embed, view=new_view)
        release_button.callback = self.release_callback
        self.add_item(release_button)
    
    async def use_item_callback(self, interaction: discord.Interaction):
        """Use item on Pokemon"""
        from ui.embeds import EmbedBuilder
        
        # Get player's inventory
        inventory = self.bot.player_manager.get_inventory(interaction.user.id)
        
        if not inventory:
            await interaction.response.send_message(
                "🎒 Your bag is empty! Buy items from the shop.",
                ephemeral=True
            )
            return
        
        # Show item selection
        embed = EmbedBuilder.item_use_select(inventory, self.pokemon_data, self.bot.items_db)
        view = ItemUseView(self.bot, inventory, self.pokemon_data)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    async def move_to_party_callback(self, interaction: discord.Interaction):
        """Move Pokemon from box to party"""
        # Use the PlayerManager's withdraw_pokemon helper, which already
        # handles all the ownership/party-size/position logic.
        success, message = self.bot.player_manager.withdraw_pokemon(
            interaction.user.id,
            str(self.pokemon_data.get('pokemon_id') or self.pokemon_data.get('id'))
        )

        if success:
            species_data = self.bot.species_db.get_species(self.pokemon_data['species_dex_number'])
            name = self.pokemon_data.get('nickname') or species_data.get('name', 'Pokémon')
            await interaction.response.send_message(
                f"✅ Moved **{name}** to your party!",
                ephemeral=True
            )
            self.stop()
        else:
            await interaction.response.send_message(
                message or "❌ Failed to move Pokémon. Try again!",
                ephemeral=True
            )

    async def move_to_box_callback(self, interaction: discord.Interaction):
        """Move Pokemon from party to box"""
        # Check party size
        party = self.bot.player_manager.get_party(interaction.user.id)
        if len(party) <= 1:
            await interaction.response.send_message(
                "❌ You must have at least one Pokémon in your party!",
                ephemeral=True
            )
            return
        
        # Move to box
        success = self.bot.player_manager.move_to_box(
            interaction.user.id,
            self.pokemon_data['id']
        )
        
        if success:
            species_data = self.bot.species_db.get_species(self.pokemon_data['species_dex_number'])
            name = self.pokemon_data.get('nickname') or species_data['name']
            await interaction.response.send_message(
                f"✅ Moved **{name}** to your box!",
                ephemeral=True
            )
            self.stop()
        else:
            await interaction.response.send_message(
                "❌ Failed to move Pokémon. Try again!",
                ephemeral=True
            )
    
    async def give_item_callback(self, interaction: discord.Interaction):
        """Give held item to Pokemon"""
        from ui.embeds import EmbedBuilder
        
        # Get player's inventory
        inventory = self.bot.player_manager.get_inventory(interaction.user.id)
        
        if not inventory:
            await interaction.response.send_message(
                "🎒 Your bag is empty! Buy items from the shop.",
                ephemeral=True
            )
            return
        
        # Filter for held items only
        held_items = {k: v for k, v in inventory.items() 
                     if self.bot.items_db.get_item(k).get('category') == 'held_item'}
        
        if not held_items:
            await interaction.response.send_message(
                "🎒 You don't have any held items!",
                ephemeral=True
            )
            return
        
        # Show item selection
        embed = EmbedBuilder.held_item_select(held_items, self.pokemon_data, self.bot.items_db)
        view = HeldItemView(self.bot, held_items, self.pokemon_data)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    
    async def release_callback(self, interaction: discord.Interaction):
        """Release Pokemon (with confirmation)"""
        species_data = self.bot.species_db.get_species(self.pokemon_data['species_dex_number'])
        name = self.pokemon_data.get('nickname') or species_data['name']
        
        # Show confirmation view
        embed = discord.Embed(
            title="âš  Release Pokémon?",
            description=f"Are you sure you want to release **{name}** (Lv. {self.pokemon_data['level']})?\n\n"
                       f"**This action cannot be undone!**",
            color=discord.Color.red()
        )
        
        view = ReleaseConfirmView(self.bot, self.pokemon_data)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        self.stop()


class ItemUseView(View):
    """Item usage selection view"""
    
    def __init__(self, bot, inventory: dict, pokemon_data: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.inventory = inventory
        self.pokemon_data = pokemon_data
        
        # Filter usable items (healing, status cure, etc.)
        usable_items = {}
        for item_id, quantity in inventory.items():
            item_data = bot.items_db.get_item(item_id)
            if item_data and item_data.get('category') in ['healing', 'status_cure', 'vitamin', 'evolution']:
                usable_items[item_id] = quantity
        
        if not usable_items:
            return
        
        # Create dropdown (max 25 items)
        options = []
        for item_id, quantity in list(usable_items.items())[:25]:
            item_data = bot.items_db.get_item(item_id)
            label = f"{item_data['name']} (x{quantity})"
            description = item_data.get('description', '')[:100]
            
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=item_id,
                    description=description
                )
            )
        
        select = Select(
            placeholder="Choose an item to use...",
            options=options,
            custom_id="item_select"
        )
        select.callback = self.item_callback
        self.add_item(select)
    
    async def item_callback(self, interaction: discord.Interaction):
        """Use the selected item"""
        item_id = interaction.data['values'][0]
        
        # Use the item
        result = self.bot.player_manager.use_item_on_pokemon(
            interaction.user.id,
            item_id,
            self.pokemon_data['id']
        )
        
        if result['success']:
            item_data = self.bot.items_db.get_item(item_id)
            species_data = self.bot.species_db.get_species(self.pokemon_data['species_dex_number'])
            name = self.pokemon_data.get('nickname') or species_data['name']
            
            await interaction.response.send_message(
                f"✅ Used **{item_data['name']}** on **{name}**!\n{result['message']}",
                ephemeral=True
            )
            self.stop()
        else:
            await interaction.response.send_message(
                f"❌ {result['message']}",
                ephemeral=True
            )


class HeldItemView(View):
    """Held item selection view"""
    
    def __init__(self, bot, held_items: dict, pokemon_data: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.held_items = held_items
        self.pokemon_data = pokemon_data
        
        # Create dropdown
        options = []
        for item_id, quantity in list(held_items.items())[:25]:
            item_data = bot.items_db.get_item(item_id)
            label = f"{item_data['name']} (x{quantity})"
            description = item_data.get('description', '')[:100]
            
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=item_id,
                    description=description
                )
            )
        
        select = Select(
            placeholder="Choose an item to give...",
            options=options,
            custom_id="held_item_select"
        )
        select.callback = self.item_callback
        self.add_item(select)
    
    async def item_callback(self, interaction: discord.Interaction):
        """Give the selected item"""
        item_id = interaction.data['values'][0]
        
        # Give the item
        success = self.bot.player_manager.give_held_item(
            interaction.user.id,
            self.pokemon_data['id'],
            item_id
        )
        
        if success:
            item_data = self.bot.items_db.get_item(item_id)
            species_data = self.bot.species_db.get_species(self.pokemon_data['species_dex_number'])
            name = self.pokemon_data.get('nickname') or species_data['name']
            
            # Check if Pokemon was already holding something
            if self.pokemon_data.get('held_item'):
                old_item = self.bot.items_db.get_item(self.pokemon_data['held_item'])
                await interaction.response.send_message(
                    f"✅ **{name}** is now holding **{item_data['name']}**!\n"
                    f"(Returned **{old_item['name']}** to bag)",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"✅ **{name}** is now holding **{item_data['name']}**!",
                    ephemeral=True
                )
            self.stop()
        else:
            await interaction.response.send_message(
                "❌ Failed to give item. Try again!",
                ephemeral=True
            )


class ReleaseConfirmView(View):
    """Confirmation view for releasing Pokemon"""
    
    def __init__(self, bot, pokemon_data: dict):
        super().__init__(timeout=60)
        self.bot = bot
        self.pokemon_data = pokemon_data
        
        # Confirm button
        confirm_button = Button(
            label="✅ Yes, Release",
            style=discord.ButtonStyle.danger,
            custom_id="confirm_release"
        )
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)
        
        # Cancel button
        cancel_button = Button(
            label="❌ Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="cancel_release"
        )
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)
    
    async def confirm_callback(self, interaction: discord.Interaction):
        """Confirm release"""
        # Check party size
        party = self.bot.player_manager.get_party(interaction.user.id)
        in_party = any(p['id'] == self.pokemon_data['id'] for p in party)
        
        if in_party and len(party) <= 1:
            await interaction.response.send_message(
                "❌ You can't release your last Pokémon!",
                ephemeral=True
            )
            self.stop()
            return
        
        # Release the Pokemon
        success = self.bot.player_manager.release_pokemon(
            interaction.user.id,
            self.pokemon_data['id']
        )
        
        if success:
            species_data = self.bot.species_db.get_species(self.pokemon_data['species_dex_number'])
            name = self.pokemon_data.get('nickname') or species_data['name']
            
            await interaction.response.send_message(
                f"✅ Released **{name}**. Goodbye, friend! 👋",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Failed to release Pokémon. Try again!",
                ephemeral=True
            )
        
        self.stop()
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel release"""
        await interaction.response.send_message(
            "✅ Cancelled. Your Pokémon is safe!",
            ephemeral=True
        )
        self.stop()


class BagView(View):
    """Bag/Inventory view"""
    
    def __init__(self, bot, inventory: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.inventory = inventory
        
        # Add category buttons
        categories = ['all', 'healing', 'poke_ball', 'status_cure', 'held_item', 'vitamin']
        
        for category in categories:
            button = Button(
                label=category.replace('_', ' ').title(),
                style=discord.ButtonStyle.secondary,
                custom_id=f"bag_{category}"
            )
            button.callback = self.create_category_callback(category)
            self.add_item(button)
    
    def create_category_callback(self, category: str):
        """Create callback for category button"""
        async def callback(interaction: discord.Interaction):
            from ui.embeds import EmbedBuilder
            
            # Filter inventory by category
            if category == 'all':
                filtered_inv = self.inventory
            else:
                filtered_inv = {}
                for item_id, quantity in self.inventory.items():
                    item_data = self.bot.items_db.get_item(item_id)
                    if item_data and item_data.get('category') == category:
                        filtered_inv[item_id] = quantity
            
            if not filtered_inv:
                await interaction.response.send_message(
                    f"🎒 No {category} items in your bag!",
                    ephemeral=True
                )
                return
            
            # Show filtered view
            embed = EmbedBuilder.bag_view(filtered_inv, self.bot.items_db, category)
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
        
        return callback


class TravelSelectView(View):
    """Location travel selection view"""
    
    def __init__(self, bot, all_locations: dict, current_location_id: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.all_locations = all_locations
        self.current_location_id = current_location_id
        
        # Create location dropdown
        options = []
        for location_id, location_data in all_locations.items():
            label = location_data.get('name', location_id.replace('_', ' ').title())
            
            # Mark current location
            is_current = (location_id == current_location_id)
            if is_current:
                label = f"📍 {label} (Current)"
            
            description = location_data.get('description', '')[:100]
            
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=location_id,
                    description=description,
                    default=is_current
                )
            )
        
        select = Select(
            placeholder="Choose a Lights District destination...",
            options=options,
            custom_id="location_select"
        )
        select.callback = self.location_callback
        self.add_item(select)
    
    async def location_callback(self, interaction: discord.Interaction):
        """Handle location selection"""
        new_location_id = interaction.data['values'][0]
        
        if new_location_id == self.current_location_id:
            await interaction.response.send_message(
                "❌ You're already at this location!",
                ephemeral=True
            )
            return
        
        # Update player's location
        self.bot.player_manager.update_player(
            interaction.user.id,
            current_location_id=new_location_id
        )
        
        location_name = self.bot.location_manager.get_location_name(new_location_id)
        
        await interaction.response.send_message(
            f"🧭 You traveled to **{location_name}**!",
            ephemeral=True
        )
        
        self.stop()


class EncounterSelectView(View):
    """Wild encounter selection from rolled encounters"""

    def __init__(self, bot, encounters: list, location: dict, player_id: int, location_id: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.encounters = encounters
        self.location = location
        self.player_id = player_id
        self.location_id = location_id

        self._persist_active_encounters()
        
        # Add encounter select dropdown
        options = []
        for i, pokemon in enumerate(encounters[:25], 1):  # Discord max 25 options
            types = "/".join([t.title() for t in pokemon.species_data['types']])
            label = f"#{i}: {pokemon.species_name} (Lv. {pokemon.level})"
            description = f"Type: {types}"
            
            # Add gender indicator
            if pokemon.gender:
                description += f" | {pokemon.gender.upper()}"
            
            # Add shiny indicator
            if pokemon.is_shiny:
                label = f"âœ¨ {label}"
                description = "SHINY! | " + description
            
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(i - 1),  # Index in encounters list
                    description=description[:100]
                )
            )
        
        select = Select(
            placeholder="Choose a Pokémon to battle...",
            options=options,
            custom_id="encounter_select"
        )
        select.callback = self.encounter_callback
        self.add_item(select)
        
        # Add reroll button
        reroll_button = Button(
            label="🔄 Reroll Encounters",
            style=discord.ButtonStyle.secondary,
            custom_id="reroll_button",
            row=1
        )
        reroll_button.callback = self.reroll_callback
        self.add_item(reroll_button)
    
    async def encounter_callback(self, interaction: discord.Interaction):
        """Handle encounter selection - start battle"""
        from battle_engine_v2 import BattleType

        encounter_index = int(interaction.data['values'][0])
        if encounter_index < 0 or encounter_index >= len(self.encounters):
            await interaction.response.send_message(
                "❌ That encounter is no longer available!",
                ephemeral=True
            )
            return

        wild_pokemon = self.encounters.pop(encounter_index)
        self._persist_active_encounters()
        
        # Check if already in battle
        battle_cog = self.bot.get_cog('BattleCog')
        if interaction.user.id in battle_cog.user_battles:
            await interaction.response.send_message(
                "❌ You're already in a battle! Finish it first!",
                ephemeral=True
            )
            return
        
        # Get trainer's party and reconstruct Pokemon objects
        trainer_party_data = self.bot.player_manager.get_party(interaction.user.id)
        trainer_pokemon = []
        for poke_data in trainer_party_data:
            species_data = self.bot.species_db.get_species(poke_data['species_dex_number'])
            pokemon = reconstruct_pokemon_from_data(poke_data, species_data)
            trainer_pokemon.append(pokemon)
        
        # Defer the response
        await interaction.response.defer()
        
        # Start battle using unified battle engine
        if not battle_cog:
            await interaction.followup.send(
                "❌ Battle system not loaded!",
                ephemeral=True
            )
            return
        
        battle_id = battle_cog.battle_engine.start_wild_battle(
            trainer_id=interaction.user.id,
            trainer_name=interaction.user.display_name,
            trainer_party=trainer_pokemon,
            wild_pokemon=wild_pokemon
        )
        
        # Start battle UI
        await battle_cog.start_battle_ui(
            interaction=interaction,
            battle_id=battle_id,
            battle_type=BattleType.WILD
        )
        
        self.stop()
    
    async def reroll_callback(self, interaction: discord.Interaction):
        """Reroll encounters"""
        from ui.embeds import EmbedBuilder
        
        # Get current location
        trainer = self.bot.player_manager.get_player(interaction.user.id)
        current_location_id = trainer.current_location_id
        
        # Roll new encounters
        await interaction.response.defer(ephemeral=True)
        
        new_encounters = self.bot.location_manager.roll_multiple_encounters(
            current_location_id,
            10,
            self.bot.species_db
        )
        
        if not new_encounters:
            await interaction.followup.send(
                "❌ Failed to generate encounters. Try again!",
                ephemeral=True
            )
            return
        
        # Update view with new encounters
        embed = EmbedBuilder.encounter_roll(new_encounters, self.location)
        new_view = EncounterSelectView(
            self.bot,
            new_encounters,
            self.location,
            interaction.user.id,
            current_location_id
        )
        
        await interaction.followup.send(
            embed=embed,
            view=new_view,
            ephemeral=True
        )
        
        self.stop()

    def _persist_active_encounters(self):
        """Store or clear the player's active encounter pool"""
        if not hasattr(self.bot, 'active_encounters'):
            self.bot.active_encounters = {}

        if self.encounters:
            self.bot.active_encounters[self.player_id] = {
                'location_id': self.location_id,
                'encounters': self.encounters
            }
        else:
            self.bot.active_encounters.pop(self.player_id, None)


class ReturnToEncounterView(View):
    """Single-button view that reopens the player's saved encounters"""

    def __init__(self, bot, player_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.player_id = player_id

    @discord.ui.button(label="↩️ Back to Encounters", style=discord.ButtonStyle.success)
    async def return_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "❌ This button isn't for you!",
                ephemeral=True
            )
            return

        active_data = getattr(self.bot, 'active_encounters', {}).get(self.player_id)
        if not active_data:
            await interaction.response.send_message(
                "⚠️ You don't have any saved encounters right now. Use the encounter button to roll a new batch!",
                ephemeral=True
            )
            return

        encounters = active_data.get('encounters') or []
        if not encounters:
            await interaction.response.send_message(
                "⚠️ You've battled every Pokémon from that batch! Roll for new encounters when you're ready.",
                ephemeral=True
            )
            # Clear stale reference just in case
            if hasattr(self.bot, 'active_encounters'):
                self.bot.active_encounters.pop(self.player_id, None)
            return

        location_id = active_data.get('location_id')
        location = None
        if location_id:
            location = self.bot.location_manager.get_location(location_id)

        if not location:
            await interaction.response.send_message(
                "⚠️ The location for those encounters is no longer available. Roll again to get a fresh batch!",
                ephemeral=True
            )
            if hasattr(self.bot, 'active_encounters'):
                self.bot.active_encounters.pop(self.player_id, None)
            return

        from ui.embeds import EmbedBuilder

        embed = EmbedBuilder.encounter_roll(encounters, location)
        view = EncounterSelectView(self.bot, encounters, location, self.player_id, location_id)

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )

        self.stop()
    


class BoxPokemonActionsView(View):
    """Actions for a single boxed Pokémon (e.g., add to party)."""

    def __init__(self, bot, pokemon_data: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.pokemon_data = pokemon_data

    @discord.ui.button(label="➕ Add to Party", style=discord.ButtonStyle.success, row=0)
    async def add_to_party(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Move this Pokémon from box to party."""
        # Use the PlayerManager's withdraw_pokemon helper, which already
        # handles all the ownership/party-size/position logic.
        success, message = self.bot.player_manager.withdraw_pokemon(
            interaction.user.id,
            str(self.pokemon_data.get('pokemon_id') or self.pokemon_data.get('id'))
        )

        if success:
            # Build a nicer success message with the Pokémon's name
            species_data = self.bot.species_db.get_species(self.pokemon_data['species_dex_number'])
            name = self.pokemon_data.get('nickname') or species_data.get('name', 'Pokémon')
            await interaction.response.send_message(
                f"✅ Moved **{name}** to your party!",
                ephemeral=True
            )
            self.stop()
        else:
            # Fall back to the manager's error message
            await interaction.response.send_message(
                message or "❌ Failed to move Pokémon. Try again!",
                ephemeral=True
            )

class BattleMenuView(View):
    """Battle menu with casual and ranked options"""

    def __init__(self, bot, location: dict):
        super().__init__(timeout=300)
        self.bot = bot
        self.location = location

    def _get_available_players(self, interaction: discord.Interaction):
        trainer = self.bot.player_manager.get_player(interaction.user.id)
        if not trainer:
            return None, None, None, [], "❌ You need a trainer profile before battling other players!"

        location_id = trainer.current_location_id
        location_name = self.bot.location_manager.get_location_name(location_id)

        available_trainers = self.bot.player_manager.get_players_in_location(
            location_id,
            exclude_user_id=interaction.user.id
        )

        battle_cog = self.bot.get_cog('BattleCog')
        busy_ids = set(battle_cog.user_battles.keys()) if battle_cog else set()
        available_trainers = [
            other for other in available_trainers
            if getattr(other, 'discord_user_id', None) not in busy_ids
        ]

        return trainer, location_id, location_name, available_trainers, None

    @discord.ui.button(label="🎮 Casual Battle", style=discord.ButtonStyle.primary, row=0)
    async def casual_button(self, interaction: discord.Interaction, button: Button):
        """Show casual PvP options"""
        from ui.embeds import EmbedBuilder

        _, location_id, location_name, available_trainers, error = self._get_available_players(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if not available_trainers:
            await interaction.response.send_message(
                "⚠️ No other trainers in this location are available for casual battles right now.",
                ephemeral=True
            )
            return

        view = PvPChallengeSetupView(
            bot=self.bot,
            challenger=interaction.user,
            opponents=available_trainers,
            location_id=location_id,
            location_name=location_name,
            guild=interaction.guild,
            is_ranked=False
        )

        embed = EmbedBuilder.pvp_challenge_menu(location_name, available_trainers, ranked=False)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🏆 Ranked Battle", style=discord.ButtonStyle.danger, row=0)
    async def ranked_button(self, interaction: discord.Interaction, button: Button):
        """Show ranked options (players + NPCs)"""
        from ui.embeds import EmbedBuilder

        _, location_id, location_name, available_trainers, error = self._get_available_players(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        ranked_npcs = self.location.get('ranked_npc_trainers', []) if self.location else []

        response_sent = False

        if available_trainers:
            player_view = PvPChallengeSetupView(
                bot=self.bot,
                challenger=interaction.user,
                opponents=available_trainers,
                location_id=location_id,
                location_name=location_name,
                guild=interaction.guild,
                is_ranked=True
            )
            player_embed = EmbedBuilder.pvp_challenge_menu(location_name, available_trainers, ranked=True)
            await interaction.response.send_message(embed=player_embed, view=player_view, ephemeral=True)
            response_sent = True

        if ranked_npcs:
            npc_view = NpcTrainerSelectView(self.bot, ranked_npcs, self.location, ranked=True)
            npc_embed = EmbedBuilder.npc_trainer_list(ranked_npcs, self.location, ranked=True)
            if response_sent:
                await interaction.followup.send(embed=npc_embed, view=npc_view, ephemeral=True)
            else:
                await interaction.response.send_message(embed=npc_embed, view=npc_view, ephemeral=True)
                response_sent = True

        if not response_sent:
            await interaction.response.send_message(
                "⚠️ No ranked challengers are available at this location right now.",
                ephemeral=True
            )


class PvPChallengeSetupView(View):
    """Configure and send a PvP challenge"""

    def __init__(
        self,
        bot,
        challenger: discord.Member,
        opponents: List,
        location_id: str,
        location_name: str,
        guild: Optional[discord.Guild],
        is_ranked: bool = False
    ):
        super().__init__(timeout=300)
        self.bot = bot
        self.challenger = challenger
        self.location_id = location_id
        self.location_name = location_name
        self.guild = guild
        self.is_ranked = is_ranked
        self.visible_opponents = opponents[:25]
        self.selected_opponent_id: Optional[int] = None
        self.selected_format = BattleFormat.SINGLES if BattleFormat else None
        self.team_size = 1

        opponent_options = []
        for trainer in self.visible_opponents:
            trainer_name = getattr(trainer, 'trainer_name', 'Trainer')
            discord_id = getattr(trainer, 'discord_user_id', 0)
            description = f"ID: {discord_id}"
            opponent_options.append(
                discord.SelectOption(
                    label=trainer_name[:100],
                    description=description[:100],
                    value=str(discord_id)
                )
            )

        opponent_select = Select(
            placeholder="Choose a trainer to challenge...",
            options=opponent_options,
            min_values=1,
            max_values=1,
            custom_id="pvp_opponent_select"
        )
        opponent_select.callback = self.opponent_callback
        self.add_item(opponent_select)

        format_options = [
            discord.SelectOption(label="Singles", value="singles", description="1 active Pokémon"),
            discord.SelectOption(label="Doubles", value="doubles", description="2 active Pokémon")
        ]
        format_select = Select(
            placeholder="Choose battle format",
            options=format_options,
            min_values=1,
            max_values=1,
            custom_id="pvp_format_select"
        )
        format_select.callback = self.format_callback
        self.add_item(format_select)

        size_options = [
            discord.SelectOption(label=f"{i} Pokémon", value=str(i))
            for i in range(1, 7)
        ]
        size_select = Select(
            placeholder="How many Pokémon per trainer?",
            options=size_options,
            min_values=1,
            max_values=1,
            custom_id="pvp_size_select"
        )
        size_select.callback = self.size_callback
        self.add_item(size_select)

        button_label = "Send Ranked Challenge" if self.is_ranked else "Send Challenge"
        send_button = Button(
            label=button_label,
            style=discord.ButtonStyle.success,
            custom_id="pvp_send_challenge"
        )
        send_button.callback = self.send_challenge
        self.add_item(send_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.challenger.id:
            await interaction.response.send_message(
                "❌ Only the challenger can use this menu.",
                ephemeral=True
            )
            return False
        return True

    async def opponent_callback(self, interaction: discord.Interaction):
        value = interaction.data.get('values', [None])[0]
        self.selected_opponent_id = int(value) if value else None
        await interaction.response.defer()

    async def format_callback(self, interaction: discord.Interaction):
        value = interaction.data.get('values', [None])[0]
        if BattleFormat and value:
            try:
                self.selected_format = BattleFormat(value)
            except ValueError:
                self.selected_format = BattleFormat.SINGLES
        await interaction.response.defer()

    async def size_callback(self, interaction: discord.Interaction):
        value = interaction.data.get('values', [None])[0]
        if value:
            self.team_size = max(1, min(6, int(value)))
        await interaction.response.defer()

    def _format_label(self) -> str:
        if not self.selected_format or not BattleFormat:
            return "Singles"
        return "Singles" if self.selected_format == BattleFormat.SINGLES else "Doubles"

    async def send_challenge(self, interaction: discord.Interaction):
        if self.selected_opponent_id is None:
            await interaction.response.send_message(
                "Select a trainer to challenge first!",
                ephemeral=True
            )
            return

        team_size = max(1, min(6, self.team_size))
        if BattleFormat and self.selected_format == BattleFormat.DOUBLES and team_size < 2:
            await interaction.response.send_message(
                "Doubles battles require at least 2 Pokémon per trainer.",
                ephemeral=True
            )
            return

        battle_cog = self.bot.get_cog('BattleCog')
        if not battle_cog:
            await interaction.response.send_message(
                "❌ Battle system not available right now.",
                ephemeral=True
            )
            return

        challenger_trainer = self.bot.player_manager.get_player(self.challenger.id)
        opponent_trainer = self.bot.player_manager.get_player(self.selected_opponent_id)
        if not challenger_trainer or not opponent_trainer:
            await interaction.response.send_message(
                "❌ Could not load trainer data for this challenge.",
                ephemeral=True
            )
            return

        if challenger_trainer.current_location_id != self.location_id:
            await interaction.response.send_message(
                "⚠️ Travel back to the location before issuing a challenge.",
                ephemeral=True
            )
            return

        if opponent_trainer.current_location_id != self.location_id:
            await interaction.response.send_message(
                "⚠️ That trainer has moved to another location.",
                ephemeral=True
            )
            return

        busy_ids = set(battle_cog.user_battles.keys())
        if self.challenger.id in busy_ids or self.selected_opponent_id in busy_ids:
            await interaction.response.send_message(
                "⚠️ One of the trainers is already in a battle.",
                ephemeral=True
            )
            return

        challenger_party = self.bot.player_manager.get_party(self.challenger.id)
        opponent_party = self.bot.player_manager.get_party(self.selected_opponent_id)

        challenger_ready = sum(1 for mon in challenger_party if mon.get('current_hp', 0) > 0)
        opponent_ready = sum(1 for mon in opponent_party if mon.get('current_hp', 0) > 0)

        if challenger_ready < team_size:
            await interaction.response.send_message(
                f"❌ You only have {challenger_ready} healthy Pokémon. Heal up first!",
                ephemeral=True
            )
            return

        if opponent_ready < team_size:
            await interaction.response.send_message(
                "⚠️ That trainer doesn't have enough healthy Pokémon for this format.",
                ephemeral=True
            )
            return

        if not interaction.channel:
            await interaction.response.send_message(
                "❌ This channel is unavailable for sending the challenge.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        opponent_member = None
        if self.guild:
            opponent_member = self.guild.get_member(self.selected_opponent_id)
        opponent_mention = opponent_member.mention if opponent_member else f"<@{self.selected_opponent_id}>"
        challenger_mention = self.challenger.mention

        if self.is_ranked:
            title = "🏆 Ranked PvP Challenge"
            color = discord.Color.gold()
            description = (
                f"{challenger_mention} has issued a ranked challenge to {opponent_mention}!\n"
                f"Location: **{self.location_name}**"
            )
            footer_text = "Ranked victories grant Challenger points."
        else:
            title = "⚔️ PvP Challenge"
            color = discord.Color.red()
            description = (
                f"{challenger_mention} has challenged {opponent_mention}!\n"
                f"Location: **{self.location_name}**"
            )
            footer_text = "Only the challenged trainer can accept."

        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        embed.add_field(
            name="Format",
            value=f"{self._format_label()} — {team_size} Pokémon per trainer",
            inline=False
        )
        embed.set_footer(text=footer_text)

        response_view = PvPChallengeResponseView(
            bot=self.bot,
            challenger_id=self.challenger.id,
            opponent_id=self.selected_opponent_id,
            battle_format=self.selected_format or (BattleFormat.SINGLES if BattleFormat else None),
            team_size=team_size,
            location_id=self.location_id,
            location_name=self.location_name,
            challenger_name=getattr(challenger_trainer, 'trainer_name', self.challenger.display_name),
            opponent_name=getattr(opponent_trainer, 'trainer_name', opponent_member.display_name if opponent_member else 'Trainer'),
            is_ranked=self.is_ranked
        )

        message = await interaction.channel.send(
            content=f"{opponent_mention}, {challenger_mention} wants to battle!",
            embed=embed,
            view=response_view
        )
        response_view.message = message

        await interaction.followup.send("Challenge sent! Waiting for them to respond...", ephemeral=True)
        self.stop()


class PvPChallengeResponseView(View):
    """Handles accept/decline of a PvP challenge"""

    def __init__(
        self,
        bot,
        challenger_id: int,
        opponent_id: int,
        battle_format,
        team_size: int,
        location_id: str,
        location_name: str,
        challenger_name: str,
        opponent_name: str,
        is_ranked: bool = False
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id
        self.battle_format = battle_format
        self.team_size = team_size
        self.location_id = location_id
        self.location_name = location_name
        self.challenger_name = challenger_name
        self.opponent_name = opponent_name
        self.message: Optional[discord.Message] = None
        self.is_ranked = is_ranked

    def _format_label(self) -> str:
        if not BattleFormat or not self.battle_format:
            return "Singles"
        return "Singles" if self.battle_format == BattleFormat.SINGLES else "Doubles"

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="Challenge expired due to no response.", view=self)
            except Exception:
                pass

    async def _finalize(self, text: str):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content=text, view=self)
            except Exception:
                pass

    def _build_party(self, user_id: int) -> tuple[Optional[List], Optional[str]]:
        party_rows = self.bot.player_manager.get_party(user_id)
        healthy = [row for row in party_rows if row.get('current_hp', 0) > 0]
        if len(healthy) < self.team_size:
            return None, f"Only {len(healthy)} Pokémon are battle-ready."

        party = []
        for poke_data in healthy[:self.team_size]:
            species = self.bot.species_db.get_species(poke_data['species_dex_number'])
            party.append(reconstruct_pokemon_from_data(poke_data, species))
        return party, None

    async def _start_battle(self, interaction: discord.Interaction) -> Optional[str]:
        battle_cog = self.bot.get_cog('BattleCog')
        if not battle_cog:
            return "Battle system is unavailable."

        challenger = self.bot.player_manager.get_player(self.challenger_id)
        opponent = self.bot.player_manager.get_player(self.opponent_id)
        if not challenger or not opponent:
            return "Unable to load trainer data for this battle."

        if challenger.current_location_id != self.location_id or opponent.current_location_id != self.location_id:
            return "Both trainers must be in the same location to battle."

        busy_ids = set(battle_cog.user_battles.keys())
        if self.challenger_id in busy_ids or self.opponent_id in busy_ids:
            return "One of the trainers is already battling."

        challenger_party, error = self._build_party(self.challenger_id)
        if error:
            return f"{self.challenger_name} can't battle right now: {error}"

        opponent_party, error = self._build_party(self.opponent_id)
        if error:
            return f"{self.opponent_name} can't battle right now: {error}"

        fmt = self.battle_format or (BattleFormat.SINGLES if BattleFormat else None)
        if fmt is None:
            return "Battle format is unavailable."

        ranked_context = None
        if self.is_ranked:
            ranked_context = {
                'mode': 'pvp',
                'players': [self.challenger_id, self.opponent_id]
            }

        battle_id = battle_cog.battle_engine.start_pvp_battle(
            trainer1_id=self.challenger_id,
            trainer1_name=self.challenger_name,
            trainer1_party=challenger_party,
            trainer2_id=self.opponent_id,
            trainer2_name=self.opponent_name,
            trainer2_party=opponent_party,
            battle_format=fmt,
            is_ranked=self.is_ranked,
            ranked_context=ranked_context
        )

        battle_cog.user_battles[self.challenger_id] = battle_id
        battle_cog.user_battles[self.opponent_id] = battle_id

        await battle_cog.start_battle_ui(
            interaction=interaction,
            battle_id=battle_id,
            battle_type=BattleType.PVP
        )
        return None

    @discord.ui.button(label="Accept Challenge", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("Only the challenged trainer can accept!", ephemeral=True)
            return

        await interaction.response.defer()
        error = await self._start_battle(interaction)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return

        await self._finalize("✅ Challenge accepted! The battle is starting…")
        self.stop()

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("Only the challenged trainer can decline!", ephemeral=True)
            return

        await interaction.response.send_message("You declined the challenge.", ephemeral=True)
        await self._finalize("❌ Challenge declined.")
        self.stop()

    @discord.ui.button(label="Cancel Challenge", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.challenger_id:
            await interaction.response.send_message("Only the challenger can cancel this request.", ephemeral=True)
            return

        await interaction.response.send_message("Challenge cancelled.", ephemeral=True)
        await self._finalize("❌ Challenge cancelled by the challenger.")
        self.stop()


class NpcTrainerSelectView(View):
    """Select an NPC trainer to battle"""

    def __init__(self, bot, npc_trainers: list, location: dict, ranked: bool = False):
        super().__init__(timeout=300)
        self.bot = bot
        self.npc_trainers = npc_trainers
        self.location = location
        self.ranked = ranked

        # Add NPC select dropdown
        options = []
        for i, npc in enumerate(npc_trainers[:25], 1):  # Discord max 25 options
            npc_name = npc.get('name', 'Unknown Trainer')
            npc_class = npc.get('class', 'Trainer')
            party_size = len(npc.get('party', []))
            prize_money = npc.get('prize_money', 0)
            
            label = npc_name
            description = npc_class
            
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(i - 1),  # Index in npc_trainers list
                    description=description[:100]
                )
            )
        
        placeholder = "Choose a ranked trainer to battle..." if ranked else "Choose a trainer to battle..."
        custom_id = "ranked_npc_select" if ranked else "npc_select"
        select = Select(
            placeholder=placeholder,
            options=options,
            custom_id=custom_id
        )
        select.callback = self.npc_callback
        self.add_item(select)
    
    async def npc_callback(self, interaction: discord.Interaction):
        """Handle NPC selection - start trainer battle"""
        from battle_engine_v2 import BattleType
        
        npc_index = int(interaction.data['values'][0])
        npc_data = self.npc_trainers[npc_index]
        
        # Check if already in battle
        battle_cog = self.bot.get_cog('BattleCog')
        if interaction.user.id in battle_cog.user_battles:
            await interaction.response.send_message(
                "❌ You're already in a battle! Finish it first!",
                ephemeral=True
            )
            return
        
        # Get trainer's party and reconstruct Pokemon objects
        trainer_party_data = self.bot.player_manager.get_party(interaction.user.id)
        trainer_pokemon = []
        for poke_data in trainer_party_data:
            species_data = self.bot.species_db.get_species(poke_data['species_dex_number'])
            pokemon = reconstruct_pokemon_from_data(poke_data, species_data)
            trainer_pokemon.append(pokemon)
        
        # Build NPC's party
        npc_pokemon = []
        for npc_poke in npc_data.get('party', []):
            pokemon = self._create_npc_pokemon(npc_poke)
            npc_pokemon.append(pokemon)
        
        # Defer the response
        await interaction.response.defer()
        
        # Start battle using unified battle engine
        if not battle_cog:
            await interaction.followup.send(
                "❌ Battle system not loaded!",
                ephemeral=True
            )
            return
        
        ranked_context = self._build_ranked_context(npc_data)

        battle_id = battle_cog.battle_engine.start_trainer_battle(
            trainer_id=interaction.user.id,
            trainer_name=interaction.user.display_name,
            trainer_party=trainer_pokemon,
            npc_party=npc_pokemon,
            npc_name=npc_data.get('name', 'Trainer'),
            npc_class=npc_data.get('class', 'Trainer'),
            prize_money=npc_data.get('prize_money', 0),
            is_ranked=self.ranked,
            ranked_context=ranked_context
        )
        
        # Register battle
        battle_cog.user_battles[interaction.user.id] = battle_id
        
        # Start battle UI
        await battle_cog.start_battle_ui(
            interaction=interaction,
            battle_id=battle_id,
            battle_type=BattleType.TRAINER
        )

        self.stop()

    def _build_ranked_context(self, npc_data: dict) -> Optional[Dict[str, Any]]:
        if not self.ranked:
            return None
        npc_rank = npc_data.get('rank_tier_number') or npc_data.get('rank') or 1
        return {
            'mode': 'npc',
            'npc_rank': npc_rank,
            'npc_name': npc_data.get('name'),
            'npc_class': npc_data.get('class')
        }

    def _create_npc_pokemon(self, npc_poke_data: dict):
        """Create a Pokemon object from NPC trainer data"""
        from models import Pokemon
        import random
        
        # Get species data
        species_dex_number = npc_poke_data.get('species_dex_number')
        species_data = self.bot.species_db.get_species(species_dex_number)
        
        # Get level
        level = npc_poke_data.get('level', 5)
        
        # Get moves (or auto-generate from level)
        moves = npc_poke_data.get('moves', [])
        
        # Generate random IVs for NPC (slightly lower than perfect)
        ivs = {
            'hp': random.randint(20, 31),
            'attack': random.randint(20, 31),
            'defense': random.randint(20, 31),
            'sp_attack': random.randint(20, 31),
            'sp_defense': random.randint(20, 31),
            'speed': random.randint(20, 31)
        }
        
        # Create the Pokemon
        pokemon = Pokemon(
            species_data=species_data,
            level=level,
            owner_discord_id=-1,  # NPC trainer
            nature=npc_poke_data.get('nature') or random.choice(['hardy', 'docile', 'serious', 'bashful', 'quirky']),
            ability=npc_poke_data.get('ability') or species_data.get('abilities', {}).get('primary'),
            moves=moves if moves else None,  # None will auto-generate
            ivs=ivs,
            is_shiny=npc_poke_data.get('is_shiny', False)
        )
        
        # Set gender if specified
        if 'gender' in npc_poke_data:
            pokemon.gender = npc_poke_data['gender']
        
        # Set held item if specified
        if 'held_item' in npc_poke_data:
            pokemon.held_item = npc_poke_data['held_item']
        
        return pokemon
    