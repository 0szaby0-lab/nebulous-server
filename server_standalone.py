#!/usr/bin/env python3
"""
Nebulous Private Server - Standalone HTTP & MongoDB Server
Runs on any platform (Android Termux / Linux / Render / VPS) with zero external dependencies,
or integrates with pymongo if installed.
"""

import os
import sys
import json
import base64
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# Configuration
PORT = int(os.environ.get("PORT", 8000))
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DB_NAME = os.environ.get("DB_NAME", "nebulous_db")
SERVER_PUBLIC_IP = os.environ.get("SERVER_PUBLIC_IP", "127.0.0.1")
SERVER_PUBLIC_UDP_PORT = int(os.environ.get("SERVER_PUBLIC_UDP_PORT", 25000))

# Database connection
mongo_client = None
db = None
players_col = None

if MONGODB_URI:
    try:
        import pymongo
        print(f"[*] Connecting to MongoDB: {MONGODB_URI[:25]}...")
        mongo_client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        db = mongo_client[DB_NAME]
        players_col = db["players"]
        players_col.create_index("account_id", unique=True)
        players_col.create_index("ticket")
        print("[+] Connected to MongoDB Atlas successfully!")
    except Exception as e:
        print(f"[!] MongoDB connection error: {e}. Falling back to in-memory storage.")
        players_col = None

# Fallback in-memory storage
_memory_players = {}
_next_account_id = 10001


def get_default_player(account_id: int, username: str = "Player"):
    return {
        "account_id": account_id,
        "username": username,
        "ticket": f"ticket_{account_id}_{int(datetime.now(timezone.utc).timestamp())}",
        "coins": 999999999,          # Max plasma
        "clan_coins": 999999999,
        "xp": 15000000,              # High level XP
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
        "profile_bio": "Nebulous Custom Server VIP Player",
        "custom_skin_id": 0,
        "owned_skins": list(range(1, 100)),
        "is_mod": True,
        "ads_removed": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


def db_get_or_create_player(username="Player", account_id=None):
    global _next_account_id
    if players_col is not None:
        try:
            query = {"account_id": account_id} if account_id else {"username": username}
            p = players_col.find_one(query)
            if p:
                return p
            last = players_col.find_one(sort=[("account_id", -1)])
            new_id = (last["account_id"] + 1) if last and "account_id" in last else 10001
            p = get_default_player(new_id, username)
            players_col.insert_one(p)
            return p
        except Exception as e:
            print(f"[!] MongoDB read error: {e}")

    if account_id and account_id in _memory_players:
        return _memory_players[account_id]
    for p in _memory_players.values():
        if p["username"] == username:
            return p
    new_id = account_id if account_id else _next_account_id
    _next_account_id = max(_next_account_id, new_id + 1)
    p = get_default_player(new_id, username)
    _memory_players[new_id] = p
    return p


def db_get_player_by_ticket(ticket):
    if not ticket:
        return None
    if players_col is not None:
        try:
            return players_col.find_one({"ticket": ticket})
        except Exception:
            pass
    for p in _memory_players.values():
        if p.get("ticket") == ticket:
            return p
    return None


def db_get_player_by_id(account_id):
    if players_col is not None:
        try:
            return players_col.find_one({"account_id": int(account_id)})
        except Exception:
            pass
    return _memory_players.get(int(account_id))


def db_update_player(account_id, updates):
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    if players_col is not None:
        try:
            players_col.update_one({"account_id": int(account_id)}, {"$set": updates})
            return
        except Exception as e:
            print(f"[!] MongoDB update error: {e}")
    if int(account_id) in _memory_players:
        _memory_players[int(account_id)].update(updates)


def db_get_leaderboard(limit=50):
    if players_col is not None:
        try:
            return list(players_col.find().sort("xp", -1).limit(limit))
        except Exception:
            pass
    s = sorted(_memory_players.values(), key=lambda x: x.get("xp", 0), reverse=True)
    return s[:limit]


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class NebulousHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/status":
            top = db_get_leaderboard(5)
            self._send_json({
                "status": "online",
                "server": "Nebulous Custom Master Server",
                "mongodb": players_col is not None,
                "game_node": f"{SERVER_PUBLIC_IP}:{SERVER_PUBLIC_UDP_PORT}",
                "top_players": [
                    {"id": p.get("account_id"), "name": p.get("username"), "xp": p.get("xp"), "coins": p.get("coins")}
                    for p in top
                ]
            })
        else:
            self._send_json({"status": "OK"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length).decode("utf-8", errors="ignore") if length > 0 else ""

        # Parse urlencoded params
        params = {}
        for k, v in urllib.parse.parse_qs(parsed.query).items():
            params[k] = v[0] if v else ""
        for k, v in urllib.parse.parse_qs(raw_body).items():
            params[k] = v[0] if v else ""

        # Also attempt json parsing
        if raw_body.startswith("{"):
            try:
                j = json.loads(raw_body)
                if isinstance(j, dict):
                    params.update(j)
            except Exception:
                pass

        endpoint = path.replace("/api/account/", "")
        ticket = params.get("Ticket", "")
        player = db_get_player_by_ticket(ticket)

        # 1. Login
        if endpoint in ["GoogleLogin", "FacebookLogin"]:
            uname = params.get("UserName") or params.get("Name") or "NebulousVIP"
            p = db_get_or_create_player(uname)
            self._send_json({
                "Ticket": p["ticket"],
                "UserName": p["username"],
                "Coins": int(p["coins"]),
                "profileFonts": p.get("profile_fonts", [0]),
                "AccountColors": p.get("account_colors", [1, 2, 3]),
                "ClanRole": p.get("clan_role", "NONE"),
                "ClanName": p.get("clan_name", ""),
                "ClanColors": p.get("clan_colors", []),
                "AmMod": True,
                "AdsRemoved": True,
                "BanUntilUtc": None
            })

        # 2. Daily CheckIn
        elif endpoint == "CheckIn":
            if not player:
                player = db_get_or_create_player("Player")
            new_coins = min(int(player.get("coins", 0)) + 10000, 999999999)
            db_update_player(player["account_id"], {"coins": new_coins})
            self._send_json({
                "CheckinReward": 10000,
                "Coins": new_coins,
                "RewardVideosRemaining": 10
            })

        # 3. Client Config
        elif endpoint == "GetClientConfig":
            self._send_json({
                "ServerAddressOverrides": {
                    "EU": SERVER_PUBLIC_IP,
                    "EUROPE": SERVER_PUBLIC_IP,
                    "US": SERVER_PUBLIC_IP,
                    "ASIA": SERVER_PUBLIC_IP
                },
                "ClanAllies": [],
                "ClanEnemies": [],
                "FriendAIDs": [],
                "UploadSizeLimitBytes": 1048576,
                "FreeTourneys": True,
                "FreeArenas": True,
                "PingPort": SERVER_PUBLIC_UDP_PORT,
                "AmMod": True,
                "SaleTypes": [0, 1, 2, 3, 4],
                "ExpiresUtc": "2030-01-01T00:00:00Z",
                "AnnouncementURL": "",
                "HasSpin": True,
                "HasCheckinReward": True,
                "NewMail": False,
                "BrandNewMail": False,
                "AdsRemoved": True,
                "EnableBannerAds": False,
                "EnableNyan": True,
                "SkinURLBase": "https://s3.amazonaws.com/simplicialsoftware.skins/"
            })

        # 4. Player Profile
        elif endpoint == "GetPlayerProfile":
            aid = params.get("accountID") or params.get("AccountId")
            p = db_get_player_by_id(aid) if aid else player
            if not p:
                p = db_get_or_create_player("Player")
            self._send_json({
                "plasma": int(p["coins"]),
                "profile": p.get("profile_bio", "Nebulous Custom EU Server Player"),
                "profileColors": p.get("account_colors", [1, 2, 3]),
                "profileFonts": p.get("profile_fonts", [0]),
                "customSkinID": p.get("custom_skin_id", 0),
                "setNamePrice": 0,
                "banned": False,
                "chatBanned": False,
                "arenaBanned": False,
                "relationship": "NONE",
                "profileVisibility": "PUBLIC",
                "profileBGColorEnabled": True,
                "profileBGColor": -16711936,
                "yearsPlayed": 5,
                "legend": True,
                "hero": True,
                "champion": True,
                "supporter": True,
                "conqueror": True,
                "tricky": True,
                "masterTamer": True,
                "tycoon": True,
                "YTURL": "",
                "Translator": False,
                "IsDiscordant": False,
                "IsMod": True,
                "IsYT": False
            })

        # 5. Player Stats
        elif endpoint == "GetPlayerStats":
            aid = params.get("AccountId") or params.get("accountID")
            p = db_get_player_by_id(aid) if aid else player
            if not p:
                p = db_get_or_create_player("Player")
            self._send_json({
                "AccountName": p["username"],
                "AccountColors": p.get("account_colors", [1, 2, 3]),
                "clanID": p.get("clan_id", -1),
                "ClanName": p.get("clan_name") or None,
                "ClanColors": p.get("clan_colors", []),
                "ClanRole": p.get("clan_role", "NONE"),
                "XP": int(p.get("xp", 15000000)),
                "DotsEaten": int(p.get("dots_eaten", 1250000)),
                "BlobsEaten": int(p.get("blobs_eaten", 45000)),
                "BlobsLost": int(p.get("blobs_lost", 120)),
                "BiggestBlob": int(p.get("biggest_blob", 85000)),
                "MassGained": int(p.get("mass_gained", 999999999)),
                "MassEjected": int(p.get("mass_ejected", 500000)),
                "EjectCount": int(p.get("eject_count", 15000)),
                "SplitCount": int(p.get("split_count", 18000)),
                "AverageScore": int(p.get("average_score", 25000)),
                "HighestScore": int(p.get("highest_score", 150000)),
                "TimesRestarted": int(p.get("times_restarted", 20)),
                "LongestLifeMS": int(p.get("longest_life_ms", 7200000)),
                "GamesWon": int(p.get("games_won", 500)),
                "MaxPlasmaChain": int(p.get("max_plasma_chain", 99)),
                "CoinsCollected": int(p.get("coins_collected", 500000)),
                "SMBHCollidedCount": int(p.get("smbh_collided_count", 25)),
                "SMBHEatenCount": int(p.get("smbh_eaten_count", 15)),
                "BHCollidedCount": int(p.get("bh_collided_count", 30)),
                "TBHCollidedCount": int(p.get("tbh_collided_count", 30)),
                "TimesTeleported": int(p.get("times_teleported", 150)),
                "PowerupsUsed": int(p.get("powerups_used", 600)),
                "TrickCount": int(p.get("trick_count", 120)),
                "Accolades": int(p.get("accolades", 250)),
                "SpecialObjects": [
                    {"Type": "Candies", "Count": 100},
                    {"Type": "Leaves", "Count": 100},
                    {"Type": "Presents", "Count": 100},
                    {"Type": "Nebulas", "Count": 100},
                    {"Type": "Eggs", "Count": 100},
                    {"Type": "Suns", "Count": 100},
                    {"Type": "Beads", "Count": 100},
                    {"Type": "Drops", "Count": 100},
                    {"Type": "Moons", "Count": 100},
                    {"Type": "Notes", "Count": 100},
                    {"Type": "Snowflakes", "Count": 100},
                    {"Type": "Pumpkins", "Count": 100}
                ],
                "AchievementsEarned": list(range(1, 40))
            })

        # 6. Purchase Prices (ALL FREE)
        elif endpoint == "GetPurchasePrices":
            if not player:
                player = db_get_or_create_player("Player")
            free_items = [{"ItemType": "Skin", "ItemID": i, "Price": 0} for i in range(1, 250)]
            self._send_json({
                "Coins": int(player["coins"]),
                "ClanCoins": int(player["clan_coins"]),
                "Items": free_items,
                "DailyFreeSkins": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            })

        # 7. Coin Purchase
        elif endpoint == "CoinPurchase":
            if not player:
                player = db_get_or_create_player("Player")
            itype = params.get("ItemType", "Skin")
            iid = int(params.get("ItemID", 1))
            owned = player.get("owned_skins", [])
            if iid not in owned:
                owned.append(iid)
                db_update_player(player["account_id"], {"owned_skins": owned})
            self._send_json({
                "ItemType": itype,
                "ItemID": iid,
                "Coins": int(player["coins"]),
                "ClanCoins": int(player["clan_coins"])
            })

        # 8. Skin IDs
        elif endpoint == "GetSkinIDs":
            if not player:
                player = db_get_or_create_player("Player")
            owned = player.get("owned_skins", list(range(1, 100)))
            self._send_json({
                "Skins": [{"ID": sid, "Status": "Purchased", "PurchaseCount": 1} for sid in owned],
                "Coins": int(player["coins"]),
                "ClanCoins": int(player["clan_coins"]),
                "skinMapPrice": 0,
                "purchasedSecondPet": True,
                "unlockedMultiskin": True,
                "LimitMultiple": 10.0
            })

        # 9. Master Server Ticket (JDKaYIIScQ)
        elif endpoint == "JDKaYIIScQ":
            ticket_b64 = base64.b64encode(os.urandom(16)).decode("ascii")
            self._send_json({
                "RezPlEVBeW": ticket_b64,
                "IP": SERVER_PUBLIC_IP,
                "PortA": SERVER_PUBLIC_UDP_PORT,
                "Nonce": 1
            })

        # 10. Active Effects
        elif endpoint in ["GetActiveEffects", "GetActiveClanEffects"]:
            self._send_json({
                "XPMultiplier": 5,
                "XPMultiplierDurationRemainingS": 86400,
                "ClanCoins": 999999999
            })

        # 11. Clan Stats
        elif endpoint == "GetClanStats":
            self._send_json({
                "ClanName": "Alpha",
                "ClanTag": "ALP",
                "clanID": 1,
                "ClanColors": [1, 2, 3],
                "ClanRole": "Leader",
                "XP": 50000000,
                "MembersCount": 1
            })

        # 12. Leaderboards
        elif endpoint in ["GetHighScores", "GetXPLeaderBoard"]:
            top = db_get_leaderboard(50)
            scores = [
                {"Rank": i, "AccountId": p["account_id"], "Name": p["username"], "XP": p["xp"], "ClanName": p.get("clan_name", "")}
                for i, p in enumerate(top, 1)
            ]
            self._send_json({"LeaderBoard": scores, "Scores": scores})

        # 13. Profile Updates
        elif endpoint in ["SetPlayerProfile", "SetPlayerProfileColors", "SetProfileVisibility"]:
            if player:
                updates = {}
                if "customSkinID" in params:
                    updates["custom_skin_id"] = int(params["customSkinID"])
                if "profile" in params:
                    updates["profile_bio"] = params["profile"]
                if updates:
                    db_update_player(player["account_id"], updates)
            self._send_json({"status": "OK"})

        # Fallback for all other endpoints
        else:
            self._send_json({
                "status": "OK",
                "Coins": 999999999,
                "ClanCoins": 999999999,
                "Items": [],
                "Packs": []
            })

    def log_message(self, format, *args):
        # Concise logging
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]} -> {args[1]}")


def main():
    print(f"==================================================")
    print(f" Nebulous Custom Master Server (EU / Global)")
    print(f" Port: {PORT}")
    print(f" MongoDB: {'Connected' if players_col is not None else 'Local Mode'}")
    print(f"==================================================")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), NebulousHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
