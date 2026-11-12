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
                # Struct: 1 (int8), client_id (int32), result=0 (int8: SUCCESS),
                # public_id=100 (int32), private_id=100 (int32), game_id=1 (int32),
                # ban_length=0 (int32), ad_stuff=1.0 (float32), split_multiplier=1 (int8)
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

        # 4. KEEP_ALIVE (0x7f = 127)
        if pkt_type == 0x7F:
            if addr in self.sessions:
                self.sessions[addr].last_seen = time.time()
            return

        # 5. Any other game packet (spawn 0x1d, move 0x20, split, eject)
        if addr in self.sessions:
            self.sessions[addr].last_seen = time.time()
            logger.info(f"Received Game Packet 0x{pkt_type:02x} ({len(data)} bytes) from {addr}")

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
