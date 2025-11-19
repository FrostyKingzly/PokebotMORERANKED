"""
Battle Engine V2 - Unified Core Battle System
Supports: Wild battles, Trainer battles (PvE), and PvP battles

This is a complete rewrite that handles ALL battle types with a single engine.
Includes abilities, switching, items, and AI opponent support.
"""

import re
import random
import json
import uuid
import math
from typing import Dict, List, Optional, Tuple, Any
from ruleset_handler import RulesetHandler
from dataclasses import dataclass, field
from enum import Enum

# Import enhanced systems
try:
    from enhanced_calculator import EnhancedDamageCalculator
    from status_conditions import StatusConditionManager
    from ability_handler import AbilityHandler
    ENHANCED_SYSTEMS_AVAILABLE = True
except ImportError:
    ENHANCED_SYSTEMS_AVAILABLE = False
    print("⚠️ Enhanced systems not available. Using basic calculator.")


class BattleType(Enum):
    """Types of battles supported"""
    WILD = "wild"
    TRAINER = "trainer"  # PvE against NPC
    PVP = "pvp"  # Player vs Player


class BattleFormat(Enum):
    """Battle format types"""
    SINGLES = "singles"  # 1v1
    DOUBLES = "doubles"  # 2v2
    MULTI = "multi"  # 2v2 with partners


@dataclass
class Battler:
    """Represents one side of a battle (trainer or opponent)"""
    battler_id: int  # Discord ID for trainers, negative for NPCs/wild
    battler_name: str
    party: List[Any]  # List of Pokemon objects
    active_positions: List[int]  # Which Pokemon are currently active (indices into party)
    is_ai: bool = False  # Whether this battler is controlled by AI
    can_switch: bool = True
    can_use_items: bool = True
    can_flee: bool = False
    
    # For trainer battles
    trainer_class: Optional[str] = None  # "Youngster", "Ace Trainer", etc.
    prize_money: int = 0
    
    def get_active_pokemon(self) -> List[Any]:
        """Get currently active Pokemon"""
        return [self.party[i] for i in self.active_positions if i < len(self.party)]
    
    def has_usable_pokemon(self) -> bool:
        """Check if battler has any Pokemon that can still fight"""
        return any(p.current_hp > 0 for p in self.party)


@dataclass 
class BattleState:
    """Complete state of an ongoing battle"""
    battle_id: str
    battle_type: BattleType
    battle_format: BattleFormat
    
    # Battlers (either 2 for normal, or 4 for multi battles)
    trainer: Battler  # The player who initiated
    opponent: Battler  # Wild Pokemon, NPC trainer, or other player
    
    # Battle state
    turn_number: int = 1
    phase: str = 'START'  # START, WAITING_ACTIONS, RESOLVING, FORCED_SWITCH, END
    forced_switch_battler_id: Optional[int] = None  # Which battler must switch
    is_over: bool = False
    winner: Optional[str] = None  # 'trainer', 'opponent', 'draw'
    fled: bool = False
    
    # Field conditions
    weather: Optional[str] = None  # 'sandstorm', 'rain', 'sun', 'snow', 'hail'
    weather_turns: int = 0
    terrain: Optional[str] = None  # 'electric', 'grassy', 'psychic', 'misty'
    terrain_turns: int = 0
    
    # Field hazards
    trainer_hazards: Dict[str, int] = field(default_factory=dict)  # 'stealth_rock': 1, 'spikes': 3, etc.
    opponent_hazards: Dict[str, int] = field(default_factory=dict)
    
    # Screens and field effects
    trainer_screens: Dict[str, int] = field(default_factory=dict)  # 'reflect': 5, 'light_screen': 3
    opponent_screens: Dict[str, int] = field(default_factory=dict)
    
    # Turn actions (stored for simultaneous resolution)
    pending_actions: Dict[str, 'BattleAction'] = field(default_factory=dict)  # battler_id -> action
    
    # Battle log
    battle_log: List[str] = field(default_factory=list)
    turn_log: List[str] = field(default_factory=list)  # Current turn's events
    
    # NEW: queue AI replacement to happen AFTER end-of-turn
    pending_ai_switch_index: Optional[int] = None
    
    # For wild battles only
    catch_attempted: bool = False
    wild_dazed: bool = False  # True when wild Pokémon has been reduced to a 'dazed' state instead of fainting

    # Ranked metadata
    is_ranked: bool = False
    ranked_context: Dict[str, Any] = field(default_factory=dict)


class HeldItemManager:
    """Utility helper for held item effects."""

    def __init__(self, items_db):
        self.items_db = items_db

    def _is_consumed(self, pokemon, item_id: str) -> bool:
        consumed = getattr(pokemon, '_consumed_items', set())
        return item_id in consumed

    def _consume(self, pokemon, item_id: str):
        consumed = getattr(pokemon, '_consumed_items', set())
        consumed.add(item_id)
        pokemon._consumed_items = consumed

    def _get_item(self, pokemon):
        if not self.items_db:
            return None
        item_id = getattr(pokemon, 'held_item', None)
        if not item_id:
            return None
        if self._is_consumed(pokemon, item_id):
            return None
        return self.items_db.get_item(item_id)

    # -------- Restrictions / tracking --------
    def check_move_restrictions(self, pokemon, move_data) -> Optional[str]:
        item = self._get_item(pokemon)
        if not item:
            return None
        effect = item.get('effect_data') or {}

        if effect.get('blocks_status_moves') and move_data.get('category') == 'status':
            return f"{pokemon.species_name} can't use status moves while holding {item.get('name', item['id'])}!"

        if effect.get('locks_move'):
            locked = getattr(pokemon, '_choice_locked_move', None)
            move_id = move_data.get('id') or move_data.get('move_id')
            if locked and move_id and move_id != locked:
                move_name = move_data.get('name', move_id).title()
                item_name = item.get('name', item['id'])
                return f"{pokemon.species_name} is locked into {move_name} because of its {item_name}!"
        return None

    def register_move_use(self, pokemon, move_data):
        item = self._get_item(pokemon)
        if not item:
            return
        effect = item.get('effect_data') or {}
        if effect.get('locks_move'):
            move_id = move_data.get('id') or move_data.get('move_id')
            pokemon._choice_locked_move = move_id

    def clear_choice_lock(self, pokemon):
        if hasattr(pokemon, '_choice_locked_move'):
            delattr(pokemon, '_choice_locked_move')

    # -------- Offensive modifiers --------
    def _power_multiplier(self, pokemon, move_data) -> float:
        item = self._get_item(pokemon)
        if not item:
            return 1.0
        effect = item.get('effect_data') or {}
        multiplier = 1.0
        move_type = (move_data.get('type') or '').lower()
        category = move_data.get('category')

        if effect.get('type'):
            if move_type == effect['type'].lower():
                multiplier *= effect.get('power_multiplier', 1.0)
        elif 'power_multiplier' in effect:
            multiplier *= effect.get('power_multiplier', 1.0)

        stat = effect.get('stat')
        stat_mult = effect.get('multiplier', 1.0)
        if stat == 'attack' and category == 'physical':
            multiplier *= stat_mult
        elif stat == 'sp_attack' and category == 'special':
            multiplier *= stat_mult

        return multiplier

    def _defense_multiplier(self, pokemon, move_data) -> float:
        item = self._get_item(pokemon)
        if not item:
            return 1.0
        effect = item.get('effect_data') or {}
        stat = effect.get('stat')
        if stat == 'sp_defense' and move_data.get('category') == 'special':
            return effect.get('multiplier', 1.0)
        return 1.0

    def modify_damage(self, attacker, defender, move_data, damage: int) -> Tuple[int, List[str]]:
        if damage <= 0:
            return damage, []

        messages: List[str] = []
        damage = int(round(damage * self._power_multiplier(attacker, move_data)))
        defense_mult = self._defense_multiplier(defender, move_data)
        if defense_mult > 1:
            damage = max(1, int(math.ceil(damage / defense_mult)))

        damage, survival_msg = self._try_focus_items(defender, damage)
        if survival_msg:
            messages.append(survival_msg)

        return damage, messages

    def _try_focus_items(self, defender, damage: int) -> Tuple[int, Optional[str]]:
        if damage < defender.current_hp or defender.current_hp <= 0:
            return damage, None
        item = self._get_item(defender)
        if not item:
            return damage, None
        effect = item.get('effect_data') or {}

        trigger = item.get('trigger')
        if trigger and trigger != 'before_damage':
            return damage, None

        prevents_ko = effect.get('prevents_ko') or effect.get('requires_full_hp') or ('activation_chance' in effect)
        if not prevents_ko:
            return damage, None

        if effect.get('requires_full_hp') and defender.current_hp < defender.max_hp:
            return damage, None

        activation = effect.get('activation_chance')
        if activation is not None and random.random() > activation:
            return damage, None

        if defender.current_hp <= 1:
            return damage, None

        damage = defender.current_hp - 1
        item_name = item.get('name', item['id'])
        message = f"{defender.species_name} hung on using its {item_name}!"
        if effect.get('one_time_use'):
            self._consume(defender, item['id'])
        return damage, message

    def apply_after_damage(self, attacker, move_data, dealt_damage: int) -> List[str]:
        item = self._get_item(attacker)
        if not item:
            return []

        # Choice items lock even on misses
        self.register_move_use(attacker, move_data)

        if dealt_damage <= 0:
            return []

        effect = item.get('effect_data') or {}
        messages: List[str] = []

        if effect.get('recoil_percent'):
            recoil = max(1, int(round(attacker.max_hp * (effect['recoil_percent'] / 100.0))))
            attacker.current_hp = max(0, attacker.current_hp - recoil)
            messages.append(f"{attacker.species_name} was hurt by its {item.get('name', item['id'])}! (-{recoil} HP)")

        return messages

    def process_end_of_turn(self, pokemon) -> List[str]:
        item = self._get_item(pokemon)
        if not item:
            return []
        effect = item.get('effect_data') or {}
        heal_percent = effect.get('heal_percent')
        if not heal_percent or getattr(pokemon, 'current_hp', 0) <= 0 or pokemon.current_hp >= pokemon.max_hp:
            return []
        heal = max(1, int(round(pokemon.max_hp * (heal_percent / 100.0))))
        pokemon.current_hp = min(pokemon.max_hp, pokemon.current_hp + heal)
        return [f"{pokemon.species_name} restored health with its {item.get('name', item['id'])}! (+{heal} HP)"]

    def get_speed_multiplier(self, pokemon) -> float:
        item = self._get_item(pokemon)
        if not item:
            return 1.0
        effect = item.get('effect_data') or {}
        if effect.get('stat') == 'speed':
            return effect.get('multiplier', 1.0)
        return 1.0

@dataclass
class BattleAction:
    """A single action taken by a battler"""
    action_type: str  # 'move', 'switch', 'item', 'flee'
    battler_id: int
    
    # For moves
    move_id: Optional[str] = None
    target_position: Optional[int] = None  # Which opponent slot to target
    mega_evolve: bool = False
    
    # For switching
    switch_to_position: Optional[int] = None
    
    # For items
    item_id: Optional[str] = None
    item_target_position: Optional[int] = None  # Which party member gets the item
    
    # Priority for turn order
    priority: int = 0
    speed: int = 0


class BattleEngine:
    """
    Core battle engine that handles all battle types
    """
    
    def __init__(self, moves_db, type_chart, species_db=None, items_db=None):
        """
        Initialize the battle engine
        
        Args:
            moves_db: MovesDatabase instance
            type_chart: Type effectiveness data
            species_db: Optional species database for wild Pokemon generation
        """
        self.moves_db = moves_db
        self.type_chart = type_chart
        self.species_db = species_db
        self.items_db = items_db
        self.held_item_manager = HeldItemManager(items_db) if items_db else None
        
        # Initialize enhanced systems
        # Ruleset handler
        self.ruleset_handler = RulesetHandler()
        if ENHANCED_SYSTEMS_AVAILABLE:
            self.calculator = EnhancedDamageCalculator(moves_db, type_chart)
            self.ability_handler = AbilityHandler('data/abilities.json')
            print("✨ Enhanced battle systems loaded!")
        else:
            print("⚠️ Using basic battle calculator")
        
        # Active battles
        self.active_battles: Dict[str, BattleState] = {}
    
    # ========================
    # Battle Initialization
    # ========================
    
    def start_battle(
        self,
        trainer_id: int,
        trainer_name: str,
        trainer_party: List[Any],
        opponent_party: List[Any],
        battle_type: BattleType,
        battle_format: BattleFormat = BattleFormat.SINGLES,
        opponent_id: Optional[int] = None,
        opponent_name: Optional[str] = None,
        opponent_is_ai: bool = True,
        is_ranked: bool = False,
        ranked_context: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """Universal battle starter"""
        battle_id = str(uuid.uuid4())

        if not trainer_party:
            raise ValueError("Trainer must have at least one Pokémon to start a battle.")
        if not opponent_party:
            raise ValueError("Opponent must have at least one Pokémon to battle.")

        active_slot_count = 2 if battle_format == BattleFormat.DOUBLES else 1
        trainer_active_positions = list(range(min(len(trainer_party), active_slot_count)))
        opponent_active_positions = list(range(min(len(opponent_party), active_slot_count)))
        if not trainer_active_positions:
            trainer_active_positions = [0]
        if not opponent_active_positions:
            opponent_active_positions = [0]

        # Create trainer battler
        trainer = Battler(
            battler_id=trainer_id,
            battler_name=trainer_name,
            party=trainer_party,
            active_positions=trainer_active_positions,
            is_ai=False,
            can_switch=True,
            can_use_items=True,
            can_flee=(battle_type == BattleType.WILD)
        )
        
        # Create opponent battler
        if opponent_id is None:
            opponent_id = -1 if battle_type == BattleType.WILD else -random.randint(1000, 9999)
        
        opponent = Battler(
            battler_id=opponent_id,
            battler_name=opponent_name or ("Wild Pokémon" if battle_type == BattleType.WILD else "Opponent"),
            party=opponent_party,
            active_positions=opponent_active_positions,
            is_ai=opponent_is_ai,
            can_switch=(battle_type != BattleType.WILD),  # Wild Pokemon can't switch
            can_use_items=(battle_type == BattleType.TRAINER),
            can_flee=False,
            trainer_class=kwargs.get('trainer_class'),
            prize_money=kwargs.get('prize_money', 0)
        )
        
        # Create battle state
        battle = BattleState(
            battle_id=battle_id,
            battle_type=battle_type,
            battle_format=battle_format,
            trainer=trainer,
            opponent=opponent,
            is_ranked=is_ranked,
            ranked_context=ranked_context or {}
        )
        
        # Trigger entry abilities
        # Trigger entry abilities and capture messages
        try:
            battle.entry_messages = self._trigger_entry_abilities(battle)
        except Exception:
            battle.entry_messages = []

        # Default to Standard NatDex (nat)
        try:
            battle.ruleset = self.ruleset_handler.resolve_default_ruleset('nat')
        except Exception:
            battle.ruleset = 'standardnatdex'

        # Store battle
        self.active_battles[battle_id] = battle
        
        return battle_id
    
    def start_wild_battle(self, trainer_id: int, trainer_name: str, 
                         trainer_party: List[Any], wild_pokemon: Any) -> str:
        """Convenience method for wild battles"""
        return self.start_battle(
            trainer_id=trainer_id,
            trainer_name=trainer_name,
            trainer_party=trainer_party,
            opponent_party=[wild_pokemon],
            battle_type=BattleType.WILD,
            opponent_name=f"Wild {wild_pokemon.species_name}"
        )
    
    def start_trainer_battle(
        self,
        trainer_id: int,
        trainer_name: str,
        trainer_party: List[Any],
        npc_party: List[Any],
        npc_name: str,
        npc_class: str,
        prize_money: int,
        is_ranked: bool = False,
        ranked_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Convenience method for NPC trainer battles"""
        return self.start_battle(
            trainer_id=trainer_id,
            trainer_name=trainer_name,
            trainer_party=trainer_party,
            opponent_party=npc_party,
            battle_type=BattleType.TRAINER,
            opponent_name=npc_name,
            trainer_class=npc_class,
            prize_money=prize_money,
            is_ranked=is_ranked,
            ranked_context=ranked_context
        )
    
    def start_pvp_battle(
        self,
        trainer1_id: int,
        trainer1_name: str,
        trainer1_party: List[Any],
        trainer2_id: int,
        trainer2_name: str,
        trainer2_party: List[Any],
        battle_format: BattleFormat = BattleFormat.SINGLES,
        is_ranked: bool = False,
        ranked_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Convenience method for PvP battles"""
        return self.start_battle(
            trainer_id=trainer1_id,
            trainer_name=trainer1_name,
            trainer_party=trainer1_party,
            opponent_party=trainer2_party,
            battle_type=BattleType.PVP,
            opponent_id=trainer2_id,
            opponent_name=trainer2_name,
            opponent_is_ai=False,
            battle_format=battle_format,
            is_ranked=is_ranked,
            ranked_context=ranked_context
        )
    
    # ========================
    # Ability System
    # ========================
    
    def _trigger_entry_abilities(self, battle: BattleState) -> list[str]:
        """Trigger abilities when Pokemon enter the field"""
        if not ENHANCED_SYSTEMS_AVAILABLE:
            return []
        
        messages = []
        
        # Trigger for all active Pokemon
        for pokemon in battle.trainer.get_active_pokemon():
            ability_msgs = self.ability_handler.trigger_on_entry(pokemon, battle)
            messages.extend(ability_msgs)
            messages.extend(self._apply_entry_hazards(battle, battle.trainer, pokemon))

        for pokemon in battle.opponent.get_active_pokemon():
            ability_msgs = self.ability_handler.trigger_on_entry(pokemon, battle)
            messages.extend(ability_msgs)
            messages.extend(self._apply_entry_hazards(battle, battle.opponent, pokemon))

        return messages


    # ========================
    # Action Registration
    # ========================
    
    def register_action(self, battle_id: str, battler_id: int, action: BattleAction) -> Dict:
        """
        Register an action for a battler
        
        Returns:
            Status dict with success/error
        """
        battle = self.active_battles.get(battle_id)
        if not battle:
            return {"error": "Battle not found"}
        
        # NEW CODE: Check if forced switch is required
        if battle.phase == 'FORCED_SWITCH':
            if battle.forced_switch_battler_id == battler_id:
                if action.action_type != 'switch':
                    return {"error": "You must switch to another Pokémon!"}
                # Clear forced switch state after valid switch action
                battle.phase = 'WAITING_ACTIONS'
                battle.forced_switch_battler_id = None
            # If it's not the forced switch battler, don't allow actions yet
            elif battler_id != battle.forced_switch_battler_id:
                return {"error": "Waiting for opponent to switch..."}
        
        if battle.is_over:
            return {"error": "Battle is already over"}
        
        # Validate battler
        if battler_id not in [battle.trainer.battler_id, battle.opponent.battler_id]:
            return {"error": "Invalid battler ID"}
        
        # Store action
        battle.pending_actions[str(battler_id)] = action
        
        # Check if we have all actions needed
        required_actions = []
        if not battle.trainer.is_ai:
            required_actions.append(str(battle.trainer.battler_id))
        if not battle.opponent.is_ai:
            required_actions.append(str(battle.opponent.battler_id))
        
        all_actions_ready = all(str(rid) in battle.pending_actions for rid in required_actions)
        
        return {
            "success": True,
            "waiting_for": [rid for rid in required_actions if str(rid) not in battle.pending_actions],
            "ready_to_resolve": all_actions_ready
        }
    
    def generate_ai_action(self, battle_id: str, battler_id: int) -> BattleAction:
        """
        Generate an AI action for a battler
        
        For now: Simple random move selection
        TODO: Implement smart AI
        """
        battle = self.active_battles.get(battle_id)
        if not battle:
            return None
        
        # Find the battler
        battler = battle.trainer if battle.trainer.battler_id == battler_id else battle.opponent
        active_pokemon = battler.get_active_pokemon()[0]  # Get first active (singles for now)
        
        # Simple AI: Pick a random move
        usable_moves = [m for m in active_pokemon.moves if m['pp'] > 0]
        if not usable_moves:
            # Struggle
            return BattleAction(
                action_type='move',
                battler_id=battler_id,
                move_id='struggle',
                target_position=0
            )
        
        chosen_move = random.choice(usable_moves)
        
        return BattleAction(
            action_type='move',
            battler_id=battler_id,
            move_id=chosen_move['move_id'],
            target_position=0  # Target first opponent
        )
    
    # ========================
    # Turn Processing
    # ========================
    
    async def process_turn(self, battle_id: str) -> Dict:
        """
        Process a complete turn with all registered actions
        
        Returns:
            Dict with turn results and narration
        """
        battle = self.active_battles.get(battle_id)
        if not battle:
            return {"error": "Battle not found"}
        
        # Generate AI actions if needed
        if battle.trainer.is_ai and str(battle.trainer.battler_id) not in battle.pending_actions:
            action = self.generate_ai_action(battle_id, battle.trainer.battler_id)
            battle.pending_actions[str(battle.trainer.battler_id)] = action
        
        if battle.opponent.is_ai and str(battle.opponent.battler_id) not in battle.pending_actions:
            action = self.generate_ai_action(battle_id, battle.opponent.battler_id)
            battle.pending_actions[str(battle.opponent.battler_id)] = action
        
        # Clear turn log
        battle.turn_log = []
        
        # Sort actions by priority and speed
        actions = list(battle.pending_actions.values())
        actions = self._sort_actions(battle, actions)
        
        
        manual_switch_messages: List[str] = []

        # Execute actions in order
        for action in actions:
            # If the battle is over or the wild Pokémon has been dazed, stop resolving further actions
            if battle.is_over or getattr(battle, "wild_dazed", False):
                break

            # Skip actions for battlers whose active Pokémon have fainted
            battler = battle.trainer if action.battler_id == battle.trainer.battler_id else battle.opponent
            active_pokemon = battler.get_active_pokemon()
            if not active_pokemon or all(p.current_hp <= 0 for p in active_pokemon):
                # This side has no conscious active Pokémon right now (usually due to fainting earlier this turn)
                # They will either be forced to switch or the battle will end, so their queued action is ignored.
                continue

            # If a forced switch is pending for this battler, ignore non-switch actions
            if (
                battle.phase == 'FORCED_SWITCH'
                and battle.forced_switch_battler_id == battler.battler_id
                and action.action_type != 'switch'
            ):
                continue

            result = await self._execute_action(battle, action)
            if action.action_type == 'switch':
                manual_switch_messages.extend(result.get('messages', []))
            else:
                battle.turn_log.extend(result.get('messages', []))

        # End of turn effects (skip if wild Pokémon is in the special 'dazed' state)
        if getattr(battle, "wild_dazed", False):
            eot_messages = []
            auto_switch_messages = []
        else:
            eot_messages = self._process_end_of_turn(battle)
            auto_switch_messages = self.auto_switch_if_forced_ai(battle)

        battle.turn_log.extend(eot_messages)

        switch_messages = manual_switch_messages + auto_switch_messages
        
        # Check for battle end
        self._check_battle_end(battle)
        
        # Clear pending actions
        battle.pending_actions = {}
        
        # Increment turn
        battle.turn_number += 1
        
        return {
            "success": True,
            "turn_number": battle.turn_number - 1,
            "messages": battle.turn_log,
            "switch_messages": switch_messages,
            "is_over": battle.is_over,
            "winner": battle.winner,
            "battle_over": battle.is_over
        }
    
    def _sort_actions(self, battle: BattleState, actions: List[BattleAction]) -> List[BattleAction]:
        """Sort actions by priority, then speed"""
        # Get move priority and speed for each action
        def get_action_priority(action: BattleAction) -> Tuple[int, int]:
            # Switching always goes first
            if action.action_type == 'switch':
                return (100, 999)
            
            # Items are high priority
            if action.action_type == 'item':
                return (90, 999)
            
            # Moves
            if action.action_type == 'move':
                move_data = self.moves_db.get_move(action.move_id)
                priority = move_data.get('priority', 0)
                
                # Get Pokemon speed
                battler = battle.trainer if action.battler_id == battle.trainer.battler_id else battle.opponent
                pokemon = battler.get_active_pokemon()[0]  # Simplified for now
                speed = self._get_effective_speed(pokemon)
                
                return (priority, speed)
            
            # Flee
            return (0, 0)
        
        actions.sort(key=get_action_priority, reverse=True)
        return actions

    def _get_effective_speed(self, pokemon) -> int:
        speed = getattr(pokemon, 'speed', 0)
        if ENHANCED_SYSTEMS_AVAILABLE and hasattr(self, 'calculator'):
            try:
                speed = self.calculator.get_speed(pokemon)
            except Exception:
                pass
        if self.held_item_manager:
            speed = int(round(speed * self.held_item_manager.get_speed_multiplier(pokemon)))
        return speed
    
    async def _execute_action(self, battle: BattleState, action: BattleAction) -> Dict:
        """Execute a single action"""
        if action.action_type == 'move':
            return await self._execute_move(battle, action)
        elif action.action_type == 'switch':
            return self._execute_switch(battle, action)
        elif action.action_type == 'item':
            return self._execute_item(battle, action)
        elif action.action_type == 'flee':
            return self._execute_flee(battle, action)
        
        return {"messages": []}
    
    async def _execute_move(self, battle: BattleState, action: BattleAction) -> Dict:
        """Execute a move action"""
        # Get attacker and defender
        if action.battler_id == battle.trainer.battler_id:
            attacker_battler = battle.trainer
            defender_battler = battle.opponent
        else:
            attacker_battler = battle.opponent
            defender_battler = battle.trainer
        
        attacker = attacker_battler.get_active_pokemon()[0]
        defender = defender_battler.get_active_pokemon()[action.target_position or 0]
        
        # Check if attacker can move (status conditions, flinch, etc.)
        if ENHANCED_SYSTEMS_AVAILABLE and hasattr(attacker, 'status_manager'):
            can_move, prevention_msg = attacker.status_manager.can_move(attacker)
            if not can_move:
                return {"messages": [prevention_msg]}
        
        # Get move data
        move_data = self.moves_db.get_move(action.move_id)
        if not move_data:
            return {"messages": [f"{attacker.species_name} tried to use an unknown move!"]}

        if self.held_item_manager:
            restriction = self.held_item_manager.check_move_restrictions(attacker, move_data)
            if restriction:
                return {"messages": [restriction]}
        
        # Validate move by ruleset
        if hasattr(battle, 'ruleset') and self.ruleset_handler:
            ok, reason = self.ruleset_handler.is_move_allowed(action.move_id, battle.ruleset)
            if not ok:
                return {"messages": [f"{attacker.species_name} tried to use {move_data.get('name', action.move_id)} but it's banned by rules ({reason})."]}

        # Deduct PP
        for move in attacker.moves:
            if move['move_id'] == action.move_id:
                move['pp'] = max(0, move['pp'] - 1)
                break
        
        # Calculate damage and apply effects
        if ENHANCED_SYSTEMS_AVAILABLE:
            damage, is_crit, effectiveness, effect_msgs = self.calculator.calculate_damage_with_effects(
                attacker, defender, action.move_id,
                weather=battle.weather,
                terrain=battle.terrain,
                battle_state=battle
            )
        else:
            # Basic damage calculation fallback
            damage = 10  # Simplified
            is_crit = False
            effectiveness = 1.0
            effect_msgs = []

        if self.held_item_manager:
            damage, held_msgs = self.held_item_manager.modify_damage(attacker, defender, move_data, damage)
            effect_msgs.extend(held_msgs)

        # Endure check: if this hit would KO and defender is under ENDURE, leave at 1 HP
        if damage >= defender.current_hp and hasattr(defender, 'status_manager') and 'endure' in getattr(defender.status_manager, 'volatile_statuses', {}):
            if defender.current_hp > 1:
                damage = defender.current_hp - 1
                effect_msgs.append(f"{defender.species_name} endured the hit!")
# Apply damage
        if damage > 0:
            defender.current_hp = max(0, defender.current_hp - damage)
        
        # Build message
        messages = []
        crit_text = " It's a critical hit!" if is_crit else ""
        effectiveness_text = ""
        if effectiveness > 1:
            effectiveness_text = " It's super effective!"
        elif effectiveness < 1 and effectiveness > 0:
            effectiveness_text = " It's not very effective..."
        elif effectiveness == 0:
            effectiveness_text = " It doesn't affect the target..."
        
        move_msg = f"{attacker.species_name} used {move_data['name']}!"
        if damage > 0:
            move_msg += f" ({damage} damage){crit_text}{effectiveness_text}"
        messages.append(move_msg)
        messages.extend(effect_msgs)

        if self.held_item_manager:
            post_msgs = self.held_item_manager.apply_after_damage(attacker, move_data, damage)
            messages.extend(post_msgs)
        
        # Check for faint / dazed state
        if defender.current_hp <= 0:
            # Determine which battler owns the defender
            defender_battler = battle.trainer if defender in battle.trainer.party else battle.opponent

            # Special handling for wild battles: wild Pokémon do not fully faint, they become "dazed"
            if battle.battle_type == BattleType.WILD and defender_battler == battle.opponent:
                # Set HP to 1 and mark dazed instead of true faint
                defender.current_hp = 1
                battle.wild_dazed = True
                battle.phase = 'DAZED'
                messages.append(f"The wild {defender.species_name} is dazed!")
            else:
                messages.append(f"{defender.species_name} fainted!")

                # For player's Pokemon fainting (non‑AI), they need to switch (if they have Pokemon left)
                if defender_battler == battle.trainer and not defender_battler.is_ai:
                    if defender_battler.has_usable_pokemon():
                        # Count usable Pokemon (excluding the fainted one)
                        usable_count = sum(1 for p in defender_battler.party if p.current_hp > 0 and p != defender)
                        if usable_count > 0:
                            messages.append("You must send out another Pokémon!")
                            battle.phase = 'FORCED_SWITCH'
                            battle.forced_switch_battler_id = defender_battler.battler_id
                        else:
                            self._check_battle_end(battle)

                # For AI-controlled trainers (NPCs), auto-send the next Pokémon before continuing
                elif defender_battler.is_ai and battle.battle_type in (BattleType.TRAINER, BattleType.PVP):
                    if defender_battler.has_usable_pokemon():
                        # Choose replacement index but DO NOT switch yet; queue it for after EOT
                        replacement_index = None
                        for idx, p in enumerate(defender_battler.party):
                            if p is defender:
                                continue
                            if getattr(p, 'current_hp', 0) > 0:
                                replacement_index = idx
                                break
                        if replacement_index is not None:
                            battle.phase = 'FORCED_SWITCH'
                            battle.forced_switch_battler_id = defender_battler.battler_id
                            battle.pending_ai_switch_index = replacement_index
                    else:
                        self._check_battle_end(battle)

        return {"messages": messages}

    
    
    def auto_switch_if_forced_ai(self, battle: BattleState) -> List[str]:
        """Perform queued AI forced switch AFTER end-of-turn and return narration."""
        if battle.phase != 'FORCED_SWITCH' or battle.forced_switch_battler_id is None:
            return []
        # Identify battler
        battler = battle.trainer if battle.trainer.battler_id == battle.forced_switch_battler_id else battle.opponent
        if not battler.is_ai:
            return []
        idx = battle.pending_ai_switch_index
        if idx is None:
            # Fallback: first healthy other than current
            current_idx = battler.active_positions[0] if battler.active_positions else None
            for i, p in enumerate(battler.party):
                if i == current_idx: 
                    continue
                if getattr(p, 'current_hp', 0) > 0:
                    idx = i
                    break
        if idx is None:
            return []
        result = self.force_switch(battle.battle_id, battler.battler_id, idx)
        return result.get('messages', [])
    
    
    def _apply_entry_hazards(self, battle: BattleState, battler: Battler, pokemon: Any) -> List[str]:
        """Apply field hazards to a newly-entered pokemon and return narration.
        Grounded check is simplified: Flying-type or Levitate ability -> not grounded.
        Implements: Stealth Rock, Spikes (1-3 layers), Toxic Spikes (1-2 layers), Sticky Web.
        """
        messages: List[str] = []

        # Which hazard map applies to this side? If this battler just entered, hazards were set by the opponent.
        hazards = battle.opponent_hazards if battler == battle.opponent else battle.trainer_hazards
        if not hazards:
            return messages

        # Helper: get types and simple grounded/ability
        types = [t.lower() for t in getattr(getattr(pokemon, 'species_data', {}), 'get', lambda *_: [])('types', [])] if False else [t.lower() for t in (getattr(pokemon, 'species_data', {}) or {}).get('types', [])]
        ability_name = getattr(pokemon, 'ability', None) or getattr(pokemon, 'ability_name', None)
        has_type = lambda t: t in types
        is_grounded = (not has_type('flying')) and (str(ability_name).lower() != 'levitate')

        # --- Stealth Rock ---
        if 'stealth_rock' in hazards and hasattr(pokemon, 'species_data'):
            chart = self.type_chart.chart if hasattr(self.type_chart, 'chart') else self.type_chart
            eff = 1.0
            if chart and 'rock' in chart:
                for t in types:
                    if t in chart['rock']:
                        eff *= chart['rock'][t]
            base = max(1, pokemon.max_hp // 8)
            dmg = max(1, int(base * eff)) if eff > 0 else 0
            if dmg > 0:
                pokemon.current_hp = max(0, pokemon.current_hp - dmg)
                messages.append(f"{pokemon.species_name} is hurt by Stealth Rock! (-{dmg} HP)")

        # --- Spikes (grounded only) ---
        if is_grounded and 'spikes' in hazards:
            layers = min(3, int(hazards.get('spikes', 1)))
            # 1 layer: 1/8, 2: 1/6, 3: 1/4
            if layers == 1:
                frac_num, frac_den = 1, 8
            elif layers == 2:
                frac_num, frac_den = 1, 6
            else:
                frac_num, frac_den = 1, 4
            dmg = max(1, (pokemon.max_hp * frac_num) // frac_den)
            pokemon.current_hp = max(0, pokemon.current_hp - dmg)
            messages.append(f"{pokemon.species_name} is hurt by Spikes! (-{dmg} HP)")

        # --- Toxic Spikes (grounded only) ---
        if 'toxic_spikes' in hazards and is_grounded:
            layers = min(2, int(hazards.get('toxic_spikes', 1)))
            # Poison-type absorbs the spikes (if grounded)
            if has_type('poison'):
                # Clear all layers from this side
                if battler == battle.opponent:
                    battle.opponent_hazards.pop('toxic_spikes', None)
                else:
                    battle.trainer_hazards.pop('toxic_spikes', None)
                messages.append(f"{pokemon.species_name} absorbed the Toxic Spikes!")
            else:
                # Steel-type and Poison-type can't be poisoned; Flying/Levitate handled by grounded
                if not has_type('steel'):
                    # Apply major status via status_manager if available
                    if hasattr(pokemon, 'status_manager'):
                        status = 'tox' if layers >= 2 else 'psn'
                        can_apply, _ = pokemon.status_manager.can_apply_status(status)
                        if can_apply:
                            success, msg = pokemon.status_manager.apply_status(status)
                            if success and msg:
                                messages.append(f"{pokemon.species_name} {msg}")

        # --- Sticky Web (grounded only): lower Speed by 1 stage ---
        if 'sticky_web' in hazards and is_grounded:
            if not hasattr(pokemon, 'stat_stages'):
                pokemon.stat_stages = {
                    'attack': 0, 'defense': 0, 'sp_attack': 0,
                    'sp_defense': 0, 'speed': 0, 'evasion': 0, 'accuracy': 0
                }
            pokemon.stat_stages['speed'] = max(-6, pokemon.stat_stages['speed'] - 1)
            messages.append(f"{pokemon.species_name}'s Speed fell! (-1)")

        return messages

        # Stealth Rock
        if 'stealth_rock' in hazards and hasattr(pokemon, 'species_data'):
            defender_types = [t.lower() for t in pokemon.species_data.get('types', [])]
            # Build chart
            chart = self.type_chart.chart if hasattr(self.type_chart, 'chart') else self.type_chart
            # Effectiveness of Rock vs defender types
            eff = 1.0
            if chart and 'rock' in chart:
                for t in defender_types:
                    if t in chart['rock']:
                        eff *= chart['rock'][t]
            base = max(1, pokemon.max_hp // 8)
            dmg = int(base * eff)
            if eff > 0 and dmg < 1:
                dmg = 1
            if dmg > 0:
                pokemon.current_hp = max(0, pokemon.current_hp - dmg)
                messages.append(f"{pokemon.species_name} is hurt by Stealth Rock! (-{dmg} HP)")
        return messages
    def _execute_switch(self, battle: BattleState, action: BattleAction, forced: bool = False) -> Dict:
        """Execute a Pokemon switch"""
        battler = battle.trainer if action.battler_id == battle.trainer.battler_id else battle.opponent

        # Get old and new Pokemon
        old_pokemon = battler.get_active_pokemon()[0]
        new_pokemon = battler.party[action.switch_to_position]

        # Switch
        battler.active_positions[0] = action.switch_to_position

        if self.held_item_manager:
            self.held_item_manager.clear_choice_lock(old_pokemon)

        # Trigger entry abilities
        messages = []
        if ENHANCED_SYSTEMS_AVAILABLE:
            ability_msgs = self.ability_handler.trigger_on_entry(new_pokemon, battle)
            messages.extend(ability_msgs)

        messages.extend(self._apply_entry_hazards(battle, battler, new_pokemon))

        if forced:
            lead_messages = [f"{battler.battler_name} sent out {new_pokemon.species_name}!"]
        else:
            lead_messages = [
                f"{battler.battler_name} withdrew {old_pokemon.species_name}!",
                f"Go, {new_pokemon.species_name}!"
            ]

        return {
            "messages": lead_messages + messages
        }

    def force_switch(self, battle_id: str, battler_id: int, switch_to_position: int) -> Dict:
        """Resolve a mandatory switch outside of normal turn order."""
        battle = self.active_battles.get(battle_id)
        if not battle:
            return {"error": "Battle not found"}

        if battle.phase != 'FORCED_SWITCH' or battle.forced_switch_battler_id != battler_id:
            return {"error": "No forced switch is pending"}

        battler = battle.trainer if battler_id == battle.trainer.battler_id else battle.opponent
        if switch_to_position < 0 or switch_to_position >= len(battler.party):
            return {"error": "Invalid party slot"}
        target = battler.party[switch_to_position]
        if getattr(target, 'current_hp', 0) <= 0:
            return {"error": "That Pokémon can't battle"}

        action = BattleAction(action_type='switch', battler_id=battler_id, switch_to_position=switch_to_position)
        result = self._execute_switch(battle, action, forced=True)

        battle.phase = 'WAITING_ACTIONS'
        battle.forced_switch_battler_id = None
        battle.pending_ai_switch_index = None
        battle.pending_actions.pop(str(battler_id), None)

        return result
    
    def _execute_item(self, battle: BattleState, action: BattleAction) -> Dict:
        """Execute an item use"""
        # TODO: Implement item system
        return {"messages": [f"Used {action.item_id}!"]}
    
    def _execute_flee(self, battle: BattleState, action: BattleAction) -> Dict:
        """Execute flee attempt"""
        if battle.battle_type != BattleType.WILD:
            return {"messages": ["Can't flee from a trainer battle!"]}
        
        # Simple flee chance for now
        if random.random() < 0.5:
            battle.is_over = True
            battle.fled = True
            battle.winner = None
            return {"messages": ["Got away safely!"]}
        else:
            return {"messages": ["Can't escape!"]}
    
    def _process_end_of_turn(self, battle: BattleState) -> List[str]:
        """Process end-of-turn effects"""
        messages = []
        
        if not ENHANCED_SYSTEMS_AVAILABLE:
            return []
        
        # Status damage
        for pokemon in battle.trainer.get_active_pokemon() + battle.opponent.get_active_pokemon():
            if hasattr(pokemon, 'status_manager'):
                status_msgs = pokemon.status_manager.apply_end_of_turn_effects(pokemon)
                messages.extend(status_msgs)
            if self.held_item_manager:
                messages.extend(self.held_item_manager.process_end_of_turn(pokemon))
        
        # Weather effects
        if battle.weather:
            for pokemon in battle.trainer.get_active_pokemon():
                weather_msg = self.ability_handler.apply_weather_damage(pokemon, battle.weather)
                if weather_msg:
                    messages.append(weather_msg)
                
                heal_msg = self.ability_handler.apply_weather_healing(pokemon, battle.weather)
                if heal_msg:
                    messages.append(heal_msg)
            
            for pokemon in battle.opponent.get_active_pokemon():
                weather_msg = self.ability_handler.apply_weather_damage(pokemon, battle.weather)
                if weather_msg:
                    messages.append(weather_msg)
                
                heal_msg = self.ability_handler.apply_weather_healing(pokemon, battle.weather)
                if heal_msg:
                    messages.append(heal_msg)
            
            # Decrement weather
            battle.weather_turns -= 1
            if battle.weather_turns <= 0:
                messages.append(f"The {battle.weather} subsided!")
                battle.weather = None
        
        # Terrain effects
        if battle.terrain:
            battle.terrain_turns -= 1
            if battle.terrain_turns <= 0:
                messages.append(f"The {battle.terrain} terrain faded!")
                battle.terrain = None

        return messages
    
    def _check_battle_end(self, battle: BattleState):
        """Check if battle should end"""
        trainer_has_pokemon = battle.trainer.has_usable_pokemon()
        opponent_has_pokemon = battle.opponent.has_usable_pokemon()
        
        if not trainer_has_pokemon and not opponent_has_pokemon:
            battle.is_over = True
            battle.winner = 'draw'
        elif not trainer_has_pokemon:
            battle.is_over = True
            battle.winner = 'opponent'
        elif not opponent_has_pokemon:
            battle.is_over = True
            battle.winner = 'trainer'
    
    # ========================
    # Battle Info Getters
    # ========================
    
    def get_battle(self, battle_id: str) -> Optional[BattleState]:
        """Get battle state"""
        return self.active_battles.get(battle_id)
    
    def end_battle(self, battle_id: str):
        """Clean up a finished battle"""
        if battle_id in self.active_battles:
            del self.active_battles[battle_id]


# ========================
# Command Parser
# ========================


# ========================
# Command Parser
# ========================

class CommandParser:
    """Parse natural language battle commands into BattleActions"""
    def __init__(self, moves_db):
        self.moves_db = moves_db

    def parse(self, command: str, active_pokemon: Any, battler_id: int) -> Optional[BattleAction]:
        """Parse a simple command into a BattleAction.

        Supports:
          - 'switch'/'swap'/'go' -> None (UI must pick target)
          - otherwise: tries to match a known move in user's move list
        """
        if not command:
            return None
        command = command.lower().strip()

        # Switch intent: handled by UI elsewhere
        if any(w in command for w in ('switch', 'swap', 'go ')):
            return None

        # Try to match one of the user's moves
        for mv in getattr(active_pokemon, 'moves', []):
            md = self.moves_db.get_move(mv.get('move_id'))
            if not md:
                continue
            move_name = (md.get('name') or md.get('id') or '').lower()
            move_id = md.get('id') or mv.get('move_id')
            if (move_name and move_name in command) or (move_id and move_id in command):
                return BattleAction(
                    action_type='move',
                    battler_id=battler_id,
                    move_id=move_id,
                    target_position=0
                )

        return None