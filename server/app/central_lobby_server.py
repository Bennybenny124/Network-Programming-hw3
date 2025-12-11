"""Central lobby server.

TCP + JSON (newline-delimited) control channel.
File uploads/downloads stream raw bytes immediately after the JSON header.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Dict, Optional, Tuple

# Allow running directly from repository root or server/app
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import db_server

HOST = "linux2.cs.nycu.edu.tw"
PORT = 12345
LOBBY_BASE_PORT = 11000
ROOM_BASE_PORT = 12000


@dataclass
class LobbyProcess:
    game_name: str
    process: subprocess.Popen
    host: str
    port: int


clients_lock = threading.RLock()
clients: Dict[socket.socket, Optional[str]] = {}
active_usernames: set[str] = set()

lobbies_lock = threading.RLock()
lobbies: Dict[str, LobbyProcess] = {}


def log(message: str) -> None:
    print(f"[central] {message}")


def send_json(conn: socket.socket, payload: Dict) -> None:
    data = (json.dumps(payload) + "\n").encode("utf-8")
    conn.sendall(data)


def make_ok(req_type: str, action: str, data: Dict) -> Dict:
    return {"type": req_type, "action": action, "status": "ok", "data": data}


def make_error(req_type: str, action: str, code: str, message: str) -> Dict:
    return {
        "type": req_type,
        "action": action,
        "status": "error",
        "error": {"code": code, "message": message},
    }


class ClientContext:
    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.reader = conn.makefile("rb")
        self.username: Optional[str] = None

    def close(self) -> None:
        try:
            self.reader.close()
        finally:
            self.conn.close()


def _read_exact(reader, size: int) -> Optional[bytes]:
    chunks = []
    remaining = size
    while remaining > 0:
        data = reader.read(remaining)
        if not data:
            return None
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def handle_auth(ctx: ClientContext, message: Dict) -> None:
    action = message.get("action")
    data = message.get("data", {}) or {}
    if action == "register":
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            send_json(
                ctx.conn,
                make_error("auth", "register", "INVALID_REQUEST", "Username/password required"),
            )
            return
        success, code = db_server.register_user(username, password)
        if success:
            send_json(ctx.conn, make_ok("auth", "register", {"username": username}))
        else:
            message = "Username already exists"
            if code == "INVALID_USERNAME":
                message = "Username contains invalid characters"
            send_json(
                ctx.conn,
                make_error("auth", "register", code or "REGISTER_FAILED", message),
            )
    elif action == "login":
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            send_json(
                ctx.conn, make_error("auth", "login", "INVALID_REQUEST", "Username/password required")
            )
            return
        with clients_lock:
            if username in active_usernames:
                send_json(
                    ctx.conn,
                    make_error("auth", "login", "USER_ALREADY_LOGGED_IN", "User already logged in elsewhere"),
                )
                return
        if db_server.authenticate_user(username, password):
            ctx.username = username
            with clients_lock:
                clients[ctx.conn] = username
                active_usernames.add(username)
            send_json(ctx.conn, make_ok("auth", "login", {"username": username}))
        else:
            send_json(
                ctx.conn,
                make_error("auth", "login", "INVALID_CREDENTIALS", "Username or password incorrect"),
            )
    elif action == "logout":
        if ctx.username:
            with clients_lock:
                clients.pop(ctx.conn, None)
                active_usernames.discard(ctx.username)
            send_json(ctx.conn, make_ok("auth", "logout", {"username": ctx.username}))
            ctx.username = None
        else:
            send_json(
                ctx.conn, make_error("auth", "logout", "NOT_LOGGED_IN", "No active session on socket")
            )
    else:
        send_json(
            ctx.conn,
            make_error("auth", action or "unknown", "UNSUPPORTED", "Unsupported auth action"),
        )


def handle_store(ctx: ClientContext, message: Dict) -> None:
    action = message.get("action")
    data = message.get("data", {}) or {}
    if not ctx.username:
        send_json(ctx.conn, make_error("store", action or "unknown", "NOT_AUTHENTICATED", "Login first"))
        return

    if action == "list_games":
        games = db_server.list_games()
        with lobbies_lock:
            for game in games:
                lobby = lobbies.get(game.get("game_name"))
                if lobby:
                    game["lobby_host"] = lobby.host
                    game["lobby_port"] = lobby.port
        send_json(ctx.conn, make_ok("store", "list_games", {"games": games}))
    elif action == "get_game_detail":
        game_name = data.get("game_name")
        game = db_server.get_game(game_name) if game_name else None
        if not game:
            send_json(
                ctx.conn,
                make_error(
                    "store", "get_game_detail", "GAME_NOT_FOUND", "Specified game not found in store"
                ),
            )
            return
        lobby_info = {}
        with lobbies_lock:
            lobby = lobbies.get(game_name)
            if lobby:
                lobby_info = {"lobby_host": lobby.host, "lobby_port": lobby.port}
        comments = db_server.list_comments(game_name)
        rating_val = None
        if comments:
            try:
                rating_val = round(mean([c.get("score", 0) for c in comments]), 1)
            except Exception:
                rating_val = None
        resp = game.copy()
        # If description missing in metadata but available in extracted config, pull it
        if not resp.get("description"):
            extracted = resp.get("extracted_path")
            cfg_path = Path(extracted) / "game_config.json" if extracted else None
            if cfg_path and cfg_path.exists():
                try:
                    cfg_obj = json.loads(cfg_path.read_text(encoding="utf-8"))
                    resp["description"] = cfg_obj.get("description", "")
                except Exception:
                    pass
        resp.update(lobby_info)
        resp.update({"comments": comments, "rating": rating_val})
        send_json(ctx.conn, make_ok("store", "get_game_detail", resp))
    elif action == "download_game_file":
        game_name = data.get("game_name")
        if not game_name:
            send_json(
                ctx.conn,
                make_error("store", "download_game_file", "INVALID_REQUEST", "Missing game_name"),
            )
            return
        game = db_server.get_game(game_name)
        if not game:
            send_json(
                ctx.conn,
                make_error(
                    "store", "download_game_file", "GAME_OR_VERSION_NOT_FOUND", "Specified game not found"
                ),
            )
            return
        storage_path = game.get("storage_path")
        if not storage_path or not os.path.exists(storage_path):
            send_json(
                ctx.conn,
                make_error(
                    "store", "download_game_file", "GAME_OR_VERSION_NOT_FOUND", "Stored file missing on server"
                ),
            )
            return
        filesize = os.path.getsize(storage_path)
        header = make_ok(
            "store",
            "download_game_file",
            {
                "game_name": game_name,
                "filename": os.path.basename(storage_path),
                "filesize": filesize,
                "version": game.get("version"),
            },
        )
        send_json(ctx.conn, header)
        with open(storage_path, "rb") as fh:
            while True:
                chunk = fh.read(4096)
                if not chunk:
                    break
                ctx.conn.sendall(chunk)
        db_server.record_download(ctx.username, game_name)
        log(f"Sent {filesize} bytes for {game_name} to {ctx.username}")
    elif action == "add_comment":
        game_name = data.get("game_name")
        score = data.get("score")
        comment_text = data.get("comment", "")
        if not game_name or score is None:
            send_json(ctx.conn, make_error("store", "add_comment", "INVALID_REQUEST", "Missing game_name or score"))
            return
        try:
            score_int = int(score)
        except (TypeError, ValueError):
            send_json(ctx.conn, make_error("store", "add_comment", "INVALID_SCORE", "Score must be an integer"))
            return
        db_server.add_comment(game_name, ctx.username or "anonymous", score_int, str(comment_text))
        updated = db_server.list_comments(game_name)
        rating_val = None
        if updated:
            try:
                rating_val = round(mean([c.get("score", 0) for c in updated]), 1)
            except Exception:
                rating_val = None
        send_json(
            ctx.conn,
            make_ok(
                "store",
                "add_comment",
                {"game_name": game_name, "comments": updated, "rating": rating_val},
            ),
        )
    elif action == "mark_owned":
        game_name = data.get("game_name")
        if not game_name:
            send_json(ctx.conn, make_error("store", "mark_owned", "INVALID_REQUEST", "Missing game_name"))
            return
        db_server.record_download(ctx.username, game_name)
        send_json(ctx.conn, make_ok("store", "mark_owned", {"game_name": game_name}))
    else:
        send_json(
            ctx.conn,
            make_error("store", action or "unknown", "UNSUPPORTED", "Unsupported store action"),
        )


def handle_dev(ctx: ClientContext, message: Dict) -> None:
    action = message.get("action")
    data = message.get("data", {}) or {}
    if not ctx.username:
        send_json(ctx.conn, make_error("dev", action or "unknown", "NOT_AUTHENTICATED", "Login first"))
        return

    if action == "upload_game_file":
        game_name = data.get("game_name")
        version = data.get("version")
        filename = data.get("filename")
        filesize = data.get("filesize")
        min_players = data.get("min_players")
        max_players = data.get("max_players")
        if not all([game_name, version, filename, isinstance(filesize, int)]):
            send_json(
                ctx.conn,
                make_error("dev", "upload_game_file", "INVALID_REQUEST", "Missing required upload fields"),
            )
            return
        try:
            min_players_int = int(min_players) if min_players is not None else 1
            max_players_int = int(max_players) if max_players is not None else max(min_players_int, 4)
        except (TypeError, ValueError):
            send_json(
                ctx.conn,
                make_error("dev", "upload_game_file", "INVALID_PLAYERS", "min_players/max_players must be integers"),
            )
            return
        if min_players_int < 1 or max_players_int < min_players_int:
            send_json(
                ctx.conn,
                make_error("dev", "upload_game_file", "INVALID_PLAYERS", "Players range is invalid"),
            )
            return
        existing = db_server.get_game(game_name)
        if existing and existing.get("author") and existing.get("author") != ctx.username:
            send_json(
                ctx.conn,
                make_error(
                    "dev",
                    "upload_game_file",
                    "GAME_EXISTS_OTHER_AUTHOR",
                    "A game with the same name by another developer already exists.",
                ),
            )
            return

        storage_dir = db_server.ensure_game_storage_dir(game_name)
        dest_path = storage_dir / filename
        extract_dir = storage_dir / "extracted"

        payload = _read_exact(ctx.reader, filesize)
        if payload is None or len(payload) != filesize:
            send_json(
                ctx.conn,
                make_error("dev", "upload_game_file", "UPLOAD_FAILED", "Connection lost during upload"),
            )
            return
        with open(dest_path, "wb") as fh:
            fh.write(payload)

        # Refresh extracted contents for server-side use
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(dest_path, "r") as zf:
                zf.extractall(extract_dir)
        except Exception as exc:
            send_json(
                ctx.conn,
                make_error("dev", "upload_game_file", "UNZIP_FAILED", f"Failed to unzip package: {exc}"),
            )
            return

        description = ""
        cfg_path = extract_dir / "game_config.json"
        if cfg_path.exists():
            try:
                cfg_obj = json.loads(cfg_path.read_text(encoding="utf-8"))
                description = cfg_obj.get("description", "") or ""
            except Exception:
                description = ""

        game_obj = db_server.upsert_game(
            game_name,
            version,
            filename,
            ctx.username,
            description=description,
            extracted_path=str(extract_dir),
            min_players=min_players_int,
            max_players=max_players_int,
        )
        send_json(
            ctx.conn,
            make_ok(
                "dev",
                "upload_game_file",
                {
                    "game_name": game_name,
                    "version": version,
                    "stored_path": str(dest_path),
                    "extracted_path": str(extract_dir),
                },
            ),
        )
        log(f"Stored {filesize} bytes for game {game_name} version {version}")
        log(f"Game metadata: {game_obj}")
    elif action == "launch_game_server":
        game_name = data.get("game_name")
        if not game_name:
            send_json(
                ctx.conn,
                make_error("dev", "launch_game_server", "INVALID_REQUEST", "Missing game_name"),
            )
            return
        game = db_server.get_game(game_name)
        if not game:
            send_json(
                ctx.conn,
                make_error("dev", "launch_game_server", "GAME_NOT_FOUND", "Upload the game before launching lobby"),
            )
            return
        success, info_or_error = start_lobby_if_needed(game_name, game)
        if success:
            host, port = info_or_error
            send_json(
                ctx.conn,
                make_ok(
                    "dev",
                    "launch_game_server",
                    {"game_name": game_name, "lobby_host": host, "lobby_port": port},
                ),
            )
        else:
            send_json(
                ctx.conn,
                make_error("dev", "launch_game_server", "LAUNCH_FAILED", info_or_error),
            )
    elif action == "stop_game_server":
        game_name = data.get("game_name")
        if not game_name:
            send_json(
                ctx.conn,
                make_error("dev", "stop_game_server", "INVALID_REQUEST", "Missing game_name"),
            )
            return
        success, msg = stop_lobby(game_name)
        if success:
            send_json(
                ctx.conn,
                make_ok("dev", "stop_game_server", {"game_name": game_name, "stopped": True}),
            )
        else:
            send_json(
                ctx.conn,
                make_error("dev", "stop_game_server", "STOP_FAILED", msg or "Failed to stop lobby"),
            )
    else:
        if action == "delete_game":
            game_name = data.get("game_name")
            if not game_name:
                send_json(
                    ctx.conn,
                    make_error("dev", "delete_game", "INVALID_REQUEST", "Missing game_name"),
                )
                return
            existing = db_server.get_game(game_name)
            if not existing:
                send_json(
                    ctx.conn,
                    make_error("dev", "delete_game", "GAME_NOT_FOUND", "Game not found"),
                )
                return
            if existing.get("author") and existing.get("author") != ctx.username:
                send_json(
                    ctx.conn,
                    make_error("dev", "delete_game", "NOT_OWNER", "You are not the author of this game"),
                )
                return
            success, err = db_server.remove_game(game_name, ctx.username)
            if success:
                # stop lobby if running
                stop_lobby(game_name)
                send_json(ctx.conn, make_ok("dev", "delete_game", {"game_name": game_name, "deleted": True}))
            else:
                send_json(
                    ctx.conn,
                    make_error("dev", "delete_game", err or "DELETE_FAILED", err or "Failed to delete game"),
                )
        else:
            send_json(ctx.conn, make_error("dev", action or "unknown", "UNSUPPORTED", "Unsupported dev action"))


def start_lobby_if_needed(game_name: str, game_obj: Optional[Dict] = None) -> Tuple[bool, str | Tuple[str, int]]:
    if game_obj is None:
        game_obj = db_server.get_game(game_name)
    with lobbies_lock:
        existing = lobbies.get(game_name)
        if existing and existing.process.poll() is None:
            return True, (existing.host, existing.port)

        extracted_path = _ensure_extracted_package(game_obj)
        if not extracted_path:
            return False, "Game package not extracted on server; upload again"

        port = _find_free_port(LOBBY_BASE_PORT, set(l.port for l in lobbies.values()))
        script = CURRENT_DIR / "game_lobby_server.py"
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(script),
                    "--host",
                    HOST,
                    "--port",
                    str(port),
                    "--room-port-start",
                    str(ROOM_BASE_PORT),
                    "--game-dir",
                    extracted_path,
                    "--game-name",
                    game_name,
                ],
                cwd=str(CURRENT_DIR),
            )
        except OSError as exc:
            return False, f"Failed to start lobby process: {exc}"

        lobby = LobbyProcess(game_name=game_name, process=proc, host=HOST, port=port)
        lobbies[game_name] = lobby
        threading.Thread(target=_monitor_lobby, args=(lobby,), daemon=True).start()
        log(f"Launched lobby for {game_name} on {HOST}:{port}")
        return True, (HOST, port)


def stop_lobby(game_name: str) -> Tuple[bool, Optional[str]]:
    with lobbies_lock:
        lobby = lobbies.get(game_name)
        if not lobby:
            return False, "Lobby not running"
        proc = lobby.process
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    with lobbies_lock:
        lobbies.pop(game_name, None)
    log(f"Stopped lobby for {game_name}")
    return True, None


def _monitor_lobby(lobby: LobbyProcess) -> None:
    lobby.process.wait()
    with lobbies_lock:
        if lobbies.get(lobby.game_name) is lobby:
            lobbies.pop(lobby.game_name, None)
    log(f"Lobby process for {lobby.game_name} exited")


def _find_free_port(start_port: int, taken: set[int]) -> int:
    port = start_port
    while True:
        if port not in taken and _port_available(port):
            return port
        port += 1


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def _ensure_extracted_package(game_obj: Optional[Dict]) -> Optional[str]:
    if not game_obj:
        return None
    extracted_path = game_obj.get("extracted_path")
    if extracted_path and os.path.exists(extracted_path):
        return extracted_path
    storage_path = game_obj.get("storage_path")
    if not storage_path or not os.path.exists(storage_path):
        return None
    extract_dir = Path(storage_path).parent / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(storage_path, "r") as zf:
            zf.extractall(extract_dir)
    except Exception:
        return None
    db_server.upsert_game(
        game_obj.get("game_name"),
        game_obj.get("version"),
        game_obj.get("filename"),
        game_obj.get("author"),
        game_obj.get("description", ""),
        extracted_path=str(extract_dir),
    )
    return str(extract_dir)


def dispatch(ctx: ClientContext, message: Dict) -> None:
    msg_type = message.get("type")
    if msg_type == "auth":
        handle_auth(ctx, message)
    elif msg_type == "store":
        handle_store(ctx, message)
    elif msg_type == "dev":
        handle_dev(ctx, message)
    else:
        send_json(
            ctx.conn,
            make_error(msg_type or "unknown", message.get("action") or "unknown", "UNKNOWN_TYPE", "Unknown type"),
        )


def handle_client(conn: socket.socket, addr) -> None:
    ctx = ClientContext(conn)
    with clients_lock:
        clients[conn] = None
    log(f"Client connected from {addr}")
    try:
        while True:
            line = ctx.reader.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                send_json(
                    ctx.conn,
                    make_error("unknown", "unknown", "INVALID_JSON", "Failed to decode JSON line"),
                )
                continue
            dispatch(ctx, message)
    finally:
        with clients_lock:
            clients.pop(conn, None)
        ctx.close()
        log(f"Client disconnected from {addr}")


def serve_forever(host: str, port: int) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen()
    log(f"Central lobby listening on {host}:{port}")
    db_server.initialize_storage()
    try:
        while True:
            conn, addr = server.accept()
            thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            thread.start()
    except KeyboardInterrupt:
        log("Shutting down central lobby")
    finally:
        server.close()
        with lobbies_lock:
            running = list(lobbies.values())
        for lobby in running:
            try:
                lobby.process.terminate()
            except Exception:
                pass


def main() -> None:
    global HOST, PORT, LOBBY_BASE_PORT, ROOM_BASE_PORT
    parser = argparse.ArgumentParser(description="Central lobby server")
    parser.add_argument("--host", default=HOST, help="Host to bind")
    parser.add_argument("--port", type=int, default=PORT, help="Port to bind")
    parser.add_argument("--lobby-base-port", type=int, default=LOBBY_BASE_PORT)
    parser.add_argument("--room-base-port", type=int, default=ROOM_BASE_PORT)
    args = parser.parse_args()

    HOST = args.host
    PORT = args.port
    LOBBY_BASE_PORT = args.lobby_base_port
    ROOM_BASE_PORT = args.room_base_port

    serve_forever(HOST, PORT)


if __name__ == "__main__":
    main()
