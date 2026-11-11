import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger("nebulous_db")

MONGODB_URI = os.environ.get("MONGODB_URI", "")
DB_NAME = os.environ.get("DB_NAME", "nebulous_db")

motor_client = None
db = None
players_col = None
clans_col = None

# In-memory fallback if MongoDB is not configured or offline
_memory_players: Dict[int, Dict[str, Any]] = {}
_memory_clans: Dict[int, Dict[str, Any]] = {}
_next_account_id: int = 10001


def get_default_player(account_id: int, username: str = "Player") -> Dict[str, Any]:
    return {
        "account_id": account_id,
        "username": username,
        "ticket": f"ticket_{account_id}_{int(datetime.now(timezone.utc).timestamp())}",
        "coins": 999999999,      # Plasma / coins (free / maxed)
        "clan_coins": 999999999,
        "xp": 15000000,          # High level XP
        "level": 100,
        "dots_eaten": 1250000,
        "blobs_eaten": 45000,
        "blobs_lost": 120,
        "biggest_blob": 85000,
        "mass_gained": 999999999,
        "mass_ejected": 500000,
        "eject_count": 15000,
        "split_count": 18000,
        "average_score": 25000,
        "highest_score": 150000,
        "times_restarted": 20,
        "longest_life_ms": 7200000,
        "games_won": 500,
        "max_plasma_chain": 99,
        "coins_collected": 500000,
        "smbh_collided_count": 25,
        "smbh_eaten_count": 15,
        "bh_collided_count": 30,
        "tbh_collided_count": 30,
        "times_teleported": 150,
        "powerups_used": 600,
        "trick_count": 120,
        "accolades": 250,
        "clan_id": -1,
        "clan_name": "",
        "clan_role": "NONE",
        "clan_colors": [],
        "account_colors": [1, 2, 3],
        "profile_fonts": [0],
        "profile_bio": "Nebulous Custom EU Master Server Player",
        "custom_skin_id": 0,
        "owned_skins": list(range(1, 100)),  # Unlocks first 100 skins!
        "is_mod": True,
        "ads_removed": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


async def init_db():
    global motor_client, db, players_col, clans_col
    if MONGODB_URI:
        try:
            try:
                import dns.resolver
                dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
                dns.resolver.default_resolver.nameservers = ['8.8.8.8', '1.1.1.1']
            except Exception:
                pass

            import motor.motor_asyncio
            logger.info("Connecting to MongoDB Atlas...")
            motor_client = motor.motor_asyncio.AsyncIOMotorClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=5000
            )
            db = motor_client[DB_NAME]
            players_col = db["players"]
            clans_col = db["clans"]
            # Create indexes
            await players_col.create_index("account_id", unique=True)
            await players_col.create_index("ticket")
            await players_col.create_index("username")
            logger.info("Successfully connected to MongoDB!")
            return
        except Exception as e:
            logger.warning(f"Could not connect to MongoDB ({e}). Falling back to local in-memory storage.")
            motor_client = None
            db = None
            players_col = None
    else:
        logger.info("MONGODB_URI not set. Running in local in-memory persistence mode.")


async def get_or_create_player(username: str = "Player", account_id: Optional[int] = None) -> Dict[str, Any]:
    global _next_account_id

    if players_col is not None:
        try:
            query = {}
            if account_id:
                query["account_id"] = account_id
            else:
                query["username"] = username

            player = await players_col.find_one(query)
            if player:
                return player

            # Generate new account_id
            last = await players_col.find_one(sort=[("account_id", -1)])
            new_id = (last["account_id"] + 1) if last and "account_id" in last else 10001
            player_data = get_default_player(new_id, username)
            await players_col.insert_one(player_data)
            return player_data
        except Exception as e:
            logger.error(f"MongoDB query error: {e}")

    # Fallback storage
    if account_id and account_id in _memory_players:
        return _memory_players[account_id]

    for p in _memory_players.values():
        if p["username"] == username:
            return p

    new_id = account_id if account_id else _next_account_id
    _next_account_id = max(_next_account_id, new_id + 1)
    player_data = get_default_player(new_id, username)
    _memory_players[new_id] = player_data
    return player_data


async def get_player_by_id(account_id: int) -> Optional[Dict[str, Any]]:
    if players_col is not None:
        try:
            return await players_col.find_one({"account_id": account_id})
        except Exception as e:
            logger.error(f"MongoDB query error: {e}")
    return _memory_players.get(account_id)


async def get_player_by_ticket(ticket: str) -> Optional[Dict[str, Any]]:
    if not ticket:
        return None
    if players_col is not None:
        try:
            player = await players_col.find_one({"ticket": ticket})
            if player:
                return player
        except Exception as e:
            logger.error(f"MongoDB query error: {e}")

    for p in _memory_players.values():
        if p.get("ticket") == ticket:
            return p
    return None


async def update_player(account_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    if players_col is not None:
        try:
            await players_col.update_one({"account_id": account_id}, {"$set": updates})
            return await players_col.find_one({"account_id": account_id})
        except Exception as e:
            logger.error(f"MongoDB update error: {e}")

    if account_id in _memory_players:
        _memory_players[account_id].update(updates)
        return _memory_players[account_id]
    return None


async def get_leaderboard(limit: int = 50) -> List[Dict[str, Any]]:
    if players_col is not None:
        try:
            cursor = players_col.find().sort("xp", -1).limit(limit)
            return await cursor.to_list(length=limit)
        except Exception as e:
            logger.error(f"MongoDB leaderboard error: {e}")

    sorted_players = sorted(_memory_players.values(), key=lambda p: p.get("xp", 0), reverse=True)
    return sorted_players[:limit]
