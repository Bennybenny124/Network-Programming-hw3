"""Game lobby server.

Maintains room list for a single game and spawns a room server per room.
Protocol: TCP + JSON lines (newline-delimited).
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 11000
DEFAULT_ROOM_PORT_START = 12000

CURRENT_DIR = Path(__file__).resolve().parent
ROOM_SERVER_SCRIPT = CURRENT_DIR / "room_server.py"

# Game package directory and entry name (set in main)
game_dir: Path = CURRENT_DIR
entry_room_server = "run_room_server.py"


@dataclass
class Room:
    room_id: str
    game_name: str
    version: str
    host_username: str
    max_players: int
    room_server_host: str
    room_server_port: int
    process: subprocess.Popen
    players: List[str] = field(default_factory=list)
    status: str = "waiting"

    def to_dict(self) -> Dict:
        return {
            "room_id": self.room_id,
            "game_name": self.game_name,
            "version": self.version,
            "host_username": self.host_username,
            "players": list(self.players),
            "max_players": self.max_players,
            "status": self.status,
            "room_server_host": self.room_server_host,
            "room_server_port": self.room_server_port,
        }


rooms_lock = threading.RLock()
rooms: Dict[str, Room] = {}
room_counter = 0


def log(msg: str) -> None:
    print(f"[lobby] {msg}")


def send_json(conn: socket.socket, payload: Dict) -> None:
    data = (json.dumps(payload) + "\n").encode("utf-8")
    conn.sendall(data)


def make_ok(action: str, data: Dict) -> Dict:
    return {"type": "lobby", "action": action, "status": "ok", "data": data}


def make_error(action: str, code: str, message: str) -> Dict:
    return {"type": "lobby", "action": action, "status": "error", "error": {"code": code, "message": message}}


def _find_free_port(host: str, start: int, taken: List[int]) -> int:
    port = start
    while True:
        if port not in taken and _port_available(host, port):
            return port
        port += 1


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _next_room_id() -> str:
    global room_counter
    with rooms_lock:
        room_counter += 1
        return f"R{room_counter}"


def handle_list_rooms(conn: socket.socket) -> None:
    with rooms_lock:
        payload = {"rooms": [room.to_dict() for room in rooms.values()]}
    send_json(conn, make_ok("list_rooms", payload))


def _monitor_room(room: Room) -> None:
    room.process.wait()
    with rooms_lock:
        tracked = rooms.get(room.room_id)
        if tracked is room:
            room.status = "closed"
    log(f"Room server for {room.room_id} exited")


def handle_create_room(conn: socket.socket, data: Dict, lobby_host: str, room_port_start: int, game_name: str) -> None:
    username = data.get("username") or "host"
    max_players = int(data.get("max_players") or 2)
    version = data.get("version") or "latest"
    room_id = _next_room_id()

    with rooms_lock:
        # prevent the same user from owning/joining multiple waiting rooms
        for existing in rooms.values():
            if existing.status == "waiting" and username in existing.players:
                send_json(
                    conn,
                    make_error(
                        "create_room",
                        "ALREADY_IN_ROOM",
                        "User is already in a room",
                    ),
                )
                return
        taken_ports = [room.room_server_port for room in rooms.values()]
    port = _find_free_port(lobby_host, room_port_start, taken_ports)

    entry_script = game_dir / entry_room_server
    if not entry_script.exists():
        entry_script = game_dir / "run_room_server.py"
    if not entry_script.exists() and ROOM_SERVER_SCRIPT.exists():
        entry_script = ROOM_SERVER_SCRIPT  # fallback to shared dummy server
    if not entry_script.exists():
        send_json(conn, make_error("create_room", "ROOM_SERVER_MISSING", "Room server entry not found in game package"))
        return

    cmd = [
        sys.executable,
        "-u",
        str(entry_script),
        "--host",
        lobby_host,
        "--port",
        str(port),
        "--max-players",
        str(max_players),
        "--game-name",
        game_name,
        "--room-id",
        room_id,
    ]
    try:
        proc = subprocess.Popen(cmd, cwd=str(entry_script.parent))
    except OSError as exc:
        send_json(conn, make_error("create_room", "ROOM_SERVER_FAILED", f"Failed to start room server: {exc}"))
        return

    room = Room(
        room_id=room_id,
        game_name=game_name,
        version=version,
        host_username=username,
        max_players=max_players,
        room_server_host=lobby_host,
        room_server_port=port,
        process=proc,
        players=[username],
    )
    with rooms_lock:
        rooms[room_id] = room
    threading.Thread(target=_monitor_room, args=(room,), daemon=True).start()

    send_json(
        conn,
        make_ok(
            "create_room",
            {
                "room_id": room_id,
                "game_name": game_name,
                "version": version,
                "room_server_host": lobby_host,
                "room_server_port": port,
            },
        ),
    )
    log(f"Created room {room_id} on port {port} for {game_name} by {username}")


def handle_join_room(conn: socket.socket, data: Dict) -> None:
    room_id = data.get("room_id")
    username = data.get("username")
    if not room_id or not username:
        send_json(conn, make_error("join_room", "INVALID_REQUEST", "room_id and username required"))
        return
    with rooms_lock:
        for existing in rooms.values():
            if existing.status == "waiting" and existing.room_id != room_id and username in existing.players:
                send_json(conn, make_error("join_room", "ALREADY_IN_ROOM", "User is already in another room"))
                return
        room = rooms.get(room_id)
        if not room:
            send_json(conn, make_error("join_room", "ROOM_NOT_FOUND", "Room not found"))
            return
        if room.status != "waiting":
            send_json(conn, make_error("join_room", "ROOM_NOT_JOINABLE", "Room not accepting players"))
            return
        if len(room.players) >= room.max_players and username not in room.players:
            send_json(conn, make_error("join_room", "ROOM_FULL", "Room is full"))
            return
        if username not in room.players:
            room.players.append(username)
        payload = {
            "room_id": room.room_id,
            "game_name": room.game_name,
            "version": room.version,
            "room_server_host": room.room_server_host,
            "room_server_port": room.room_server_port,
        }
    send_json(conn, make_ok("join_room", payload))
    log(f"{username} joined room {room_id}")


def handle_leave_room(conn: socket.socket, data: Dict) -> None:
    username = data.get("username")
    room_id = data.get("room_id")
    if not username:
        send_json(conn, make_error("leave_room", "INVALID_REQUEST", "username required"))
        return
    removed = False
    with rooms_lock:
        if room_id:
            room = rooms.get(room_id)
            if room and username in room.players:
                room.players = [p for p in room.players if p != username]
                removed = True
        else:
            for room in rooms.values():
                if username in room.players:
                    room.players = [p for p in room.players if p != username]
                    removed = True
        # clean closed/empty rooms list? keep for now, just status update
    if removed:
        send_json(conn, make_ok("leave_room", {"room_id": room_id or ""}))
        log(f"{username} left room {room_id or 'unknown'}")
    else:
        send_json(conn, make_error("leave_room", "NOT_IN_ROOM", "User not found in room"))


def handle_message(conn: socket.socket, message: Dict, lobby_host: str, room_port_start: int, game_name: str) -> None:
    if message.get("type") != "lobby":
        send_json(conn, make_error(message.get("action") or "unknown", "INVALID_TYPE", "Expected lobby type"))
        return
    action = message.get("action")
    data = message.get("data", {}) or {}
    if action == "list_rooms":
        handle_list_rooms(conn)
    elif action == "create_room":
        handle_create_room(conn, data, lobby_host, room_port_start, game_name)
    elif action == "join_room":
        handle_join_room(conn, data)
    elif action == "leave_room":
        handle_leave_room(conn, data)
    else:
        send_json(conn, make_error(action or "unknown", "UNSUPPORTED", "Unsupported lobby action"))


def handle_client(conn: socket.socket, addr, lobby_host: str, room_port_start: int, game_name: str) -> None:
    log(f"Client connected: {addr}")
    reader = conn.makefile("rb")
    try:
        while True:
            line = reader.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                send_json(conn, make_error("unknown", "INVALID_JSON", "Failed to parse JSON"))
                continue
            handle_message(conn, message, lobby_host, room_port_start, game_name)
    finally:
        reader.close()
        conn.close()
        log(f"Client disconnected: {addr}")


def serve_forever(host: str, port: int, room_port_start: int, game_name: str) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    log(f"Lobby for {game_name} listening on {host}:{port} (room ports from {room_port_start})")
    try:
        while True:
            conn, addr = server.accept()
            thread = threading.Thread(
                target=handle_client, args=(conn, addr, host, room_port_start, game_name), daemon=True
            )
            thread.start()
    except KeyboardInterrupt:
        log("Stopping lobby server")
    finally:
        server.close()
        with rooms_lock:
            running = list(rooms.values())
        for room in running:
            try:
                room.process.terminate()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Game lobby server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--room-port-start", type=int, default=DEFAULT_ROOM_PORT_START)
    parser.add_argument("--game-dir", required=True)
    parser.add_argument("--game-name", required=True)
    args = parser.parse_args()

    global game_dir, entry_room_server
    game_dir = Path(args.game_dir).resolve()
    config_path = game_dir / "game_config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            entry_room_server = cfg.get("entry_room_server", entry_room_server)
        except Exception:
            entry_room_server = "run_room_server.py"

    serve_forever(args.host, args.port, args.room_port_start, args.game_name)


if __name__ == "__main__":
    main()
