"""Dummy room server bundled with the Dummy game."""

from __future__ import annotations

import argparse
import json
import socket
import threading
from typing import Dict

DEFAULT_HOST = "127.0.0.1"

def log(msg: str) -> None:
    print(f"[Dummy room] {msg}")


def send_json(conn: socket.socket, payload: Dict) -> None:
    data = (json.dumps(payload) + "\n").encode("utf-8")
    conn.sendall(data)


def make_ok(action: str, data: Dict) -> Dict:
    return {"type": "room", "action": action, "status": "ok", "data": data}


def make_error(action: str, code: str, message: str) -> Dict:
    return {"type": "room", "action": action, "status": "error", "error": {"code": code, "message": message}}


def handle_message(conn: socket.socket, message: Dict, game_name: str, room_id: str) -> None:
    if message.get("type") != "room":
        send_json(conn, make_error(message.get("action") or "unknown", "INVALID_TYPE", "Expected room type"))
        return
    action = message.get("action")
    data = message.get("data", {}) or {}
    if action == "join":
        username = data.get("username") or "player"
        send_json(
            conn,
            make_ok(
                "join",
                {"message": "WELCOME", "room_id": room_id, "game_name": game_name, "username": username},
            ),
        )
    elif action == "heartbeat":
        send_json(conn, make_ok("heartbeat", {"message": "ALIVE"}))
    else:
        send_json(conn, make_error(action or "unknown", "UNSUPPORTED", "Unsupported room action"))


def handle_client(conn: socket.socket, addr, game_name: str, room_id: str) -> None:
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
            handle_message(conn, message, game_name, room_id)
    finally:
        reader.close()
        conn.close()
        log(f"Client disconnected: {addr}")


def serve(host: str, port: int, max_players: int, game_name: str, room_id: str) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    log(f"Room server {room_id} for {game_name} on {host}:{port} (max {max_players})")
    try:
        while True:
            conn, addr = server.accept()
            thread = threading.Thread(target=handle_client, args=(conn, addr, game_name, room_id), daemon=True)
            thread.start()
    except KeyboardInterrupt:
        log("Stopping room server")
    finally:
        server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dummy room server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--max-players", type=int, default=2)
    parser.add_argument("--game-name", required=True)
    parser.add_argument("--room-id", required=True)
    args = parser.parse_args()
    serve(args.host, args.port, args.max_players, args.game_name, args.room_id)


if __name__ == "__main__":
    main()
