import os
import base64
import struct
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import database as db

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("nebulous_server")

# Game server configuration
SERVER_PUBLIC_IP = os.environ.get("SERVER_PUBLIC_IP", "nebulous-server.onrender.com")
SERVER_PUBLIC_UDP_PORT = int(os.environ.get("SERVER_PUBLIC_UDP_PORT", 25000))
APP_DOMAIN = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")


def build_game_config(client_id: int) -> bytes:
    bio = io.BytesIO()
    # byte 0: packet_type = 0x2b (43), bytes 1..4: client_id
    bio.write(struct.pack(">Bi", 0x2b, client_id))
    # int c (room / config ID = 1)
    bio.write(struct.pack(">i", 1))
    # 10 booleans (d, p, s, v, y, B, E, S, U, W) all False
    bio.write(b"\x00" * 10)
    return bio.getvalue()


def build_game_state(client_id: int) -> bytes:
    bio = io.BytesIO()
    # 0x4b (Lob/n0;)
    bio.write(struct.pack(">Bi", 0x4b, client_id))
    # g (int = 1, must match f=1 in ConnectResult2)
    bio.write(struct.pack(">i", 1))
    # zoom / scale (float = 1000.0)
    bio.write(struct.pack(">f", 1000.0))
    # h (byte: players count = 0)
    bio.write(b"\x00")
    # i (byte: blobs count = 0)
    bio.write(b"\x00")
    # k (short = 0)
    bio.write(struct.pack(">h", 0))
    # j (short = 0)
    bio.write(struct.pack(">h", 0))
    # m (byte: food count = 0)
    bio.write(b"\x00")
    # l (byte: virus count = 0)
    bio.write(b"\x00")
    return bio.getvalue()


def build_leaderboard(client_id: int) -> bytes:
    bio = io.BytesIO()
    # 0x1f (Lob/x0;)
    bio.write(struct.pack(">Bi", 0x1f, client_id))
    # scale (float = 1.0)
    bio.write(struct.pack(">f", 1.0))
    # g (byte: count = 0)
    bio.write(b"\x00")
    # i (int = 0)
    bio.write(struct.pack(">i", 0))
    return bio.getvalue()


def build_custom_rooms(client_id: int, offset: int = 0) -> bytes:
    bio = io.BytesIO()
    bio.write(struct.pack(">Bi", 0x2d, client_id))
    if offset == 0:
        bio.write(struct.pack(">B", 4)) # 4 rooms
        # Room 1: FFA (mode=1, flags=0, region_diff=2 [EU], cur=1)
        bio.write(struct.pack(">iBBBh", 1, 1, 0, 2, 1))
        # Room 2: Teams (mode=2, flags=0, region_diff=2 [EU], cur=1)
        bio.write(struct.pack(">iBBBh", 2, 2, 0, 2, 1))
        # Room 3: Experimental (mode=3, flags=0, region_diff=2 [EU], cur=1)
        bio.write(struct.pack(">iBBBh", 3, 3, 0, 2, 1))
        # Room 4: Mayhem (mode=4, flags=0, region_diff=2 [EU], cur=1)
        bio.write(struct.pack(">iBBBh", 4, 4, 0, 2, 1))
    else:
        bio.write(struct.pack(">B", 0)) # No more rooms
    return bio.getvalue()


def build_room_query_response(client_id: int, token: int = 1) -> bytes:
    return struct.pack(">BiBiiiii", 0x2c, client_id, 4, 1, 2, 3, 4, token)


def build_clan_info(client_id: int) -> bytes:
    bio = io.BytesIO()
    bio.write(struct.pack(">Bi", 0x43, client_id))
    bio.write(b"\x00\x00") # empty string UTF
    bio.write(struct.pack(">iBB", 0, 0, 0)) # h=0, byte=0, count=0
    return bio.getvalue()


def build_tournament_list(client_id: int) -> bytes:
    return struct.pack(">BiBi", 0x18, client_id, 0, 0)


def build_clan_status(client_id: int) -> bytes:
    return struct.pack(">Biib", 0x12, client_id, -1, 0)


class ClientSession:
    def __init__(self, addr, client_id):
        self.addr = addr
        self.client_id = client_id
        import time
        self.last_seen = time.time()
        self.seq = 0
        self.active = True


class NebulousUDPServerProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.sessions = {}
        self.update_task = None

    def connection_made(self, transport):
        self.transport = transport
        logger.info(f"Nebulous European UDP Game Node active on port {SERVER_PUBLIC_UDP_PORT}")
        self.update_task = asyncio.create_task(self.game_loop())

    def datagram_received(self, data, addr):
        if not data:
            return
        import time
        pkt_type = data[0]

        # 1. PING (0x75)
        if pkt_type == 0x75 or (len(data) == 4 and data[0] == 0x75):
            self.transport.sendto(b"\x76\x00\x00\x00\x00", addr)
            return

        # 2. CHALLENGE (0x85)
        if pkt_type in (0x85, 256 - 0x7B):
            self.transport.sendto(b"\x86\x01\x02\x03\x04", addr)
            logger.info(f"Challenge received from {addr}, sent 0x86 cookie")
            return

        # 3. CONNECT (0x7d)
        if pkt_type == 0x7D:
            try:
                cookie_len = data[5] if len(data) > 5 else 4
                cid_offset = 6 + cookie_len + 2
                client_id = 10001
                if len(data) >= cid_offset + 4:
                    client_id = struct.unpack(">i", data[cid_offset:cid_offset + 4])[0]

                logger.info(f"Connect handshake from {addr} (client_id={client_id})")
                session = ClientSession(addr, client_id)
                self.sessions[addr] = session

                # ConnectResult2 (SUCCESS)
                resp = struct.pack(">BiBiiiifb", 1, client_id, 0, 100, 100, 1, 0, 1.0, 1)
                self.transport.sendto(resp, addr)

                # GameConfig (0x2b)
                config_pkt = build_game_config(client_id)
                self.transport.sendto(config_pkt, addr)

                # Initial GameState (0x4b)
                state_pkt = build_game_state(client_id)
                self.transport.sendto(state_pkt, addr)

                # Initial Leaderboard (0x1f)
                lb_pkt = build_leaderboard(client_id)
                self.transport.sendto(lb_pkt, addr)
                logger.info(f"Sent ConnectResult2, GameConfig, GameState & Leaderboard to {addr}")
            except Exception as e:
                logger.error(f"Connect packet error: {e}")
            return

        # 4. CUSTOM ROOMS REQUEST (0x21)
        if pkt_type == 0x21:
            offset = 0
            client_id = 10001
            if len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]
            elif addr in self.sessions:
                client_id = self.sessions[addr].client_id
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            if len(data) >= 7:
                offset = struct.unpack(">h", data[5:7])[0]
            resp = build_custom_rooms(client_id, offset)
            self.transport.sendto(resp, addr)
            logger.info(f"Responded to 0x21 with room list (0x2d) to {addr} (client_id={client_id}, offset={offset})")
            return

        # 4b. ROOM QUERY (0x2c)
        if pkt_type == 0x2C:
            client_id = 10001
            token = 1
            if len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]
            if len(data) >= 10:
                token = struct.unpack(">i", data[-4:])[0]
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            resp = build_room_query_response(client_id, token)
            self.transport.sendto(resp, addr)
            return

        # 5. TEAM ROOMS / CLAN WAR INFO (0x42)
        if pkt_type == 0x42:
            client_id = 10001
            if len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]
            elif addr in self.sessions:
                client_id = self.sessions[addr].client_id
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            resp = build_clan_info(client_id)
            self.transport.sendto(resp, addr)
            return

        # 6. TOURNAMENT LIST (0x17)
        if pkt_type == 0x17:
            client_id = 10001
            if len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]
            elif addr in self.sessions:
                client_id = self.sessions[addr].client_id
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            resp = build_tournament_list(client_id)
            self.transport.sendto(resp, addr)
            return

        # 7. CLAN STATUS (0x11)
        if pkt_type == 0x11:
            client_id = 10001
            if len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]
            elif addr in self.sessions:
                client_id = self.sessions[addr].client_id
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            resp = build_clan_status(client_id)
            self.transport.sendto(resp, addr)
            return

        # 8. PING KEEPALIVE (0x07)
        if pkt_type == 0x07:
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            self.transport.sendto(data, addr)
            return

        # 9. KEEP_ALIVE (0x7f)
        if pkt_type == 0x7F:
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            return

        # 10. SPAWN REQUEST (0x1d)
        if pkt_type == 0x1D:
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            return

        # 11. PLAYER INPUT (0x02)
        if addr in self.sessions:
            self.sessions[addr].last_seen = time.time()

    async def game_loop(self):
        import time
        while True:
            try:
                await asyncio.sleep(0.05)
                now = time.time()
                inactive = []
                for addr, session in list(self.sessions.items()):
                    if now - session.last_seen > 30.0:
                        inactive.append(addr)
                        continue
                    session.seq = (session.seq + 1) & 0x7FFFFFFF
                    pkt = build_game_state(session.client_id)
                    self.transport.sendto(pkt, addr)
                for addr in inactive:
                    del self.sessions[addr]
            except asyncio.CancelledError:
                break
            except Exception as e:
                pass

    def connection_lost(self, exc):
        if self.update_task:
            self.update_task.cancel()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Nebulous Master API Server...")
    await db.init_db()
    loop = asyncio.get_running_loop()
    udp_transport = None
    try:
        udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: NebulousUDPServerProtocol(),
            local_addr=("0.0.0.0", SERVER_PUBLIC_UDP_PORT)
        )
        logger.info(f"European UDP Game Node started successfully on 0.0.0.0:{SERVER_PUBLIC_UDP_PORT}")
    except Exception as e:
        logger.warning(f"Could not bind UDP game port {SERVER_PUBLIC_UDP_PORT}: {e}")
    yield
    if udp_transport:
        udp_transport.close()
    logger.info("Shutting down Nebulous Server...")


app = FastAPI(
    title="Nebulous Private Server Master API",
    description="Custom private backend for Nebulous with MongoDB persistence for levels, stats, plasma, and free purchases.",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Helper to parse form data or query parameters from HttpURLConnection POST
async def get_request_params(request: Request) -> Dict[str, Any]:
    params = dict(request.query_params)
    try:
        form = await request.form()
        for k, v in form.items():
            params[k] = v
    except Exception:
        pass
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
    except Exception:
        pass
    return params


@app.get("/")
async def root():
    leaderboard = await db.get_leaderboard(limit=5)
    return {
        "status": "online",
        "service": "Nebulous Custom EU Master Server",
        "mongodb_connected": db.players_col is not None,
        "public_game_node": f"{SERVER_PUBLIC_IP}:{SERVER_PUBLIC_UDP_PORT}",
        "top_players": [
            {"account_id": p.get("account_id"), "name": p.get("username"), "xp": p.get("xp"), "coins": p.get("coins")}
            for p in leaderboard
        ]
    }


# ==============================================================================
# AUTHENTICATION & LOGIN (GoogleLogin / FacebookLogin)
# ==============================================================================

@app.post("/api/account/GoogleLogin")
@app.post("/api/account/FacebookLogin")
async def handle_login(request: Request):
    params = await get_request_params(request)
    auth_token = params.get("authToken", "")
    username = params.get("UserName") or params.get("Name")

    if not username:
        if db.players_col is not None:
            try:
                existing = await db.players_col.find_one()
                if existing and existing.get("username"):
                    username = existing.get("username")
            except Exception:
                pass
        if not username:
            username = "Szaby"

    logger.info(f"Login request from user '{username}' (Token: {auth_token[:15]}...)")

    player = await db.get_or_create_player(username=username)

    response_data = {
        "Ticket": player["ticket"],
        "UserName": player["username"],
        "AccountID": int(player["account_id"]),
        "accountID": int(player["account_id"]),
        "Coins": int(player["coins"]),
        "profileFonts": player.get("profile_fonts", [0]),
        "AccountColors": player.get("account_colors", [1, 2, 3]),
        "ClanRole": player.get("clan_role", "NONE"),
        "ClanName": player.get("clan_name", ""),
        "ClanColors": player.get("clan_colors", []),
        "AmMod": True,          # Gives moderator privileges in client
        "AdsRemoved": True,     # Disables ads
        "BanUntilUtc": None
    }
    return JSONResponse(content=response_data)


@app.post("/api/account/SetName")
async def handle_set_name(request: Request):
    params = await get_request_params(request)
    ticket = params.get("Ticket")
    new_name = params.get("Name")
    player = await db.get_player_by_ticket(ticket)
    if player and new_name:
        await db.update_player(player["account_id"], {"username": new_name})
        return JSONResponse(content={"status": "OK", "UserName": new_name, "Coins": int(player["coins"])})
    return JSONResponse(content={"status": "OK"})


# ==============================================================================
# DAILY CHECK-IN & REWARDS
# ==============================================================================

@app.post("/api/account/CheckIn")
async def handle_checkin(request: Request):
    params = await get_request_params(request)
    ticket = params.get("Ticket", "")
    player = await db.get_player_by_ticket(ticket)
    if not player:
        player = await db.get_or_create_player("Player")

    # Add plasma reward
    new_coins = min(player.get("coins", 0) + 10000, 999999999)
    await db.update_player(player["account_id"], {"coins": new_coins})

    return JSONResponse(content={
        "CheckinReward": 10000,
        "Coins": new_coins,
        "RewardVideosRemaining": 10
    })


# ==============================================================================
# CLIENT CONFIGURATION & REGION ROUTING
# ==============================================================================

@app.post("/api/account/GetClientConfig")
async def handle_client_config(request: Request):
    # Server Address Overrides map regions to this server's host or game nodes
    return JSONResponse(content={
        "ServerAddressOverrides": {
            "US_EAST": SERVER_PUBLIC_IP,
            "US_WEST": SERVER_PUBLIC_IP,
            "EU": SERVER_PUBLIC_IP,
            "EAST_ASIA": SERVER_PUBLIC_IP,
            "SOUTH_ASIA": SERVER_PUBLIC_IP,
            "SOUTH_AMERICA": SERVER_PUBLIC_IP,
            "AUSTRALIA": SERVER_PUBLIC_IP,
            "JAPAN": SERVER_PUBLIC_IP,
            "MIDDLE_EAST": SERVER_PUBLIC_IP,
            "SOUTH_AFRICA": SERVER_PUBLIC_IP,
            "INDIA": SERVER_PUBLIC_IP,
            "DEBUG": SERVER_PUBLIC_IP,
            "DEBUG_GLOBAL": SERVER_PUBLIC_IP,
            "EUROPE": SERVER_PUBLIC_IP
        },
        "ClanAllies": [],
        "ClanEnemies": [],
        "FriendAIDs": [],
        "UploadSizeLimitBytes": 1048576,
        "UploadPetSizeLimitBytes": 262144,
        "UploadParticleSizeLimitBytes": 131072,
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


# ==============================================================================
# PLAYER PROFILE
# ==============================================================================

@app.post("/api/account/GetPlayerProfile")
async def handle_get_profile(request: Request):
    params = await get_request_params(request)
    account_id = params.get("accountID") or params.get("AccountId")
    player = None
    if account_id:
        try:
            player = await db.get_player_by_id(int(account_id))
        except Exception:
            pass
    if not player:
        ticket = params.get("Ticket")
        player = await db.get_player_by_ticket(ticket)
    if not player:
        player = await db.get_or_create_player("Szaby")

    return JSONResponse(content={
        "accountID": int(player["account_id"]),
        "AccountId": int(player["account_id"]),
        "plasma": int(player["coins"]),
        "Coins": int(player["coins"]),
        "profile": player.get("profile_bio", "Nebulous Custom EU Server Player"),
        "profileColors": player.get("account_colors", [1, 2, 3]),
        "profileFonts": player.get("profile_fonts", [0]),
        "customSkinID": player.get("custom_skin_id", 0),
        "setNamePrice": 0,           # Free name changes
        "banned": False,
        "chatBanned": False,
        "arenaBanned": False,
        "relationship": "NONE",
        "profileVisibility": "PUBLIC",
        "profileBGColorEnabled": True,
        "profileBGColor": -16711936,  # Custom profile background color
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


# ==============================================================================
# PLAYER STATS & PROGRESSION (Saved to MongoDB)
# ==============================================================================

@app.post("/api/account/GetPlayerStats")
async def handle_get_stats(request: Request):
    params = await get_request_params(request)
    account_id = params.get("AccountId") or params.get("accountID")
    player = None
    if account_id:
        try:
            player = await db.get_player_by_id(int(account_id))
        except Exception:
            pass
    if not player:
        ticket = params.get("Ticket")
        player = await db.get_player_by_ticket(ticket)
    if not player:
        player = await db.get_or_create_player("Player")

    return JSONResponse(content={
        "AccountName": player["username"],
        "AccountColors": player.get("account_colors", [1, 2, 3]),
        "clanID": player.get("clan_id", -1),
        "ClanName": player.get("clan_name") or None,
        "ClanColors": player.get("clan_colors", []),
        "ClanRole": player.get("clan_role", "NONE"),
        "XP": int(player.get("xp", 15000000)),
        "DotsEaten": int(player.get("dots_eaten", 1250000)),
        "BlobsEaten": int(player.get("blobs_eaten", 45000)),
        "BlobsLost": int(player.get("blobs_lost", 120)),
        "BiggestBlob": int(player.get("biggest_blob", 85000)),
        "MassGained": int(player.get("mass_gained", 999999999)),
        "MassEjected": int(player.get("mass_ejected", 500000)),
        "EjectCount": int(player.get("eject_count", 15000)),
        "SplitCount": int(player.get("split_count", 18000)),
        "AverageScore": int(player.get("average_score", 25000)),
        "HighestScore": int(player.get("highest_score", 150000)),
        "TimesRestarted": int(player.get("times_restarted", 20)),
        "LongestLifeMS": int(player.get("longest_life_ms", 7200000)),
        "GamesWon": int(player.get("games_won", 500)),
        "MaxPlasmaChain": int(player.get("max_plasma_chain", 99)),
        "CoinsCollected": int(player.get("coins_collected", 500000)),
        "SMBHCollidedCount": int(player.get("smbh_collided_count", 25)),
        "SMBHEatenCount": int(player.get("smbh_eaten_count", 15)),
        "BHCollidedCount": int(player.get("bh_collided_count", 30)),
        "TBHCollidedCount": int(player.get("tbh_collided_count", 30)),
        "TimesTeleported": int(player.get("times_teleported", 150)),
        "PowerupsUsed": int(player.get("powerups_used", 600)),
        "TrickCount": int(player.get("trick_count", 120)),
        "Accolades": int(player.get("accolades", 250)),
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


# ==============================================================================
# SHOP & FREE PURCHASES ("minden ingyen")
# ==============================================================================

@app.post("/api/account/GetPurchasePrices")
async def handle_purchase_prices(request: Request):
    params = await get_request_params(request)
    ticket = params.get("Ticket")
    player = await db.get_player_by_ticket(ticket)
    if not player:
        player = await db.get_or_create_player("Player")

    # Set all item prices to 0 for free shopping!
    free_items = []
    for item_id in range(1, 250):
        free_items.append({
            "ItemType": "Skin",
            "ItemID": item_id,
            "Price": 0
        })

    return JSONResponse(content={
        "Coins": int(player["coins"]),
        "ClanCoins": int(player["clan_coins"]),
        "Items": free_items,
        "DailyFreeSkins": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    })


@app.post("/api/account/CoinPurchase")
async def handle_coin_purchase(request: Request):
    params = await get_request_params(request)
    ticket = params.get("Ticket")
    player = await db.get_player_by_ticket(ticket)
    if not player:
        player = await db.get_or_create_player("Player")

    item_type = params.get("ItemType", "Skin")
    item_id = int(params.get("ItemID", 1))

    # Add item to player inventory and persist in MongoDB
    owned = player.get("owned_skins", [])
    if item_id not in owned:
        owned.append(item_id)
        await db.update_player(player["account_id"], {"owned_skins": owned})

    logger.info(f"Player {player['username']} purchased {item_type} #{item_id} (Free!)")

    return JSONResponse(content={
        "ItemType": item_type,
        "ItemID": item_id,
        "Coins": int(player["coins"]),
        "ClanCoins": int(player["clan_coins"])
    })


@app.post("/api/account/GetSkinIDs")
async def handle_get_skin_ids(request: Request):
    params = await get_request_params(request)
    ticket = params.get("Ticket")
    player = await db.get_player_by_ticket(ticket)
    if not player:
        player = await db.get_or_create_player("Player")

    owned = player.get("owned_skins", list(range(1, 50)))
    skins_list = [
        {"ID": sid, "Status": "Purchased", "PurchaseCount": 1}
        for sid in owned
    ]

    return JSONResponse(content={
        "Skins": skins_list,
        "Coins": int(player["coins"]),
        "ClanCoins": int(player["clan_coins"]),
        "skinMapPrice": 0,
        "purchasedSecondPet": True,
        "unlockedMultiskin": True,
        "LimitMultiple": 10.0
    })


# ==============================================================================
# MASTER SERVER TICKET & GAME DISCOVERY (JDKaYIIScQ)
# ==============================================================================

@app.post("/api/account/JDKaYIIScQ")
async def handle_game_ticket(request: Request):
    params = await get_request_params(request)
    region = params.get("region", "EU")
    logger.info(f"Game ticket requested for region: {region}")

    # Generate a dummy 16-byte session ticket encoded in base64
    ticket_bytes = os.urandom(16)
    ticket_b64 = base64.b64encode(ticket_bytes).decode("ascii")

    return JSONResponse(content={
        "RezPlEVBeW": ticket_b64,
        "IP": SERVER_PUBLIC_IP,
        "PortA": SERVER_PUBLIC_UDP_PORT,
        "Nonce": 1
    })


# ==============================================================================
# ADDITIONAL ENDPOINTS (Effects, Clan, Leaderboard, Packs)
# ==============================================================================

@app.post("/api/account/GetActiveEffects")
@app.post("/api/account/GetActiveClanEffects")
async def handle_active_effects(request: Request):
    return JSONResponse(content={
        "XPMultiplier": 5,
        "XPMultiplierDurationRemainingS": 86400,
        "ClanCoins": 999999999
    })


@app.post("/api/account/GetPacks")
async def handle_get_packs(request: Request):
    return JSONResponse(content={"Packs": []})


@app.post("/api/account/GetFriends")
async def handle_get_friends(request: Request):
    return JSONResponse(content={"totalFriends": 0, "Friends": []})


@app.post("/api/account/GetClanStats")
async def handle_get_clan_stats(request: Request):
    return JSONResponse(content={
        "ClanName": "Alpha",
        "ClanTag": "ALP",
        "clanID": 1,
        "ClanColors": [1, 2, 3],
        "ClanRole": "Leader",
        "XP": 50000000,
        "MembersCount": 1
    })


@app.post("/api/account/GetClanMembers")
async def handle_get_clan_members(request: Request):
    return JSONResponse(content={"Members": []})


@app.post("/api/account/GetTeamList")
async def handle_get_teams(request: Request):
    return JSONResponse(content={"Teams": []})


@app.post("/api/account/GetHighScores")
@app.post("/api/account/GetXPLeaderBoard")
async def handle_leaderboards(request: Request):
    players = await db.get_leaderboard(limit=50)
    scores = []
    for rank, p in enumerate(players, 1):
        scores.append({
            "Rank": rank,
            "AccountId": p["account_id"],
            "Name": p["username"],
            "XP": p["xp"],
            "ClanName": p.get("clan_name", "")
        })
    return JSONResponse(content={"LeaderBoard": scores, "Scores": scores})


@app.post("/api/account/SetPlayerProfile")
@app.post("/api/account/SetPlayerProfileColors")
@app.post("/api/account/SetProfileVisibility")
async def handle_set_profile(request: Request):
    params = await get_request_params(request)
    ticket = params.get("Ticket")
    player = await db.get_player_by_ticket(ticket)
    if player:
        updates = {}
        if "customSkinID" in params:
            updates["custom_skin_id"] = int(params["customSkinID"])
        if "profile" in params:
            updates["profile_bio"] = params["profile"]
        if updates:
            await db.update_player(player["account_id"], updates)
    return JSONResponse(content={"status": "OK"})


# ==============================================================================
# UNIVERSAL FALLBACK ROUTE (Catches any remaining /api/account/ endpoints)
# ==============================================================================

@app.api_route("/api/account/{endpoint_name}", methods=["GET", "POST"])
async def fallback_endpoint(endpoint_name: str, request: Request):
    params = await get_request_params(request)
    logger.info(f"Fallback called for endpoint '{endpoint_name}' with params: {list(params.keys())}")
    return JSONResponse(content={
        "status": "OK",
        "Coins": 999999999,
        "ClanCoins": 999999999,
        "Items": [],
        "Packs": []
    })
