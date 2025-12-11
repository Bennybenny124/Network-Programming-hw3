"""Dummy pygame client that connects to the room server and shows a window."""

from __future__ import annotations

import argparse
import json
import socket
import sys

import pygame


def connect_and_join(host: str, port: int, username: str) -> dict:
    payload = {"type": "room", "action": "join", "data": {"username": username}}
    with socket.create_connection((host, port), timeout=5) as sock:
        reader = sock.makefile("rb")
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        line = reader.readline()
        if not line:
            raise ConnectionError("Room server closed connection")
        return json.loads(line.decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Dummy pygame client")
    parser.add_argument("--room-host", required=True)
    parser.add_argument("--room-port", type=int, required=True)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()

    try:
        resp = connect_and_join(args.room_host, args.room_port, args.username)
        if resp.get("status") != "ok":
            raise RuntimeError(resp.get("error", {}).get("message", "Join failed"))
    except Exception as exc:  # pragma: no cover - surface to user
        print(f"Failed to connect or join room: {exc}")
        sys.exit(1)

    pygame.init()
    screen = pygame.display.set_mode((800, 600))
    pygame.display.set_caption("Dummy Game")
    font = pygame.font.SysFont("Arial", 32)
    clock = pygame.time.Clock()
    message = f"Connected to room as {args.username}"

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
        screen.fill((30, 30, 30))
        text = font.render(message, True, (200, 230, 255))
        screen.blit(text, (60, 280))
        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


if __name__ == "__main__":
    main()
