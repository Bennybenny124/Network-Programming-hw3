"""TicTacToe pygame client."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import pygame

WIDTH, HEIGHT = 360, 420
GRID_SIZE = 3
CELL_SIZE = 100
MARGIN = 30
BG = (240, 240, 240)
LINE = (50, 50, 50)
X_COLOR = (200, 60, 60)
O_COLOR = (60, 120, 200)
TEXT = (20, 20, 20)


def send_json(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


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


def draw_board(screen, state, font):
    screen.fill(BG)
    # grid lines
    for i in range(1, GRID_SIZE):
        pygame.draw.line(screen, LINE, (MARGIN, MARGIN + i * CELL_SIZE), (MARGIN + GRID_SIZE * CELL_SIZE, MARGIN + i * CELL_SIZE), 2)
        pygame.draw.line(screen, LINE, (MARGIN + i * CELL_SIZE, MARGIN), (MARGIN + i * CELL_SIZE, MARGIN + GRID_SIZE * CELL_SIZE), 2)
    # marks
    board = state.get("board", [""] * 9)
    for idx, mark in enumerate(board):
        row, col = divmod(idx, GRID_SIZE)
        cx = MARGIN + col * CELL_SIZE + CELL_SIZE // 2
        cy = MARGIN + row * CELL_SIZE + CELL_SIZE // 2
        if mark == "X":
            color = X_COLOR
            offset = 30
            pygame.draw.line(screen, color, (cx - offset, cy - offset), (cx + offset, cy + offset), 4)
            pygame.draw.line(screen, color, (cx - offset, cy + offset), (cx + offset, cy - offset), 4)
        elif mark == "O":
            color = O_COLOR
            pygame.draw.circle(screen, color, (cx, cy), 40, 4)

    turn = state.get("turn")
    need = state.get("players_needed", 0)
    winner = state.get("winner")
    symbol = state.get("symbol", "?")
    status = ""
    if need and not winner:
        status = f"Waiting for opponent ({need} more)"
    elif winner:
        if winner == "":
            status = "Draw! Y to replay, N to quit"
        else:
            status = f"{winner} wins! Y to replay, N to quit"
    else:
        if turn == state.get("username"):
            status = f"Your turn ({symbol})"
        elif turn:
            status = f"Waiting for {turn}"
        else:
            status = "Waiting for opponent"
    text_surface = font.render(status, True, TEXT)
    screen.blit(text_surface, (MARGIN, HEIGHT - 60))


def main() -> None:
    parser = argparse.ArgumentParser(description="TicTacToe client")
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

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption(f"TicTacToe - {args.username}")
    font = pygame.font.SysFont(None, 22)
    clock = pygame.time.Clock()
    running = True
    while running and not stop_event.is_set():
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
                break
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and not state.get("winner"):
                x, y = event.pos
                if MARGIN <= x <= MARGIN + CELL_SIZE * GRID_SIZE and MARGIN <= y <= MARGIN + CELL_SIZE * GRID_SIZE:
                    col = (x - MARGIN) // CELL_SIZE
                    row = (y - MARGIN) // CELL_SIZE
                    idx = row * GRID_SIZE + col
                    board = state.get("board", [""] * 9)
                    if not board[idx] and not state.get("players_needed", 0):
                        # optimistic local update for snappier UI; server will echo authoritative state
                        sym = state.get("symbol")
                        if sym:
                            board[idx] = sym
                        state["turn"] = None
                        state["board"] = board
                    send_json(sock, {"type": "room", "action": "move", "data": {"cell": idx}})
            if event.type == pygame.KEYDOWN and state.get("winner") is not None:
                if event.key == pygame.K_y:
                    send_json(sock, {"type": "room", "action": "play_again", "data": {"again": True}})
                elif event.key == pygame.K_n:
                    send_json(sock, {"type": "room", "action": "play_again", "data": {"again": False}})
                    running = False
                    break

        draw_board(screen, state, font)
        pygame.display.flip()
        clock.tick(60)

    stop_event.set()
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    sock.close()
    pygame.quit()


if __name__ == "__main__":
    main()
