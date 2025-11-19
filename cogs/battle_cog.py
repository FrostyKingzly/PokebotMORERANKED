import discord
from discord.ext import commands
from pathlib import Path
from typing import Optional

from battle_engine_v2 import BattleEngine, BattleType, BattleAction, BattleFormat, HeldItemManager
from battle_exp_integration import BattleExpHandler
from capture import simulate_throw, guaranteed_capture
from learnset_database import LearnsetDatabase
# Emoji placeholders (fallbacks if ui.emoji is missing)
try:
    from ui.emoji import SWORD, FIELD, EVENTS, YOU, FOE
except Exception:
    SWORD = "⚔️"; FIELD = "🌦️"; EVENTS = "📋"; YOU = "👉"; FOE = "🎯"

try:
    from version import BUILD_TAG
except Exception:
    BUILD_TAG = "dev"

class BattleCog(commands.Cog):
    """Handles battle UI and flow."""
    def __init__(self, bot: commands.Bot, battle_engine: BattleEngine):
        self.bot = bot
        self.battle_engine = battle_engine
        # Tracks active battle per user id (int -> str battle_id)
        self.user_battles = {}
        self.exp_handler = self._init_exp_handler()

    def _init_exp_handler(self) -> Optional[BattleExpHandler]:
        species_db = getattr(self.bot, "species_db", None)
        player_manager = getattr(self.bot, "player_manager", None)
        if not species_db or not player_manager:
            return None

        learnset_db = None
        learnset_path = Path("data/learnsets.json")
        if learnset_path.exists():
            try:
                learnset_db = LearnsetDatabase(str(learnset_path))
            except Exception:
                learnset_db = None

        try:
            return BattleExpHandler(species_db, learnset_db, player_manager)
        except Exception:
            return None

    def _unregister_battle(self, battle):
        """Remove all user tracking entries for a finished battle."""
        if not battle:
            return
        self.user_battles.pop(getattr(battle.trainer, 'battler_id', None), None)
        if getattr(battle, 'battle_type', None) == BattleType.PVP:
            self.user_battles.pop(getattr(battle.opponent, 'battler_id', None), None)

    def _get_ball_inventory(self, discord_user_id: int):
        """Return a dict of {item_id: (item_data, quantity)} for Poké Balls.

        Uses ItemsDatabase (self.bot.items_db) and the player's inventory rows.
        """
        items_db = getattr(self.bot, "items_db", None)
        if not items_db:
            return {}
        pm = self.bot.player_manager
        inventory_rows = pm.get_inventory(discord_user_id)
        balls = {}
        for row in inventory_rows:
            item_id = row.get("item_id")
            qty = row.get("quantity", 0)
            if qty <= 0:
                continue
            # Look up item data via ItemsDatabase
            item_data = items_db.get_item(item_id)
            if not item_data:
                continue
            if item_data.get("category") == "pokeball":
                balls[item_id] = (item_data, qty)
        return balls

    def _consume_ball(self, discord_user_id: int, item_id: str) -> bool:
        """Remove one ball from inventory if possible."""
        pm = self.bot.player_manager
        return pm.remove_item(discord_user_id, item_id, quantity=1)

    async def _send_dazed_prompt(self, interaction: discord.Interaction, battle):
        """Send 'Will you catch it?' prompt when wild Pokémon is dazed."""
        opponent_mon = battle.opponent.get_active_pokemon()[0]
        embed = discord.Embed(
            title=f"😵 The wild {opponent_mon.species_name} is dazed!",
            description="**Will you catch it?**",
            color=discord.Color.gold()
        )

        view = DazedCatchView(self, battle.battle_id)
        await interaction.followup.send(embed=embed, view=view)

    async def _handle_ball_throw(self, interaction: discord.Interaction, battle_id: str, item_id: str, guaranteed: bool = False):
        """Core capture logic used by the dazed 'Yes' flow, and for in-battle Bag throws."""
        battle = self.battle_engine.get_battle(battle_id)

        async def send_msg(*args, **kwargs):
            """Safe send helper: uses response.send_message first, then followups."""
            if not interaction.response.is_done():
                await interaction.response.send_message(*args, **kwargs)
            else:
                await interaction.followup.send(*args, **kwargs)

        if not battle or battle.battle_type != BattleType.WILD:
            await send_msg("❌ You can only use Poké Balls in wild battles.", ephemeral=True)
            return

        wild_mon = battle.opponent.get_active_pokemon()[0]
        balls = self._get_ball_inventory(interaction.user.id)
        if item_id not in balls:
            await send_msg("❌ You don't have that kind of Poké Ball.", ephemeral=True)
            return

        item_data, _qty = balls[item_id]

        # Consume the ball up front
        if not self._consume_ball(interaction.user.id, item_id):
            await send_msg("❌ You don't have that Poké Ball anymore.", ephemeral=True)
            return

        # Determine ball bonus: use item's catch_rate_modifier as base
        ball_bonus = float(item_data.get("catch_rate_modifier", 1.0))
        # Treat Master Ball-like behaviour as guaranteed
        if ball_bonus >= 255.0:
            guaranteed = True

        if guaranteed:
            result = guaranteed_capture()
            caught = True
            shakes = result["shakes"]
        else:
            # Use modern style formula
            species_rate = int(wild_mon.species_data.get("catch_rate", 45))
            max_hp = int(getattr(wild_mon, "max_hp", 1))
            cur_hp = int(max(0, getattr(wild_mon, "current_hp", 0)))
            status = getattr(wild_mon, "major_status", None)
            result = simulate_throw(max_hp, cur_hp, species_rate, ball_bonus, status=status)
            caught = result["caught"]
            shakes = result["shakes"]

        if caught:
            # Add Pokemon to trainer and end battle
            pm = self.bot.player_manager
            wild_mon.owner_discord_id = interaction.user.id

            # Decide whether it goes to party or box
            party = pm.get_party(interaction.user.id)
            if len(party) >= 6:
                pm.add_pokemon_to_box(wild_mon)
                location_text = "It was sent to your storage box."
            else:
                pm.add_pokemon_to_party(wild_mon)
                location_text = "It was added to your party."

            # Mark battle over
            battle.is_over = True
            battle.winner = "trainer"

            embed = discord.Embed(
                title=f"🎉 Gotcha! {wild_mon.species_name} was caught!",
                description=f"You used **{item_data.get('name', item_id)}**.\n{location_text}",
                color=discord.Color.green()
            )
            await send_msg(embed=embed)
            await self.send_return_to_encounter_prompt(interaction, interaction.user.id)
            return
        else:
            msg = f"The {item_data.get('name', item_id)} shook {shakes} time(s), but the Pokémon broke free!"
            embed = discord.Embed(
                title="...Almost had it!",
                description=msg,
                color=discord.Color.orange()
            )
            await send_msg(embed=embed)
            # Note: throwing a ball consumes the turn externally; the turn resolution
            # for the wild Pokémon will still happen via the normal battle engine.

    async def send_return_to_encounter_prompt(self, interaction: discord.Interaction, discord_user_id: int):
        """Send a button that lets the trainer reopen their remaining encounter pool"""
        active_sets = getattr(self.bot, 'active_encounters', None)
        if not active_sets:
            return

        data = active_sets.get(discord_user_id)
        if not data:
            return

        encounters = data.get('encounters') or []
        location_id = data.get('location_id')
        if not encounters or not location_id:
            return

        try:
            from ui.buttons import ReturnToEncounterView
        except Exception:
            return

        message = "↩️ Continue exploring the remaining encounters from your last roll."
        view = ReturnToEncounterView(self.bot, discord_user_id)

        send_kwargs = {
            'content': message,
            'view': view,
            'ephemeral': True
        }

        try:
            if interaction.response.is_done():
                await interaction.followup.send(**send_kwargs)
            else:
                await interaction.response.send_message(**send_kwargs)
        except Exception:
            pass

    async def start_battle_ui(
        self,
        interaction: discord.Interaction,
        battle_id: str,
        battle_type: BattleType
    ):
        """Start the multi-embed battle intro safely from a Select callback."""
        battle = self.battle_engine.get_battle(battle_id)
        if not battle:
            if not interaction.response.is_done():
                await interaction.response.send_message("Battle not found!", ephemeral=True)
            else:
                await interaction.followup.send("Battle not found!", ephemeral=True)
            return

        # Make sure we can send multiple messages from a select interaction
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

        trainer_mon = battle.trainer.get_active_pokemon()[0]
        opponent_mon = battle.opponent.get_active_pokemon()[0]

        battle_mode = battle_type or battle.battle_type

        # 1) Opening embed: differentiate wild encounters vs trainer battles
        if battle_mode == BattleType.WILD:
            enc_title = f"{SWORD} Encounter!"
            enc_description = f"You encountered a wild **{opponent_mon.species_name}**!"
        else:
            enc_title = f"{SWORD} Battle Start!"
            enc_description = (
                f"**{battle.trainer.battler_name}** challenges "
                f"**{battle.opponent.battler_name}** to a battle!"
            )

        enc = discord.Embed(
            title=enc_title,
            description=enc_description,
            color=discord.Color.blue()
        )
        enc.set_footer(text=f"Build: {BUILD_TAG}")
        await interaction.followup.send(embed=enc)

        # 2) Send-out + entry effects
        sendout_lines = []

        if battle_mode == BattleType.WILD:
            sendout_lines.append(
                f"**{battle.trainer.battler_name}** sent out **{trainer_mon.species_name}**!"
            )
        else:
            sendout_lines.append(
                f"**{battle.trainer.battler_name}** sent out **{trainer_mon.species_name}**!"
            )
            sendout_lines.append(
                f"**{battle.opponent.battler_name}** sent out **{opponent_mon.species_name}**!"
            )

        for msg in (getattr(battle, "entry_messages", []) or []):
            sendout_lines.append(f"• {msg}")

        send_embed = discord.Embed(
            title="Send-out",
            description="\n".join(sendout_lines),
            color=discord.Color.blurple()
        )

        fields = []
        if getattr(battle, "weather", None):
            wt = getattr(battle, "weather_turns", None)
            fields.append(f"Weather: **{battle.weather.title()}**" + (f" ({wt} turns)" if wt else ""))
        if getattr(battle, "terrain", None):
            tt = getattr(battle, "terrain_turns", None)
            fields.append(f"Terrain: **{battle.terrain.title()}**" + (f" ({tt} turns)" if tt else ""))
        if fields:
            send_embed.add_field(name=f"{FIELD} Field Effects", value="\n".join(fields), inline=False)

        await interaction.followup.send(embed=send_embed)

        # 3) Main action embed + view
        main_embed = self._create_battle_embed(battle)
        view = self._create_battle_view(battle)
        await interaction.followup.send(embed=main_embed, view=view)

    # --------------------
    # Helpers
    # --------------------
    def _hp_bar(self, mon) -> str:
        try:
            filled = int(round(10 * max(0, mon.current_hp) / max(1, mon.max_hp)))
        except Exception:
            filled = 0
        return ("🟩" * filled) + ("⬜" * (10 - filled))

    def _held_item_text(self, mon) -> Optional[str]:
        item_id = getattr(mon, 'held_item', None)
        if not item_id:
            return None
        return item_id.replace('_', ' ').title()

    def _create_battle_embed(self, battle) -> discord.Embed:
        # Minimal, safe fallback embed; keep your richer implementation if present in your project
        trainer_pokemon = battle.trainer.get_active_pokemon()[0]
        opponent_pokemon = battle.opponent.get_active_pokemon()[0]

        e = discord.Embed(
            title=f"{SWORD} Battle",
            description=f"**Turn {battle.turn_number}**",
            color=discord.Color.dark_grey()
        )
        trainer_value = f"HP: {self._hp_bar(trainer_pokemon)} ({max(0, trainer_pokemon.current_hp)}/{trainer_pokemon.max_hp})"
        held = self._held_item_text(trainer_pokemon)
        if held:
            trainer_value += f"\n🎁 Item: {held}"
        e.add_field(
            name=f"{trainer_pokemon.species_name}",
            value=trainer_value,
            inline=False
        )
        # Changed: Show opponent HP instead of ???
        opponent_value = f"HP: {self._hp_bar(opponent_pokemon)} ({max(0, opponent_pokemon.current_hp)}/{opponent_pokemon.max_hp})"
        foe_item = self._held_item_text(opponent_pokemon)
        if foe_item:
            opponent_value += f"\n🎁 Item: {foe_item}"
        e.add_field(
            name=f"{opponent_pokemon.species_name}",
            value=opponent_value,
            inline=False
        )
        if getattr(battle, "recent_events", None):
            e.add_field(name=f"{EVENTS} Recent Events", value="\n".join(battle.recent_events[-5:]), inline=False)
        if getattr(battle, "weather", None) or getattr(battle, "terrain", None):
            lines = []
            if getattr(battle, "weather", None):
                weather_turns = getattr(battle, "weather_turns", 0)
                turns_text = f" ({weather_turns} turns left)" if weather_turns > 0 else ""
                lines.append(f"Weather: **{battle.weather.title()}**{turns_text}")
            if getattr(battle, "terrain", None):
                terrain_turns = getattr(battle, "terrain_turns", 0)
                turns_text = f" ({terrain_turns} turns left)" if terrain_turns > 0 else ""
                lines.append(f"Terrain: **{battle.terrain.title()}**{turns_text}")
            e.add_field(name=f"{FIELD} Field Effects", value="\n".join(lines), inline=False)
        e.set_footer(text=f"Build: {BUILD_TAG}")
        return e

    def _create_battle_view(self, battle) -> discord.ui.View:
        return BattleActionView(battle.battle_id, battle.trainer.battler_id, self.battle_engine, battle, self)

    def _build_turn_embed(self, messages: list[str]) -> discord.Embed:
        if messages:
            spaced = []
            for msg in messages[-6:]:
                spaced.append(msg)
                spaced.append("")
            if spaced and spaced[-1] == "":
                spaced.pop()
            desc = "\n".join(spaced)
        else:
            desc = "The turn resolves."
        return discord.Embed(title="Turn Result", description=desc, color=discord.Color.orange())

    def _build_switch_embed(self, messages: list[str], title: str = "Switch", color: Optional[discord.Color] = None):
        if not messages:
            return None
        embed_color = color or (discord.Color.blurple() if title == "Send-out" else discord.Color.teal())
        return discord.Embed(title=title, description="\n".join(messages), color=embed_color)

    async def _send_turn_resolution(self, interaction: discord.Interaction, turn_result: dict):
        switch_msgs = [msg for msg in (turn_result.get('switch_messages') or []) if msg]
        switch_embed = self._build_switch_embed(switch_msgs)
        if switch_embed:
            await interaction.followup.send(embed=switch_embed)

        turn_msgs = turn_result.get("narration", []) or turn_result.get("messages", [])
        await interaction.followup.send(embed=self._build_turn_embed(turn_msgs))

    async def _prompt_forced_switch(self, interaction: discord.Interaction, battle, battler_id: int):
        if battler_id != battle.trainer.battler_id:
            await interaction.followup.send(
                "Waiting for your opponent to choose their next Pokémon...",
                ephemeral=True
            )
            return

        fainted = battle.trainer.get_active_pokemon()[0] if battle.trainer.get_active_pokemon() else None
        if fainted:
            desc = (
                f"**{fainted.species_name}** can no longer fight!\n\n"
                "Select another healthy Pokémon to continue the battle."
            )
        else:
            desc = "Select another healthy Pokémon to continue the battle."

        embed = discord.Embed(title="Pokémon Fainted!", description=desc, color=discord.Color.red())
        await interaction.followup.send(
            embed=embed,
            view=PartySelectView(battle, battler_id, self.battle_engine, forced=True)
        )

    async def _finish_battle(self, interaction: discord.Interaction, battle):
        trainer_name = getattr(battle.trainer, 'battler_name', 'Trainer')
        opponent_name = getattr(battle.opponent, 'battler_name', 'Opponent')
        result = battle.winner
        if result == 'trainer':
            winner_name, loser_name = trainer_name, opponent_name
        elif result == 'opponent':
            winner_name, loser_name = opponent_name, trainer_name
        else:
            desc = "🏆 Battle Over\n\nIt's a draw!"
            await interaction.followup.send(
                embed=discord.Embed(title='Battle Over', description=desc, color=discord.Color.gold())
            )
            self.battle_engine.end_battle(battle.battle_id)
            self._unregister_battle(battle)
            return

        try:
            from database import PlayerDatabase
            pdb = PlayerDatabase('data/players.db')
            party_rows = pdb.get_trainer_party(battle.trainer.battler_id)
            rows_by_pos = {row.get('party_position', i): row for i, row in enumerate(party_rows)}
            for i, mon in enumerate(battle.trainer.party):
                row = rows_by_pos.get(i) or rows_by_pos.get(getattr(mon, 'party_position', i))
                if row and 'pokemon_id' in row:
                    pdb.update_pokemon(row['pokemon_id'], {'current_hp': max(0, int(getattr(mon, 'current_hp', 0)))})
        except Exception:
            pass

        desc = f"🏆 Battle Over\n\nAll of {loser_name}'s Pokémon have fainted! {winner_name} wins!"
        await interaction.followup.send(
            embed=discord.Embed(title='Battle Over', description=desc, color=discord.Color.gold())
        )

        exp_embed = None
        if result == 'trainer':
            exp_embed = await self._create_exp_embed(battle, interaction)
        if exp_embed:
            await interaction.followup.send(embed=exp_embed)

        ranked_embed = self._build_ranked_result_embed(battle)
        if ranked_embed:
            await interaction.followup.send(embed=ranked_embed)

        self.battle_engine.end_battle(battle.battle_id)
        self._unregister_battle(battle)

        if getattr(battle, 'battle_type', None) == BattleType.WILD:
            await self.send_return_to_encounter_prompt(interaction, battle.trainer.battler_id)

    async def _create_exp_embed(self, battle, interaction: Optional[discord.Interaction] = None) -> Optional[discord.Embed]:
        if not self.exp_handler:
            return None

        trainer = getattr(battle, 'trainer', None)
        opponent = getattr(battle, 'opponent', None)
        if not trainer or not getattr(trainer, 'party', None):
            return None

        active_index = 0
        if getattr(trainer, 'active_positions', None):
            try:
                active_index = int(trainer.active_positions[0])
            except (TypeError, ValueError, IndexError):
                active_index = 0

        defeated_pokemon = None
        opponent_party = getattr(opponent, 'party', None) if opponent else None
        if opponent_party:
            active_positions = getattr(opponent, 'active_positions', None) or []
            if active_positions:
                try:
                    opp_active_index = int(active_positions[0])
                except (TypeError, ValueError, IndexError):
                    opp_active_index = 0
            else:
                opp_active_index = 0

            if 0 <= opp_active_index < len(opponent_party):
                defeated_pokemon = opponent_party[opp_active_index]

            if defeated_pokemon is None:
                for mon in reversed(opponent_party):
                    if getattr(mon, 'current_hp', 1) <= 0:
                        defeated_pokemon = mon
                        break

            if defeated_pokemon is None and opponent_party:
                defeated_pokemon = opponent_party[-1]

        if defeated_pokemon is None:
            return None

        try:
            results = await self.exp_handler.award_battle_exp(
                trainer_id=trainer.battler_id,
                party=trainer.party,
                defeated_pokemon=defeated_pokemon,
                active_pokemon_index=active_index,
                is_trainer_battle=(battle.battle_type == BattleType.TRAINER)
            )
        except Exception as exc:
            print(f"[BattleCog] Failed to award EXP: {exc}")
            return None

        return self.exp_handler.create_exp_embed(results, trainer.party, defeated_pokemon)

    def _build_ranked_result_embed(self, battle) -> Optional[discord.Embed]:
        if not getattr(battle, 'is_ranked', False):
            return None

        player_manager = getattr(self.bot, 'player_manager', None)
        if not player_manager:
            return None

        winner_side = getattr(battle, 'winner', None)
        if winner_side not in ('trainer', 'opponent'):
            return None

        winner_battler = battle.trainer if winner_side == 'trainer' else battle.opponent
        loser_battler = battle.opponent if winner_side == 'trainer' else battle.trainer
        winner_id = getattr(winner_battler, 'battler_id', None)

        if not isinstance(winner_id, int) or winner_id <= 0:
            return None

        winner_trainer = player_manager.get_player(winner_id)
        if not winner_trainer:
            return None

        winner_rank = getattr(winner_trainer, 'rank_tier_number', None) or 1
        opponent_label = getattr(loser_battler, 'battler_name', 'Opponent')
        opponent_rank = None
        opponent_is_player = (
            getattr(loser_battler, 'battler_id', -1) > 0 and
            getattr(battle, 'battle_type', None) == BattleType.PVP
        )

        if opponent_is_player:
            opponent_id = getattr(loser_battler, 'battler_id', None)
            opponent_trainer = player_manager.get_player(opponent_id) if opponent_id else None
            if opponent_trainer:
                opponent_rank = getattr(opponent_trainer, 'rank_tier_number', None) or 1
                opponent_label = getattr(opponent_trainer, 'trainer_name', opponent_label)
        else:
            ranked_context = getattr(battle, 'ranked_context', {}) or {}
            opponent_rank = ranked_context.get('npc_rank') or 1
            opponent_label = ranked_context.get('npc_name') or opponent_label

        points_awarded = self._calculate_ranked_points(winner_rank, opponent_rank, opponent_is_player)
        if points_awarded <= 0:
            return None

        old_points = getattr(winner_trainer, 'ladder_points', 0) or 0
        new_points = old_points + points_awarded

        try:
            player_manager.update_player(winner_id, ladder_points=new_points)
        except Exception:
            return None

        winner_trainer.ladder_points = new_points

        embed = discord.Embed(
            title="🏆 Challenger Progress Updated",
            description=f"{winner_trainer.trainer_name} gained Challenger points by defeating {opponent_label}.",
            color=discord.Color.green()
        )

        embed.add_field(
            name="Challenger Points",
            value=f"{old_points} → {new_points} (Δ +{points_awarded})",
            inline=False
        )

        if getattr(winner_trainer, 'has_promotion_ticket', False):
            embed.add_field(
                name="Ticket",
                value="🎟️ Challenger ticket already earned.",
                inline=False
            )

        return embed

    def _calculate_ranked_points(self, winner_rank: int, opponent_rank: Optional[int], opponent_is_player: bool) -> int:
        opponent_rank = opponent_rank or winner_rank or 1
        winner_rank = winner_rank or 1

        if opponent_is_player:
            if opponent_rank > winner_rank:
                return 50
            if opponent_rank == winner_rank:
                return 25
            return 10

        if opponent_rank > winner_rank:
            return 20
        if opponent_rank == winner_rank:
            return 10
        return 5

        if not results or not results.get('exp_gains'):
            return None

        try:
            return self.exp_handler.create_exp_embed(results, trainer.party, defeated_pokemon)
        except Exception as exc:
            print(f"[BattleCog] Failed to build EXP embed: {exc}")
            return None

    async def _handle_post_turn(self, interaction: discord.Interaction, battle_id: str):
        battle = self.battle_engine.get_battle(battle_id)
        if not battle:
            return

        if battle.battle_type == BattleType.WILD and getattr(battle, "wild_dazed", False) and not battle.is_over:
            await self._send_dazed_prompt(interaction, battle)
            return

        if battle.is_over:
            await self._finish_battle(interaction, battle)
            return

        if battle.phase == 'FORCED_SWITCH' and battle.forced_switch_battler_id:
            await self._prompt_forced_switch(interaction, battle, battle.forced_switch_battler_id)
            return

        await interaction.followup.send(
            embed=self._create_battle_embed(battle),
            view=self._create_battle_view(battle)
        )

class ForfeitConfirmView(discord.ui.View):
    def __init__(self, action_view: 'BattleActionView'):
        super().__init__(timeout=30)
        self.action_view = action_view

    @discord.ui.button(label="Yes, forfeit", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.action_view._handle_forfeit(interaction)
        try:
            await interaction.edit_original_response(content="Battle forfeited.", view=None, embed=None)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.edit_original_response(content="Forfeit cancelled.", view=None, embed=None)
            except Exception:
                pass
        self.stop()

class BattleActionView(discord.ui.View):
    def __init__(self, battle_id: str, battler_id: int, engine: BattleEngine, battle, battle_cog: 'BattleCog'):
        super().__init__(timeout=300)
        self.battle_id = battle_id
        self.battler_id = battler_id
        self.engine = engine
        self.battle = battle
        self.cog = battle_cog

    @discord.ui.button(label="⚔️ Fight", style=discord.ButtonStyle.danger, row=0)
    async def fight_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Always grab the freshest battle state
        battle = self.engine.get_battle(self.battle_id) or self.battle
        if not battle:
            await interaction.response.send_message("Battle not found.", ephemeral=True)
            return

        # Work out which side this user actually controls (battler_id stores Discord IDs for players)
        battler_id: int | None = None
        if battle.trainer.battler_id == interaction.user.id:
            battler_id = battle.trainer.battler_id
        elif battle.opponent.battler_id == interaction.user.id:
            battler_id = battle.opponent.battler_id

        if battler_id is None:
            await interaction.response.send_message("You are not a participant in this battle.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Choose a move:",
            view=MoveSelectView(battle, battler_id, self.engine),
            ephemeral=True,
        )


    @discord.ui.button(label="🔄 Switch", style=discord.ButtonStyle.primary, row=0)
    async def switch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        battle = self.engine.get_battle(self.battle_id) or self.battle
        if not battle:
            await interaction.response.send_message("Battle not found.", ephemeral=True)
            return

        # Work out which side this user actually controls (battler_id stores Discord IDs for players)
        battler_id: int | None = None
        if battle.trainer.battler_id == interaction.user.id:
            battler_id = battle.trainer.battler_id
        elif battle.opponent.battler_id == interaction.user.id:
            battler_id = battle.opponent.battler_id

        if battler_id is None:
            await interaction.response.send_message("You are not a participant in this battle.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Choose a Pokémon to switch in:",
            view=PartySelectView(battle, battler_id, self.engine, forced=False),
            ephemeral=True,
        )


    @discord.ui.button(label="🎒 Bag", style=discord.ButtonStyle.secondary, row=0)
    async def bag_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        battle = self.engine.get_battle(self.battle_id) or self.battle
        if not battle:
            await interaction.response.send_message("Battle not found.", ephemeral=True)
            return
        cog = self.cog or interaction.client.get_cog("BattleCog")
        if not cog:
            await interaction.response.send_message("Bag system is not available right now.", ephemeral=True)
            return
        await interaction.response.send_message("Items:", view=BagView(cog, battle, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="🏃 Run", style=discord.ButtonStyle.secondary, row=0)
    async def run_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Forfeit the battle?",
            description="Forfeiting counts as a loss. Are you sure you want to run?",
            color=discord.Color.dark_red()
        )
        await interaction.response.send_message(embed=embed, view=ForfeitConfirmView(self), ephemeral=True)

    async def _handle_forfeit(self, interaction: discord.Interaction):
        battle = self.engine.get_battle(self.battle_id)
        if not battle:
            await interaction.followup.send("Battle not found.", ephemeral=True)
            return
        if battle.is_over:
            await interaction.followup.send("The battle is already over.", ephemeral=True)
            return
        if self.battler_id == battle.trainer.battler_id:
            battle.winner = 'opponent'
        else:
            battle.winner = 'trainer'
        battle.is_over = True
        cog = self.cog or interaction.client.get_cog("BattleCog")
        if cog:
            await cog._finish_battle(interaction, battle)
        else:
            self.engine.end_battle(self.battle_id)

class MoveSelectView(discord.ui.View):
    def __init__(self, battle, battler_id: int, engine: BattleEngine):
        super().__init__(timeout=60)
        self.battle = battle
        self.battle_id = battle.battle_id
        self.battler_id = battler_id
        self.engine = engine

        # Figure out which active Pokémon belongs to this battler
        battler = battle.trainer if battler_id == battle.trainer.battler_id else battle.opponent
        active_pokemon = battler.get_active_pokemon()[0] if battler.get_active_pokemon() else None

        if not active_pokemon:
            return

        # Add up to 4 move buttons for this Pokémon
        for mv in getattr(active_pokemon, "moves", [])[:4]:
            move_id = mv.get("move_id") or mv.get("id")
            if not move_id:
                continue

            move_info = engine.moves_db.get_move(move_id) if hasattr(engine, "moves_db") else None
            move_name = (move_info.get("name") if move_info else None) or mv.get("name") or move_id
            cur_pp = mv.get("pp")
            max_pp = mv.get("max_pp")
            label = f"{move_name} ({cur_pp}/{max_pp})" if (cur_pp is not None and max_pp is not None) else move_name

            self.add_item(
                MoveButton(
                    label=label,
                    move_id=move_id,
                    engine=engine,
                    battle_id=self.battle_id,
                    battler_id=battler_id,
                    disabled=(cur_pp is not None and cur_pp <= 0),
                )
            )

class MoveButton(discord.ui.Button):
    def __init__(self, label, move_id, engine: BattleEngine, battle_id: str, battler_id: int, disabled: bool = False):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=0, disabled=disabled)
        self.move_id = move_id
        self.engine = engine
        self.battle_id = battle_id
        self.battler_id = battler_id



    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        action = BattleAction(action_type='move', battler_id=self.battler_id, move_id=self.move_id, target_position=0)
        res = self.engine.register_action(self.battle_id, self.battler_id, action)
        cog = interaction.client.get_cog("BattleCog")

        # If the other trainer hasn't chosen yet, just notify this user and stop.
        if not res.get("ready_to_resolve"):
            await interaction.followup.send(
                "Move selected! Waiting for the other trainer to choose...",
                ephemeral=True,
            )
            return

        if res.get("ready_to_resolve") and cog:
            turn = await self.engine.process_turn(self.battle_id)
            # Compose a narration + refreshed battle panel
            msgs = turn.get("narration", [])
            if not msgs and "messages" in turn:
                msgs = turn["messages"]
            # Add spacing between messages for better readability
            if msgs:
                spaced_msgs = []
                for msg in msgs[-6:]:
                    spaced_msgs.append(msg)
                    spaced_msgs.append("")  # Add blank line after each message
                # Remove trailing blank line
                if spaced_msgs and spaced_msgs[-1] == "":
                    spaced_msgs.pop()
                desc = "\n".join(spaced_msgs)
            else:
                desc = "The turn resolves."
            e = discord.Embed(title="Turn Result", description=desc, color=discord.Color.orange())
            await interaction.followup.send(embed=e)
            # Send separate AI send-out embed if present
            switch_msgs = turn.get('switch_messages') or []
            if switch_msgs:
                send_embed = discord.Embed(title='Send-out', description='\n\n'.join(switch_msgs), color=discord.Color.blurple())
                await interaction.followup.send(embed=send_embed)
        battle = self.engine.get_battle(self.battle_id)
        if battle:
            from cogs.battle_cog import BattleCog  # type: ignore
            # naive way to get cog from interaction.client
            cog = interaction.client.get_cog("BattleCog")
            if cog:
                refreshed = cog._create_battle_embed(battle)
                
                # If this is a wild battle and the opponent is dazed, show the catch prompt instead of the battle panel
                if battle.battle_type == BattleType.WILD and getattr(battle, 'wild_dazed', False) and not battle.is_over:
                    await cog._send_dazed_prompt(interaction, battle)
                    return
                
                if turn.get('is_over') or battle.is_over:
                    # Map engine winner ('trainer'|'opponent'|'draw') to names
                    result = turn.get('winner') or battle.winner
                    trainer_name = getattr(battle.trainer, 'battler_name', 'Trainer')
                    opponent_name = getattr(battle.opponent, 'battler_name', 'Opponent')
                    if result == 'trainer':
                        winner_name, loser_name = trainer_name, opponent_name
                    elif result == 'opponent':
                        winner_name, loser_name = opponent_name, trainer_name
                    else:
                        desc = "🏆 Battle Over\n\nIt's a draw!"
                        await interaction.followup.send(embed=discord.Embed(title='Battle Over', description=desc, color=discord.Color.gold()))
                        
                        # Clean up battle
                        self.engine.end_battle(self.battle_id)
                        if hasattr(cog, '_unregister_battle'):
                            cog._unregister_battle(battle)
                        return
                    # Persist party HP to database (player side)
                    try:
                        from database import PlayerDatabase
                        pdb = PlayerDatabase('data/players.db')
                        party_rows = pdb.get_trainer_party(battle.trainer.battler_id)
                        rows_by_pos = {row.get('party_position', i): row for i, row in enumerate(party_rows)}
                        for i, mon in enumerate(battle.trainer.party):
                            row = rows_by_pos.get(i) or rows_by_pos.get(getattr(mon, 'party_position', i))
                            if row and 'pokemon_id' in row:
                                pdb.update_pokemon(row['pokemon_id'], {'current_hp': max(0, int(getattr(mon, 'current_hp', 0))) })
                    except Exception:
                        pass
                    
                    # Send battle over message
                    desc = f"🏆 Battle Over\n\nAll of {loser_name}'s Pokémon have fainted! {winner_name} wins!"
                    await interaction.followup.send(embed=discord.Embed(title='Battle Over', description=desc, color=discord.Color.gold()))
                    
                    # Send exp gain embed if trainer won
                    create_exp_embed = getattr(cog, "_create_exp_embed", None)
                    if create_exp_embed:
                        exp_embed = await create_exp_embed(battle, interaction)
                        if exp_embed:
                            await interaction.followup.send(embed=exp_embed)
                    
                    # Clean up battle
                    self.engine.end_battle(self.battle_id)
                    if hasattr(cog, '_unregister_battle'):
                        cog._unregister_battle(battle)

                    if getattr(battle, 'battle_type', None) == BattleType.WILD:
                        await cog.send_return_to_encounter_prompt(interaction, interaction.user.id)
                else:
                    # Let BattleCog handle post-turn logic: forced switches, KO prompts, etc.
                    await cog._handle_post_turn(interaction, self.battle_id)
        
class PartySelect(discord.ui.Select):
    def __init__(self, battle, battler_id: int, forced: bool = False):
        self.battle = battle
        self.battler_id = battler_id
        self.forced = forced
        options = []
        battler = battle.trainer if battler_id == battle.trainer.battler_id else battle.opponent
        party = battler.party
        active_index = battler.active_positions[0]  # Get actual active position
        for idx, mon in enumerate(party):
            name = getattr(mon, "species_name", f"Slot {idx+1}")
            hp = f"{getattr(mon, 'current_hp', 0)}/{getattr(mon, 'max_hp', 1)}"
            disabled = (idx == active_index) or getattr(mon, "current_hp", 0) <= 0
            options.append(discord.SelectOption(label=name, description=f"HP {hp}", value=str(idx), default=False))
        placeholder = "Choose a Pokémon to send out" if forced else "Choose a Pokémon to switch in"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        idx = int(self.values[0])
        cog = interaction.client.get_cog("BattleCog")
        parent_view = getattr(self, 'view', None)
        if not parent_view:
            await interaction.followup.send("That switch prompt expired.", ephemeral=True)
            return

        if self.forced:
            result = parent_view.engine.force_switch(parent_view.battle_id, self.battler_id, idx)
            if result.get("error"):
                await interaction.followup.send(result["error"], ephemeral=True)
                return
            messages = result.get('messages', [])
            if cog:
                send_embed = cog._build_switch_embed(messages, title="Send-out")
                if send_embed:
                    await interaction.followup.send(embed=send_embed)
                battle = parent_view.engine.get_battle(parent_view.battle_id)
                if battle:
                    await interaction.followup.send(
                        embed=cog._create_battle_embed(battle),
                        view=cog._create_battle_view(battle),
                    )
            else:
                text = "\n".join(messages) or "A new Pokémon entered the battle."
                await interaction.followup.send(text)
            return

        action = BattleAction(action_type='switch', battler_id=self.battler_id, switch_to_position=idx)
        res = parent_view.engine.register_action(parent_view.battle_id, self.battler_id, action)
        if res.get("ready_to_resolve") and cog:
            turn = await parent_view.engine.process_turn(parent_view.battle_id)
            await cog._send_turn_resolution(interaction, turn)
        if cog:
            await cog._handle_post_turn(interaction, parent_view.battle_id)
class PartySelectView(discord.ui.View):
    def __init__(self, battle, battler_id: int, engine: BattleEngine, forced: bool = False):
        super().__init__(timeout=60)
        self.battle_id = battle.battle_id
        self.engine = engine
        self.forced = forced
        self.add_item(PartySelect(battle, battler_id, forced=forced))
class BagView(discord.ui.View):
    """In-battle bag view focusing on Pokeballs so you can attempt captures at any time."""
    def __init__(self, battle_cog: BattleCog, battle, discord_user_id: int):
        super().__init__(timeout=120)
        self.battle_cog = battle_cog
        self.battle_id = battle.battle_id
        self.engine = battle_cog.battle_engine
        self.discord_user_id = discord_user_id

        balls = self.battle_cog._get_ball_inventory(discord_user_id)

        if not balls:
            self.add_item(
                discord.ui.Button(
                    label="(No usable items found)",
                    style=discord.ButtonStyle.secondary,
                    disabled=True
                )
            )
            return

        self.add_item(BagBallSelect(battle_cog, self.battle_id, balls))


class DazedCatchView(discord.ui.View):
    """Prompt that lets trainers confirm whether they will catch a dazed wild Pokemon."""

    def __init__(self, battle_cog: BattleCog, battle_id: str):
        super().__init__(timeout=120)
        self.battle_cog = battle_cog
        self.battle_id = battle_id

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Player chooses to attempt a guaranteed capture on a dazed target."""

        balls = self.battle_cog._get_ball_inventory(interaction.user.id)
        if not balls:
            await interaction.response.edit_message(
                content="❌ You have no Poke Balls available!",
                embed=None,
                view=None,
            )
            return

        options = [
            discord.SelectOption(
                label=f"{item_data.get('name', item_id)} x{qty}"[:100],
                value=item_id,
            )
            for item_id, (item_data, qty) in balls.items()
        ]

        select = discord.ui.Select(
            placeholder="Choose a Poke Ball",
            min_values=1,
            max_values=1,
            options=options,
        )

        async def select_callback(select_interaction: discord.Interaction):
            chosen_id = select_interaction.data["values"][0]
            await self.battle_cog._handle_ball_throw(
                select_interaction,
                self.battle_id,
                chosen_id,
                guaranteed=True,
            )
            try:
                await select_interaction.edit_original_response(view=None)
            except discord.HTTPException:
                pass

        select.callback = select_callback
        new_view = discord.ui.View(timeout=120)
        new_view.add_item(select)

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Select a Poke Ball",
                color=discord.Color.blue(),
            ),
            view=new_view,
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Player declines to catch; the wild Pokemon flees and the encounter ends."""

        battle = self.battle_cog.battle_engine.get_battle(self.battle_id)
        if battle:
            battle.is_over = True
            battle.winner = "trainer"

        await interaction.response.edit_message(
            embed=discord.Embed(
                title="The wild Pokemon ran away!",
                description="It came to its senses and fled.",
                color=discord.Color.dark_grey(),
            ),
            view=None,
        )

async def setup(bot):
    """discord.py 2.x extension entrypoint for BattleCog"""
    # Reuse existing engine if present
    engine = getattr(bot, "battle_engine", None)
    if engine is None:
        # Build required DBs from cached bot attributes when possible
        from database import MovesDatabase, TypeChart, SpeciesDatabase, ItemsDatabase

        moves_db = getattr(bot, 'moves_db', None) or MovesDatabase('data/moves.json')
        type_chart = getattr(bot, 'type_chart', None) or TypeChart('data/type_chart.json')
        species_db = getattr(bot, 'species_db', None) or SpeciesDatabase('data/pokemon_species.json')
        items_db = getattr(bot, 'items_db', None) or ItemsDatabase('data/items.json')

        from battle_engine_v2 import BattleEngine
        engine = BattleEngine(moves_db, type_chart, species_db, items_db=items_db)
        bot.battle_engine = engine
    else:
        if getattr(engine, 'held_item_manager', None) is None and getattr(bot, 'items_db', None):
            engine.items_db = bot.items_db
            engine.held_item_manager = HeldItemManager(bot.items_db)
    await bot.add_cog(BattleCog(bot, engine))