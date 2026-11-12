#!/usr/bin/env python3
"""
Nebulous Private Game Server (Standalone & Render Node)
Author: 0szaby0
Handles complete UDP gameplay protocol for Nebulous Android:
- 0x75: Ping
- 0x85: Challenge handshake
- 0x7d: Connect & Join room
- 0x01: ConnectResult2
- 0x2b: GameConfig
- 0x4b: GameState (Lob/n0;)
- 0x1f: Leaderboard (Lob/x0;)
- 0x7f: KeepAlive
"""

import asyncio
import io
import struct
import logging
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("NebulousGameServer")

PORT = 25000


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
    # 0x2c (Lob/b;): byte 0x2c, int client_id, byte count, count * int room_id, int token
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
        self.last_seen = time.time()
        self.seq = 0
        self.active = True


class NebulousUDPServerProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.sessions = {} # addr -> ClientSession
        self.update_task = None

    def connection_made(self, transport):
        self.transport = transport
        logger.info(f"Nebulous Game Server listening on 0.0.0.0:{PORT} (UDP)")
        self.update_task = asyncio.create_task(self.game_loop())

    def datagram_received(self, data, addr):
        if not data:
            return

        pkt_type = data[0]

        # 1. PING (0x75 = 117)
        if pkt_type == 0x75 or (len(data) == 4 and data[0] == 0x75):
            resp = b"\x76\x00\x00\x00\x00"
            self.transport.sendto(resp, addr)
            logger.debug(f"Ping received from {addr}, responded with pong")
            return

        # 2. CHALLENGE (0x85 = 133 or signed -0x7b)
        if pkt_type in (0x85, 256 - 0x7B):
            resp = b"\x86\x01\x02\x03\x04"
            self.transport.sendto(resp, addr)
            logger.info(f"Challenge received from {addr}, sent 0x86 cookie challenge")
            return

        # 3. CONNECT (0x7d = 125)
        if pkt_type == 0x7D:
            try:
                cookie_len = data[5] if len(data) > 5 else 4
                cid_offset = 6 + cookie_len + 2
                client_id = 10001
                if len(data) >= cid_offset + 4:
                    client_id = struct.unpack(">i", data[cid_offset:cid_offset + 4])[0]
                
                logger.info(f"Connect handshake from {addr} with client_id={client_id}")
                
                session = ClientSession(addr, client_id)
                self.sessions[addr] = session

                # Send ConnectResult2 (packet 1, SUCCESS=0)
                resp = struct.pack(">BiBiiiifb", 1, client_id, 0, 100, 100, 1, 0, 1.0, 1)
                self.transport.sendto(resp, addr)
                logger.info(f"Sent ConnectResult2 (SUCCESS) to {addr}")

                # Send GameConfig (0x2b)
                config_pkt = build_game_config(client_id)
                self.transport.sendto(config_pkt, addr)
                logger.info(f"Sent GameConfig (0x2b) to {addr}")

                # Send initial GameState (0x4b)
                state_pkt = build_game_state(client_id)
                self.transport.sendto(state_pkt, addr)
                logger.info(f"Sent initial GameState (0x4b) to {addr}")

                # Send Leaderboard (0x1f)
                lb_pkt = build_leaderboard(client_id)
                self.transport.sendto(lb_pkt, addr)
                logger.info(f"Sent initial Leaderboard (0x1f) to {addr}")

            except Exception as e:
                logger.error(f"Error handling connect packet: {e}", exc_info=True)
            return

        # 4. CUSTOM ROOMS REQUEST (0x21 = 33)
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
            logger.info(f"RECV 0x21: {data.hex()} -> Responded (0x2d, 4 EU rooms) to {addr} (client_id={client_id}, offset={offset})")
            return

        # 4b. ROOM QUERY (0x2c = 44)
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
            logger.info(f"RECV 0x2c -> Responded with 0x2c room query to {addr} (client_id={client_id})")
            return

        # 5. TEAM ROOMS / CLAN WAR INFO (0x42 = 66)
        if pkt_type == 0x42:
            client_id = 10001
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
                client_id = self.sessions[addr].client_id
            elif len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]

            resp = build_clan_info(client_id)
            self.transport.sendto(resp, addr)
            logger.info(f"Responded to 0x42 with 0x43 (Clan/Team info) to {addr}")
            return

        # 6. TOURNAMENT / ARENA LIST (0x17 = 23)
        if pkt_type == 0x17:
            client_id = 10001
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
                client_id = self.sessions[addr].client_id
            elif len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]

            resp = build_tournament_list(client_id)
            self.transport.sendto(resp, addr)
            logger.info(f"Responded to 0x17 with 0x18 (Tournament list) to {addr}")
            return

        # 7. CLAN LIST (0x11 = 17)
        if pkt_type == 0x11:
            client_id = 10001
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
                client_id = self.sessions[addr].client_id
            elif len(data) >= 5:
                client_id = struct.unpack(">i", data[1:5])[0]

            resp = build_clan_status(client_id)
            self.transport.sendto(resp, addr)
            logger.info(f"Responded to 0x11 with 0x12 (Clan list) to {addr}")
            return

        # 8. KEEPALIVE PING (0x07 = 7)
        if pkt_type == 0x07:
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            self.transport.sendto(data, addr)
            return

        # 9. KEEP_ALIVE (0x7f = 127)
        if pkt_type == 0x7F:
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            return

        # 10. SPAWN REQUEST (0x1d = 29)
        if pkt_type == 0x1D:
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
                logger.info(f"Spawn requested by {addr}")
            return

        # 11. PLAYER INPUT (0x02 = 2) or other packets
        if addr in self.sessions:
            self.sessions[addr].last_seen = time.time()
            logger.debug(f"Received Game Packet 0x{pkt_type:02x} ({len(data)} bytes) from {addr}")

    async def game_loop(self):
        """Sends periodic game updates to all active sessions (20 FPS)."""
        while True:
            try:
                await asyncio.sleep(0.05) # 20 ticks/sec
                now = time.time()
                inactive = []

                for addr, session in list(self.sessions.items()):
                    if now - session.last_seen > 30.0:
                        inactive.append(addr)
                        continue

                    # Send GameState (0x4b)
                    session.seq = (session.seq + 1) & 0x7FFFFFFF
                    pkt = build_game_state(session.client_id)
                    self.transport.sendto(pkt, addr)

                for addr in inactive:
                    logger.info(f"Session {addr} timed out")
                    del self.sessions[addr]

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in game loop: {e}")

    def connection_lost(self, exc):
        if self.update_task:
            self.update_task.cancel()
        logger.info("Nebulous Game Server stopped.")


async def start_server():
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: NebulousUDPServerProtocol(),
        local_addr=("0.0.0.0", PORT)
    )
    logger.info(f"Game server running on 0.0.0.0:{PORT}")
    try:
        await asyncio.Future()
    finally:
        transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        logger.info("Server terminated by user.")
