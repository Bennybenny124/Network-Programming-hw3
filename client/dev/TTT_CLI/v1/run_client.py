"""TicTacToe CLI client (no pygame)."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time

GRID_SIZE = 3


def send_json(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def format_board(board: list[str]) -> str:
    cells = []
    for idx, val in enumerate(board):
        mark = val if val else str(idx + 1)
        cells.append(mark)
    lines = []
    for r in range(GRID_SIZE):
        lines.append(" | ".join(cells[r * GRID_SIZE : (r + 1) * GRID_SIZE]))
    return "\n---------\n".join(lines)


def recv_thread(sock: socket.socket, state: dict, stop_event: threading.Event) -> None:
    reader = sock.makefile("rb")
    try:
        while not stop_event.is_set():
            try:
                line = reader.readline()
            except (ConnectionResetError, TimeoutError, OSError):
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
                state["symbol"] = data.get("symbol", "X")
            elif action == "state":
                state.update(data)
            elif action == "closing":
                stop_event.set()
                break
    finally:
        reader.close()


def prompt_move(board: list[str]) -> int | None:
    while True:
        raw = input("Enter your move (1-9) or q to quit: ").strip().lower()
        if raw in ("q", "quit"):
            return None
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < 9 and not board[idx]:
                return idx
        print("Invalid move.")


def prompt_again() -> bool:
    while True:
        ans = input("Play again? (y/n): ").strip().lower()
        if ans in ("y", "Y", "yes"):
            return True
        if ans in ("n", "N", "no"):
            return False
        print("Enter y or n.")


def main() -> None:
    parser = argparse.ArgumentParser(description="TicTacToe CLI client")
    parser.add_argument("--room-host", required=True)
    parser.add_argument("--room-port", type=int, required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()

    sock = socket.create_connection((args.room_host, args.room_port), timeout=5)
    sock.settimeout(None)
    state = {"board": [""] * 9, "turn": None, "winner": None, "symbol": None, "username": args.username}
    send_json(sock, {"type": "room", "action": "join", "data": {"username": args.username}})

    stop_event = threading.Event()
    threading.Thread(target=recv_thread, args=(sock, state, stop_event), daemon=True).start()

    last_board: tuple | None = None
    last_need: int | None = None
    try:
        while not stop_event.is_set():
            board_tuple = tuple(state.get("board", [""] * 9))
            if board_tuple != last_board:
                last_board = board_tuple
                print("\n" + format_board(list(board_tuple)))
            need = state.get("players_needed", 0)
            if need and not state.get("winner"):
                if need != last_need:
                    print(f"Waiting for opponent ({need} more)...")
                last_need = need
            if state.get("winner") is not None:
                win = state.get("winner")
                if win == "":
                    print("It's a draw.")
                else:
                    print(f"{win} wins!")
                again = prompt_again()
                send_json(sock, {"type": "room", "action": "play_again", "data": {"again": again}})
                if not again:
                    break
                # wait for server reset/state
                continue
            if state.get("players_needed", 0):
                time.sleep(0.2)
                continue
            if state.get("turn") == args.username:
                board = state.get("board", [""] * 9)
                move = prompt_move(board)
                if move is None:
                    send_json(sock, {"type": "room", "action": "play_again", "data": {"again": False}})
                    break
                # optimistic local update for immediate feedback
                if 0 <= move < len(board) and not board[move]:
                    board[move] = state.get("symbol", "")
                    state["board"] = board
                    state["turn"] = None
                send_json(sock, {"type": "room", "action": "move", "data": {"cell": move}})
            else:
                time.sleep(0.2)
    finally:
        stop_event.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        sock.close()
    print("Goodbye.")


if __name__ == "__main__":
    main()
