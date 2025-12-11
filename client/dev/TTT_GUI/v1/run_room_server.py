"""TicTacToe room server (2 players, pygame client compatible)."""

from __future__ import annotations

import argparse
import json
import socket
import threading
from typing import Dict, Tuple


def log(msg: str) -> None:
    print(f"[TicTacToe room] {msg}")


def send_json(conn: socket.socket, payload: Dict) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


class GameState:
    def __init__(self) -> None:
        self.board = [""] * 9
        self.players: Dict[str, str] = {}  # username -> symbol
        self.connections: Dict[str, socket.socket] = {}
        self.turn: str | None = None  # username whose turn
        self.winner: str | None = None
        self.lock = threading.RLock()
        self.active = True
        self.play_again_votes: Dict[str, bool] = {}

    def add_player(self, username: str, conn: socket.socket) -> str | None:
        with self.lock:
            if len(self.players) >= 2:
                return None
            symbol = "X" if "X" not in self.players.values() else "O"
            self.players[username] = symbol
            self.connections[username] = conn
            # only start when two players have joined
            if len(self.players) == 2 and not self.turn:
                self.turn = list(self.players.keys())[0]
            return symbol

    def remove_player(self, username: str) -> None:
        with self.lock:
            self.players.pop(username, None)
            self.connections.pop(username, None)
            self.play_again_votes.pop(username, None)
            if username == self.turn:
                self.turn = None

    def reset(self) -> None:
        with self.lock:
            self.board = [""] * 9
            self.winner = None
            self.play_again_votes = {}
            # X always starts; find username with X, otherwise first player
            turn_candidate = None
            for user, sym in self.players.items():
                if sym == "X":
                    turn_candidate = user
                    break
            if not turn_candidate and self.players:
                turn_candidate = list(self.players.keys())[0]
            self.turn = turn_candidate


def check_winner(board: list[str]) -> str | None:
    lines = [
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    ]
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


def broadcast(state: GameState, payload: Dict) -> None:
    dead = []
    with state.lock:
        for user, conn in list(state.connections.items()):
            try:
                send_json(conn, payload)
            except Exception:
                dead.append(user)
        for user in dead:
            conn = state.connections.pop(user, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            state.remove_player(user)
        # if someone left, clear board/turn and wait for another player
        if dead and len(state.players) < 2:
            state.board = [""] * 9
            state.winner = None
            state.play_again_votes = {}
            state.turn = None


def send_state(state: GameState) -> None:
    with state.lock:
        payload = {
            "type": "room",
            "action": "state",
            "data": {
                "board": state.board,
                "turn": state.turn,
                "winner": state.winner,
                "players": state.players,
                "players_needed": max(0, 2 - len(state.players)),
                "play_again_waiting": bool(state.winner or all(cell for cell in state.board)),
            },
        }
    broadcast(state, payload)


def handle_client(conn: socket.socket, addr: Tuple[str, int], state: GameState) -> None:
    log(f"Client connected: {addr}")
    username = None
    reader = conn.makefile("rb")
    try:
        while True:
            try:
                line = reader.readline()
            except (ConnectionResetError, OSError):
                break
            if not line:
                break
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
                username = data.get("username") or f"player_{len(state.players)+1}"
                symbol = state.add_player(username, conn)
                if not symbol:
                    send_json(conn, {"type": "room", "action": "join", "status": "error", "error": {"message": "Room full"}})
                    break
                send_json(conn, {"type": "room", "action": "join", "status": "ok", "data": {"symbol": symbol, "username": username}})
                send_state(state)
            elif action == "move" and username:
                idx = int(data.get("cell", -1))
                with state.lock:
                    if len(state.players) < 2:
                        continue
                    if state.winner or all(cell for cell in state.board):
                        continue
                    if idx < 0 or idx >= 9:
                        continue
                    if state.turn != username:
                        continue
                    if state.board[idx]:
                        continue
                    state.board[idx] = state.players.get(username, "")
                    # swap turn
                    others = [u for u in state.players if u != username]
                    state.turn = others[0] if others else None
                    win_symbol = check_winner(state.board)
                    if win_symbol:
                        # map symbol back to username
                        for u, sym in state.players.items():
                            if sym == win_symbol:
                                state.winner = u
                                break
                    elif all(cell for cell in state.board):
                        state.winner = ""  # draw marker
                send_state(state)
            elif action == "play_again" and username:
                again = bool(data.get("again"))
                with state.lock:
                    state.play_again_votes[username] = again
                    if len(state.play_again_votes) < len(state.players):
                        continue
                    if not all(state.play_again_votes.values()):
                        state.active = False
                        break
                    state.reset()
                send_state(state)
    finally:
        reader.close()
        conn.close()
        if username:
            state.remove_player(username)
            send_state(state)
        log(f"Client disconnected: {addr}")


def main() -> None:
    parser = argparse.ArgumentParser(description="TicTacToe room server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--max-players", type=int, default=2)
    parser.add_argument("--game-name", required=True)
    parser.add_argument("--room-id", required=True)
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.settimeout(1.0)
    server.bind((args.host, args.port))
    server.listen()
    log(f"TicTacToe room {args.room_id} for {args.game_name} on {args.host}:{args.port}")
    state = GameState()
    try:
        while state.active:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle_client, args=(conn, addr, state), daemon=True).start()
    except KeyboardInterrupt:
        log("Stopping room server")
    finally:
        server.close()
    log("Room server exiting")


if __name__ == "__main__":
    main()
