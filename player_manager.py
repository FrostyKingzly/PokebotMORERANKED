"""
Player Manager - Handles trainer profile operations
"""

import json
from pathlib import Path
from typing import Optional, Dict, List
from database import PlayerDatabase
from models import Trainer, Pokemon


class PlayerManager:
    """Manages player/trainer data"""
    
    def __init__(self, db_path: str = "data/players.db", species_db=None, items_db=None):
        self.db = PlayerDatabase(db_path)
        self.species_db = species_db
        self.items_db = items_db
        self.inventory_cache_path = Path("config/player_inventory.json")
        self._inventory_cache = self._load_inventory_cache()

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------
    def _load_inventory_cache(self) -> Dict[str, Dict[str, int]]:
        if self.inventory_cache_path.exists():
            try:
                with open(self.inventory_cache_path, "r", encoding="utf-8") as cache_file:
                    data = json.load(cache_file)
                    if isinstance(data, dict):
                        return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_inventory_cache(self):
        self.inventory_cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.inventory_cache_path, "w", encoding="utf-8") as cache_file:
            json.dump(self._inventory_cache, cache_file, indent=2)

    def _set_cached_quantity(self, discord_user_id: int, item_id: str, quantity: int):
        user_key = str(discord_user_id)
        if quantity <= 0:
            if user_key in self._inventory_cache and item_id in self._inventory_cache[user_key]:
                self._inventory_cache[user_key].pop(item_id, None)
                if not self._inventory_cache[user_key]:
                    self._inventory_cache.pop(user_key, None)
        else:
            self._inventory_cache.setdefault(user_key, {})[item_id] = quantity
        self._save_inventory_cache()

    def _bump_cached_quantity(self, discord_user_id: int, item_id: str, delta: int):
        user_key = str(discord_user_id)
        current = self._inventory_cache.get(user_key, {}).get(item_id, 0)
        self._set_cached_quantity(discord_user_id, item_id, current + delta)

    def _rows_to_inventory(self, rows: List[Dict]) -> List[Dict]:
        inventory = []
        for row in rows:
            if row.get("quantity", 0) > 0:
                inventory.append(row)
                self._set_cached_quantity(row["discord_user_id"], row["item_id"], row["quantity"])
        return inventory
    
    # ============================================================
    # TRAINER OPERATIONS
    # ============================================================
    
    def get_player(self, discord_user_id: int) -> Optional[Trainer]:
        """Get a trainer profile"""
        data = self.db.get_trainer(discord_user_id)
        if data:
            return Trainer(data)
        return None
    
    def player_exists(self, discord_user_id: int) -> bool:
        """Check if player has registered"""
        return self.db.trainer_exists(discord_user_id)
    
    def create_player(self, discord_user_id: int, trainer_name: str,
                     avatar_url: str = None, boon_stat: str = None,
                     bane_stat: str = None) -> bool:
        """
        Create a new trainer profile
        
        Args:
            discord_user_id: Discord user ID
            trainer_name: Chosen trainer name
            avatar_url: Avatar image URL
            boon_stat: Social stat to boost (Rank 2)
            bane_stat: Social stat to lower (Rank 0)
        
        Returns:
            True if created successfully, False if already exists
        """
        return self.db.create_trainer(
            discord_user_id=discord_user_id,
            trainer_name=trainer_name,
            avatar_url=avatar_url,
            boon_stat=boon_stat,
            bane_stat=bane_stat
        )
    
    def update_player(self, discord_user_id: int, **kwargs):
        """Update trainer fields"""
        self.db.update_trainer(discord_user_id, **kwargs)
        
    def update_location(self, discord_id: int, location_id: str) -> bool:
        """
        Update player's current location
        
        Args:
            discord_id: Discord user ID
            location_id: New location identifier
            
        Returns:
            True if successful, False if player not found
        """
        with self.get_session() as session:
            trainer = session.query(Trainer).filter_by(discord_user_id=discord_id).first()
            if trainer:
                trainer.current_location_id = location_id
                session.commit()
                return True
            return False

    def delete_player(self, discord_user_id: int) -> bool:
        """Delete a trainer profile and all associated data."""
        conn = self.db.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM pokemon_instances WHERE owner_discord_id = ?",
                (discord_user_id,)
            )
            cursor.execute(
                "DELETE FROM inventory WHERE discord_user_id = ?",
                (discord_user_id,)
            )
            cursor.execute(
                "DELETE FROM pokedex WHERE discord_user_id = ?",
                (discord_user_id,)
            )
            cursor.execute(
                "DELETE FROM trainers WHERE discord_user_id = ?",
                (discord_user_id,)
            )
            deleted = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

        user_key = str(discord_user_id)
        if user_key in self._inventory_cache:
            self._inventory_cache.pop(user_key, None)
            self._save_inventory_cache()

        return deleted > 0
    
    # ============================================================
    # POKEMON OPERATIONS
    # ============================================================
    
    def add_pokemon_to_party(self, pokemon: Pokemon, position: int = None) -> str:
        """
        Add a Pokemon to trainer's party
        
        Args:
            pokemon: Pokemon object to add
            position: Party slot (0-5), auto if None
        
        Returns:
            Pokemon ID
        """
        # Get current party
        party = self.get_party(pokemon.owner_discord_id)
        
        if len(party) >= 6:
            # Party full, add to box instead
            return self.add_pokemon_to_box(pokemon)
        
        # Set party position
        if position is None:
            position = len(party)
        
        pokemon.in_party = True
        pokemon.party_position = position
        
        return self.db.add_pokemon(pokemon.to_dict())
    
    def add_pokemon_to_box(self, pokemon: Pokemon) -> str:
        """Add a Pokemon to storage box"""
        boxes = self.get_boxes(pokemon.owner_discord_id)
        
        pokemon.in_party = False
        pokemon.box_position = len(boxes)
        
        return self.db.add_pokemon(pokemon.to_dict())
    
    def get_pokemon(self, pokemon_id: str) -> Optional[Dict]:
        """Get a specific Pokemon by ID"""
        return self.db.get_pokemon(pokemon_id)
    
    def get_party(self, discord_user_id: int) -> List[Dict]:
        """Get trainer's party"""
        return self.db.get_trainer_party(discord_user_id)

    def get_players_in_location(self, location_id: str, exclude_user_id: Optional[int] = None) -> List[Trainer]:
        """Return Trainer objects for everyone currently in the given location."""
        if not location_id:
            return []

        rows = self.db.get_players_in_location(location_id)
        trainers: List[Trainer] = []
        for row in rows:
            discord_id = row.get('discord_user_id')
            if exclude_user_id is not None and discord_id == exclude_user_id:
                continue
            trainers.append(Trainer(row))
        return trainers

    def get_boxes(self, discord_user_id: int) -> List[Dict]:
        """Get trainer's boxed Pokemon"""
        return self.db.get_trainer_boxes(discord_user_id)
    
    def get_all_pokemon(self, discord_user_id: int) -> List[Dict]:
        """Get all Pokemon owned by trainer"""
        return self.get_party(discord_user_id) + self.get_boxes(discord_user_id)

    def heal_party(self, discord_user_id: int) -> int:
        """Fully restore every Pokémon currently in the trainer's party."""
        return self.db.heal_party(discord_user_id)
    
    # ============================================================
    # POKEDEX OPERATIONS
    # ============================================================
    
    def add_pokedex_seen(self, discord_user_id: int, species_dex_number: int):
        """Mark a species as seen in Pokedex"""
        self.db.add_pokedex_entry(discord_user_id, species_dex_number)
    
    def get_pokedex(self, discord_user_id: int) -> List[int]:
        """Get list of seen species"""
        return self.db.get_pokedex(discord_user_id)
    
    def has_seen_species(self, discord_user_id: int, species_dex_number: int) -> bool:
        """Check if trainer has seen this species"""
        seen = self.get_pokedex(discord_user_id)
        return species_dex_number in seen
    
    # ============================================================
    # INVENTORY OPERATIONS
    # ============================================================
    
    def get_inventory(self, discord_user_id: int) -> List[Dict]:
        """Get trainer's inventory"""
        rows = self.db.get_inventory(discord_user_id)
        inventory = self._rows_to_inventory(rows)
        if inventory:
            return inventory

        cached_items = self._inventory_cache.get(str(discord_user_id), {})
        return [
            {"discord_user_id": discord_user_id, "item_id": item_id, "quantity": qty}
            for item_id, qty in cached_items.items()
            if qty > 0
        ]
    
    def add_item(self, discord_user_id: int, item_id: str, quantity: int = 1):
        """Add item(s) to trainer's inventory"""
        self.db.add_item(discord_user_id, item_id, quantity)
        self._bump_cached_quantity(discord_user_id, item_id, quantity)

    def remove_item(self, discord_user_id: int, item_id: str, quantity: int = 1) -> bool:
        """Remove item(s) from trainer's inventory. Returns True if successful."""
        success = self.db.remove_item(discord_user_id, item_id, quantity)
        if success:
            self._bump_cached_quantity(discord_user_id, item_id, -quantity)
        return success
    
    def get_item_quantity(self, discord_user_id: int, item_id: str) -> int:
        """Get quantity of a specific item"""
        return self.db.get_item_quantity(discord_user_id, item_id)
    
    # ============================================================
    # POKEMON MANAGEMENT OPERATIONS
    # ============================================================
    
    def deposit_pokemon(self, discord_user_id: int, pokemon_id: str) -> tuple[bool, str]:
        """
        Move Pokemon from party to box
        Returns: (success, message)
        """
        pokemon = self.get_pokemon(pokemon_id)
        
        if not pokemon:
            return False, "[X] Pokemon not found!"
        
        if pokemon['owner_discord_id'] != discord_user_id:
            return False, "[X] This isn't your Pokemon!"
        
        if not pokemon.get('in_party'):
            return False, "[X] This Pokemon is already in a box!"
        
        party = self.get_party(discord_user_id)
        
        if len(party) <= 1:
            return False, "[X] You must have at least one Pokemon in your party!"
        
        # Get current box count for position
        boxes = self.get_boxes(discord_user_id)
        box_position = len(boxes)
        
        # Update Pokemon
        self.db.update_pokemon(pokemon_id, {
            'in_party': 0,
            'party_position': None,
            'box_position': box_position
        })
        
        # Reorder party positions
        for i, p in enumerate(party):
            if p['pokemon_id'] != pokemon_id:
                self.db.update_pokemon(p['pokemon_id'], {'party_position': i})
        
        species_name = pokemon.get('nickname', '')
        if not species_name and self.species_db:
            # Get species name from database
            species_data = self.species_db.get_species(pokemon['species_dex_number'])
            species_name = species_data['name'] if species_data else "Pokemon"
        elif not species_name:
            species_name = "Pokemon"
        
        return True, f"[OK] **{species_name}** was moved to the box!"
    
    def withdraw_pokemon(self, discord_user_id: int, pokemon_id: str) -> tuple[bool, str]:
        """
        Move Pokemon from box to party
        Returns: (success, message)
        """
        pokemon = self.get_pokemon(pokemon_id)
        
        if not pokemon:
            return False, "[X] Pokemon not found!"
        
        if pokemon['owner_discord_id'] != discord_user_id:
            return False, "[X] This isn't your Pokemon!"
        
        if pokemon.get('in_party'):
            return False, "[X] This Pokemon is already in your party!"
        
        party = self.get_party(discord_user_id)
        
        if len(party) >= 6:
            return False, "[X] Your party is full! Deposit a Pokemon first."
        
        party_position = len(party)
        
        # Update Pokemon
        self.db.update_pokemon(pokemon_id, {
            'in_party': 1,
            'party_position': party_position,
            'box_position': None
        })
        
        # Reorder box positions
        boxes = self.get_boxes(discord_user_id)
        for i, p in enumerate(boxes):
            self.db.update_pokemon(p['pokemon_id'], {'box_position': i})
        
        species_name = pokemon.get('nickname', '')
        if not species_name and self.species_db:
            species_data = self.species_db.get_species(pokemon['species_dex_number'])
            species_name = species_data['name'] if species_data else "Pokemon"
        elif not species_name:
            species_name = "Pokemon"
        
        return True, f"[OK] **{species_name}** was added to your party!"
    
    def release_pokemon(self, discord_user_id: int, pokemon_id: str) -> tuple[bool, str]:
        """
        Release a Pokemon permanently
        Returns: (success, message)
        """
        pokemon = self.get_pokemon(pokemon_id)
        
        if not pokemon:
            return False, "[X] Pokemon not found!"
        
        if pokemon['owner_discord_id'] != discord_user_id:
            return False, "[X] This isn't your Pokemon!"
        
        # Check if this is the last Pokemon in party
        if pokemon.get('in_party'):
            party = self.get_party(discord_user_id)
            if len(party) <= 1:
                return False, "[X] You cannot release your last Pokemon!"
        
        species_name = pokemon.get('nickname', '')
        if not species_name and self.species_db:
            species_data = self.species_db.get_species(pokemon['species_dex_number'])
            species_name = species_data['name'] if species_data else "Pokemon"
        elif not species_name:
            species_name = "Pokemon"
        
        # Delete Pokemon
        self.db.delete_pokemon(pokemon_id)
        
        # Reorder positions
        if pokemon.get('in_party'):
            party = self.get_party(discord_user_id)
            for i, p in enumerate(party):
                self.db.update_pokemon(p['pokemon_id'], {'party_position': i})
        else:
            boxes = self.get_boxes(discord_user_id)
            for i, p in enumerate(boxes):
                self.db.update_pokemon(p['pokemon_id'], {'box_position': i})
        
        return True, f"[OK] **{species_name}** was released. Farewell!"
    
    def set_nickname(self, discord_user_id: int, pokemon_id: str, nickname: str) -> tuple[bool, str]:
        """
        Set Pokemon nickname
        Returns: (success, message)
        """
        pokemon = self.get_pokemon(pokemon_id)
        
        if not pokemon:
            return False, "[X] Pokemon not found!"
        
        if pokemon['owner_discord_id'] != discord_user_id:
            return False, "[X] This isn't your Pokemon!"
        
        self.db.update_pokemon(pokemon_id, {'nickname': nickname})
        
        if nickname:
            return True, f"[OK] Nickname changed to **{nickname}**!"
        else:
            if self.species_db:
                species_data = self.species_db.get_species(pokemon['species_dex_number'])
                species_name = species_data['name'] if species_data else "Pokemon"
            else:
                species_name = "Pokemon"
            return True, f"[OK] Nickname reset to **{species_name}**!"
    
    def give_item(self, discord_user_id: int, pokemon_id: str, item_id: str) -> tuple[bool, str]:
        """
        Give held item to Pokemon
        Returns: (success, message)
        """
        pokemon = self.get_pokemon(pokemon_id)
        
        if not pokemon:
            return False, "[X] Pokemon not found!"
        
        if pokemon['owner_discord_id'] != discord_user_id:
            return False, "[X] This isn't your Pokemon!"
        
        # Check if player has the item
        if self.get_item_quantity(discord_user_id, item_id) <= 0:
            return False, "[X] You don't have this item!"
        
        # Check if Pokemon is already holding an item
        if pokemon.get('held_item'):
            return False, "[X] This Pokemon is already holding an item! Take it first."
        
        # Give item to Pokemon and remove from inventory
        self.db.update_pokemon(pokemon_id, {'held_item': item_id})
        self.remove_item(discord_user_id, item_id, 1)
        
        if self.items_db:
            item_data = self.items_db.get_item(item_id)
            item_name = item_data['name'] if item_data else item_id
        else:
            item_name = item_id
        
        return True, f"[OK] Gave **{item_name}** to Pokemon!"
    
    def take_item(self, discord_user_id: int, pokemon_id: str) -> tuple[bool, str]:
        """
        Take held item from Pokemon
        Returns: (success, message)
        """
        pokemon = self.get_pokemon(pokemon_id)
        
        if not pokemon:
            return False, "[X] Pokemon not found!"
        
        if pokemon['owner_discord_id'] != discord_user_id:
            return False, "[X] This isn't your Pokemon!"
        
        if not pokemon.get('held_item'):
            return False, "[X] This Pokemon isn't holding an item!"
        
        item_id = pokemon['held_item']
        
        # Remove item from Pokemon and add to inventory
        self.db.update_pokemon(pokemon_id, {'held_item': None})
        self.add_item(discord_user_id, item_id, 1)
        
        if self.items_db:
            item_data = self.items_db.get_item(item_id)
            item_name = item_data['name'] if item_data else item_id
        else:
            item_name = item_id
        
        return True, f"[OK] Took **{item_name}** from Pokemon!"
    
    def swap_party_positions(self, discord_user_id: int, pokemon_id_1: str, pokemon_id_2: str) -> tuple[bool, str]:
        """
        Swap positions of two Pokemon in party
        Returns: (success, message)
        """
        pokemon1 = self.get_pokemon(pokemon_id_1)
        pokemon2 = self.get_pokemon(pokemon_id_2)
        
        if not pokemon1 or not pokemon2:
            return False, "[X] One or both Pokemon not found!"
        
        if pokemon1['owner_discord_id'] != discord_user_id or pokemon2['owner_discord_id'] != discord_user_id:
            return False, "[X] These aren't your Pokemon!"
        
        if not pokemon1.get('in_party') or not pokemon2.get('in_party'):
            return False, "[X] Both Pokemon must be in your party!"
        
        # Swap positions
        pos1 = pokemon1['party_position']
        pos2 = pokemon2['party_position']
        
        self.db.update_pokemon(pokemon_id_1, {'party_position': pos2})
        self.db.update_pokemon(pokemon_id_2, {'party_position': pos1})
        
        return True, "[OK] Pokemon positions swapped!"

    def reorder_party(self, discord_user_id: int, ordered_pokemon_ids: list[str]) -> tuple[bool, str]:
        """Set an explicit order for the trainer's party.

        ordered_pokemon_ids should contain each of the current party's Pokémon IDs exactly once.
        Positions will be assigned 0..N-1 in the order provided.
        """
        # Load current party to validate IDs and ownership
        party = self.get_party(discord_user_id)
        if not party:
            return False, "[X] You don't have any Pokémon in your party!"

        current_ids = {str(p['pokemon_id']) for p in party}
        provided_ids = [str(pid) for pid in ordered_pokemon_ids]

        if len(provided_ids) != len(current_ids):
            return False, "[X] Please select every Pokémon in your party exactly once."

        if set(provided_ids) != current_ids:
            return False, "[X] One or more selected Pokémon don't match your current party."

        # Apply new positions
        for idx, pid in enumerate(provided_ids):
            self.db.update_pokemon(pid, {'party_position': idx})

        return True, "[OK] Party order updated!"

