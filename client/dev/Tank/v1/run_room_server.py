"""Tank game room server.

Protocol (JSON lines):
- room.join: {"type": "room", "action": "join", "data": {"username": "..."}}
- input: {"type": "room", "action": "input", "data": {"username": "...", "move": [dx,dy], "turret_delta": float, "fire": bool}}

Server broadcasts game state periodically as:
{"type": "room", "action": "state", "data": {...}}
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time
from typing import Dict, Tuple

DEFAULT_HOST = "0.0.0.0"
TICK_RATE = 30
ARENA_W, ARENA_H = 800, 600
TANK_SPEED = 180.0  # pixels per second
TURRET_SPEED = 180.0  # degrees per second
BULLET_SPEED = 420.0
TANK_RADIUS = 18
BULLET_RADIUS = 6


def log(msg: str) -> None:
    print(f"[TankRoom] {msg}")


def send_json(conn: socket.socket, payload: Dict) -> None:
    line = (json.dumps(payload) + "\n").encode("utf-8")
    conn.sendall(line)


class Player:
    def __init__(self, username: str, x: float, y: float, angle: float) -> None:
        self.username = username
        self.x = x
        self.y = y
        self.angle_turret = angle
        self.alive = True
        self.current_bullet_id: str | None = None
        self.respawn_timer: float | None = None


class Bullet:
    def __init__(self, bullet_id: str, owner: str, x: float, y: float, vx: float, vy: float) -> None:
        self.bullet_id = bullet_id
        self.owner = owner
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy


class TankRoom:
    def __init__(self) -> None:
        self.players: Dict[str, Player] = {}
        self.inputs: Dict[str, Dict] = {}
        self.bullets: Dict[str, Bullet] = {}
        self._bullet_counter = 0
        self._lock = threading.RLock()

    def spawn_point(self, idx: int) -> Tuple[float, float]:
        positions = [
            (100.0, 100.0),
            (ARENA_W - 100.0, ARENA_H - 100.0),
            (ARENA_W - 100.0, 100.0),
            (100.0, ARENA_H - 100.0),
        ]
        return positions[idx % len(positions)]

    def add_player(self, username: str) -> Player:
        with self._lock:
            idx = len(self.players)
            x, y = self.spawn_point(idx)
            player = Player(username, x, y, 0.0)
            self.players[username] = player
            return player

    def remove_player(self, username: str) -> None:
        with self._lock:
            self.players.pop(username, None)
            self.inputs.pop(username, None)

    def step(self, dt: float) -> None:
        with self._lock:
            # handle respawn timers
            for player in self.players.values():
                if not player.alive and player.respawn_timer is not None:
                    player.respawn_timer -= dt
                    if player.respawn_timer <= 0:
                        # respawn at a spawn point
                        idx = list(self.players.keys()).index(player.username)
                        player.x, player.y = self.spawn_point(idx)
                        player.alive = True
                        player.respawn_timer = None
                        player.current_bullet_id = None
            # apply inputs
            for username, player in list(self.players.items()):
                if not player.alive:
                    continue
                inp = self.inputs.get(username, {})
                dx, dy = inp.get("move", (0.0, 0.0))
                mag = math.hypot(dx, dy)
                if mag > 0:
                    dx /= mag
                    dy /= mag
                player.x += dx * TANK_SPEED * dt
                player.y += dy * TANK_SPEED * dt
                player.x = max(TANK_RADIUS, min(ARENA_W - TANK_RADIUS, player.x))
                player.y = max(TANK_RADIUS, min(ARENA_H - TANK_RADIUS, player.y))
                turret_delta = float(inp.get("turret_delta", 0.0))
                player.angle_turret = (player.angle_turret + turret_delta * TURRET_SPEED * dt) % 360
                if inp.get("fire") and player.current_bullet_id is None:
                    self._spawn_bullet(player)
                    inp["fire"] = False
            # move bullets
            for bullet_id, bullet in list(self.bullets.items()):
                bullet.x += bullet.vx * dt
                bullet.y += bullet.vy * dt
                if bullet.x < 0 or bullet.x > ARENA_W or bullet.y < 0 or bullet.y > ARENA_H:
                    self._despawn_bullet(bullet_id)
                    continue
                # collision with players
                for player in self.players.values():
                    if player.username == bullet.owner or not player.alive:
                        continue
                    if math.hypot(player.x - bullet.x, player.y - bullet.y) < (TANK_RADIUS + BULLET_RADIUS):
                        player.alive = False
                        player.respawn_timer = 3.0
                        self._despawn_bullet(bullet_id)
                        break

    def _spawn_bullet(self, player: Player) -> None:
        rad = math.radians(player.angle_turret)
        vx = math.cos(rad) * BULLET_SPEED
        vy = math.sin(rad) * BULLET_SPEED
        x = player.x + math.cos(rad) * (TANK_RADIUS + 5)
        y = player.y + math.sin(rad) * (TANK_RADIUS + 5)
        bullet_id = f"B{self._bullet_counter}"
        self._bullet_counter += 1
        bullet = Bullet(bullet_id, player.username, x, y, vx, vy)
        self.bullets[bullet_id] = bullet
        player.current_bullet_id = bullet_id

    def _despawn_bullet(self, bullet_id: str) -> None:
        bullet = self.bullets.pop(bullet_id, None)
        if bullet:
            owner = self.players.get(bullet.owner)
            if owner and owner.current_bullet_id == bullet_id:
                owner.current_bullet_id = None

    def to_state(self) -> Dict:
        with self._lock:
            return {
                "players": [
                    {
                        "username": p.username,
                        "x": p.x,
                        "y": p.y,
                        "angle_turret": p.angle_turret,
                        "alive": p.alive,
                        "current_bullet_id": p.current_bullet_id,
                    }
                    for p in self.players.values()
                ],
                "bullets": [
                    {"bullet_id": b.bullet_id, "owner": b.owner, "x": b.x, "y": b.y}
                    for b in self.bullets.values()
                ],
            }


def handle_client(conn: socket.socket, addr, room: TankRoom) -> None:
    log(f"Client connected: {addr}")
    username = None
    reader = conn.makefile("rb")
    try:
        for line in reader:
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "room":
                continue
            action = msg.get("action")
            data = msg.get("data", {}) or {}
            if action == "join":
                username = data.get("username") or f"player_{len(room.players)+1}"
                player = room.add_player(username)
                send_json(conn, {"type": "room", "action": "join", "status": "ok", "data": {"message": "WELCOME"}})
                log(f"{username} joined")
            elif action == "input" and username:
                move = data.get("move") or [0.0, 0.0]
                turret_delta = data.get("turret_delta", 0.0)
                fire = bool(data.get("fire"))
                room.inputs[username] = {"move": move, "turret_delta": turret_delta, "fire": fire}
    finally:
        reader.close()
        conn.close()
        if username:
            room.remove_player(username)
        log(f"Client disconnected: {addr}")


def broadcast_loop(clients: list[socket.socket], room: TankRoom) -> None:
    while True:
        time.sleep(1 / TICK_RATE)
        room.step(1 / TICK_RATE)
        state = room.to_state()
        packet = {"type": "room", "action": "state", "data": state}
        dead = []
        for conn in clients:
            try:
                send_json(conn, packet)
            except Exception:
                dead.append(conn)
        for d in dead:
            try:
                clients.remove(d)
            except ValueError:
                pass


def serve(host: str, port: int, max_players: int, game_name: str, room_id: str) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    log(f"Tank room {room_id} for {game_name} on {host}:{port} (max {max_players})")
    room = TankRoom()
    clients: list[socket.socket] = []
    broadcaster = threading.Thread(target=broadcast_loop, args=(clients, room), daemon=True)
    broadcaster.start()
    try:
        while True:
            conn, addr = server.accept()
            clients.append(conn)
            threading.Thread(target=handle_client, args=(conn, addr, room), daemon=True).start()
    except KeyboardInterrupt:
        log("Stopping room server")
    finally:
        server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Tank room server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--max-players", type=int, default=4)
    parser.add_argument("--game-name", required=True)
    parser.add_argument("--room-id", required=True)
    args = parser.parse_args()
    serve(args.host, args.port, args.max_players, args.game_name, args.room_id)


if __name__ == "__main__":
    main()
