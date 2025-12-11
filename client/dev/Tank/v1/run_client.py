"""Tank game pygame client."""

from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time

import pygame

WIDTH, HEIGHT = 800, 600
BG_COLOR = (24, 24, 28)
TANK_COLOR = (70, 170, 240)
ENEMY_COLOR = (230, 90, 90)
BULLET_COLOR = (250, 240, 120)
TURRET_COLOR = (200, 220, 255)


def send_json(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def recv_thread(sock: socket.socket, state: dict, stop_event: threading.Event) -> None:
    reader = sock.makefile("rb")
    try:
        while not stop_event.is_set():
            try:
                line = reader.readline()
            except (ConnectionResetError, BrokenPipeError, OSError):
                break
            if not line:
                break
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "room" and msg.get("action") == "state":
                state.update(msg.get("data", {}))
    finally:
        reader.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Tank pygame client")
    parser.add_argument("--room-host", required=True)
    parser.add_argument("--room-port", type=int, required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()

    sock = socket.create_connection((args.room_host, args.room_port), timeout=5)
    send_json(sock, {"type": "room", "action": "join", "data": {"username": args.username}})

    state = {"players": [], "bullets": []}
    stop_event = threading.Event()
    threading.Thread(target=recv_thread, args=(sock, state, stop_event), daemon=True).start()

    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption(f"Tank - {args.username}")
    clock = pygame.time.Clock()
    running = True

    def send_input(move, turret_delta, fire):
        try:
            send_json(
                sock,
                {
                    "type": "room",
                    "action": "input",
                    "data": {"username": args.username, "move": move, "turret_delta": turret_delta, "fire": fire},
                },
            )
        except Exception:
            pass

    fire_cooldown = 0.0

    while running:
        dt = clock.tick(60) / 1000.0
        fire_cooldown = max(0.0, fire_cooldown - dt)
        move = [0.0, 0.0]
        turret_delta = 0.0
        fire = False
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        keys = pygame.key.get_pressed()
        if keys[pygame.K_w] or keys[pygame.K_UP]:
            move[1] -= 1
        if keys[pygame.K_s] or keys[pygame.K_DOWN]:
            move[1] += 1
        if keys[pygame.K_a] or keys[pygame.K_LEFT]:
            move[0] -= 1
        if keys[pygame.K_d] or keys[pygame.K_RIGHT]:
            move[0] += 1
        if keys[pygame.K_q]:
            turret_delta -= 1
        if keys[pygame.K_e]:
            turret_delta += 1
        if keys[pygame.K_SPACE] and fire_cooldown == 0.0:
            fire = True
            fire_cooldown = 0.25

        send_input(move, turret_delta, fire)

        screen.fill(BG_COLOR)

        # draw bullets
        for b in state.get("bullets", []):
            pygame.draw.circle(screen, BULLET_COLOR, (int(b.get("x", 0)), int(b.get("y", 0))), 5)

        # draw players
        for p in state.get("players", []):
            if not isinstance(p, dict):
                continue
            color = TANK_COLOR if p.get("username") == args.username else ENEMY_COLOR
            if not p.get("alive", True):
                color = (120, 120, 120)
            x, y = p.get("x", 0), p.get("y", 0)
            pygame.draw.circle(screen, color, (int(x), int(y)), 16)
            ang = math.radians(p.get("angle_turret", 0))
            end = (int(x + math.cos(ang) * 24), int(y + math.sin(ang) * 24))
            pygame.draw.line(screen, TURRET_COLOR, (int(x), int(y)), end, 3)
            label = pygame.font.SysFont(None, 20).render(p.get("username", ""), True, (240, 240, 240))
            screen.blit(label, (x - label.get_width() / 2, y - 30))

        pygame.display.flip()

    stop_event.set()
    try:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        sock.close()
    except Exception:
        pass
    pygame.quit()


if __name__ == "__main__":
    main()
