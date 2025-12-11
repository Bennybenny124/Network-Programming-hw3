"""Lightweight JSON-based storage helpers used by the central lobby server.

The module keeps all data in the server/db directory.
It is intentionally simple and is not a standalone network service.
All functions are thread-safe enough for the threaded servers in this project.
"""

from __future__ import annotations

import json
import threading
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "db" / "data"
STORAGE_DIR = BASE_DIR / "db" / "storage"

USERS_FILE = DATA_DIR / "users.json"
GAMES_FILE = DATA_DIR / "games.json"
COMMENTS_FILE = DATA_DIR / "comments.json"

_lock = threading.RLock()


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    if not path.exists() or path.stat().st_size == 0:
        return default
    with path.open("r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


# User helpers
def get_user(username: str) -> Optional[Dict]:
    users: List[Dict] = _load_json(USERS_FILE, [])
    for user in users:
        if user.get("username") == username:
            return user
    return None


def register_user(username: str, password: str) -> Tuple[bool, Optional[str]]:
    invalid_chars = set('<>:."/\\|?*')
    if any(ch in invalid_chars for ch in username):
        return False, "INVALID_USERNAME"
    with _lock:
        users: List[Dict] = _load_json(USERS_FILE, [])
        if any(u.get("username") == username for u in users):
            return False, "USERNAME_EXISTS"
        users.append(
            {
                "username": username,
                "password": password,
                "games": [],
                "games_own": [],
            }
        )
        _save_json(USERS_FILE, users)
    return True, None


def authenticate_user(username: str, password: str) -> bool:
    user = get_user(username)
    return bool(user and user.get("password") == password)


def record_download(username: str, game_name: str) -> None:
    if not username:
        return
    with _lock:
        users: List[Dict] = _load_json(USERS_FILE, [])
        changed = False
        for user in users:
            if user.get("username") == username:
                games = user.setdefault("games", [])
                if game_name not in games:
                    games.append(game_name)
                    changed = True
        if changed:
            _save_json(USERS_FILE, users)


def _add_owned_game(username: Optional[str], game_name: str) -> None:
    if not username:
        return
    with _lock:
        users: List[Dict] = _load_json(USERS_FILE, [])
        changed = False
        for user in users:
            if user.get("username") == username:
                games_own = user.setdefault("games_own", [])
                if game_name not in games_own:
                    games_own.append(game_name)
                    changed = True
        if changed:
            _save_json(USERS_FILE, users)


# Game helpers
def list_games() -> List[Dict]:
    return _load_json(GAMES_FILE, [])


def get_game(game_name: str) -> Optional[Dict]:
    games: List[Dict] = _load_json(GAMES_FILE, [])
    for game in games:
        if game.get("game_name") == game_name:
            return game
    return None


def ensure_game_storage_dir(game_name: str) -> Path:
    path = STORAGE_DIR / game_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def upsert_game(
    game_name: str,
    version: str,
    filename: str,
    author: Optional[str],
    description: str = "",
    extracted_path: Optional[str] = None,
    min_players: Optional[int] = None,
    max_players: Optional[int] = None,
) -> Dict:
    """Insert or update a game's metadata and return the stored object."""
    with _lock:
        games: List[Dict] = _load_json(GAMES_FILE, [])
        stored_path = str(STORAGE_DIR / game_name / filename)
        existing = None
        for game in games:
            if game.get("game_name") == game_name:
                existing = game
                break

        if existing:
            existing.update(
                {
                    "version": version,
                    "filename": filename,
                    "storage_path": stored_path,
                    "description": description or existing.get("description", ""),
                    "author": existing.get("author") or author,
                    "extracted_path": extracted_path or existing.get("extracted_path"),
                    "min_players": min_players or existing.get("min_players") or 1,
                    "max_players": max_players or existing.get("max_players") or 4,
                }
            )
        else:
            existing = {
                "game_name": game_name,
                "version": version,
                "filename": filename,
                "storage_path": stored_path,
                "description": description,
                "author": author,
                "rating": None,
                "extracted_path": extracted_path,
                "min_players": min_players or 1,
                "max_players": max_players or 4,
            }
            games.append(existing)

        _save_json(GAMES_FILE, games)
    _add_owned_game(author, game_name)
    return existing


# Comment helpers (not heavily used in dummy phase)
def list_comments(game_name: str) -> List[Dict]:
    raw = _load_json(COMMENTS_FILE, [])
    if not isinstance(raw, list):
        return []
    filtered: List[Dict] = []
    for c in raw:
        if isinstance(c, dict) and c.get("game_name") == game_name:
            filtered.append(c)
    return filtered


def add_comment(game_name: str, username: str, score: int, comment: str) -> None:
    with _lock:
        comments = _load_json(COMMENTS_FILE, [])
        if not isinstance(comments, list):
            comments = []
        # prevent duplicate comment per user per game
        comments = [c for c in comments if not (isinstance(c, dict) and c.get("game_name") == game_name and c.get("username") == username)]
        comments.append(
            {
                "game_name": game_name,
                "username": username,
                "score": score,
                "comment": comment,
            }
        )
        _save_json(COMMENTS_FILE, comments)


def remove_game(game_name: str, author: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """Remove a game and all related data. Optionally verify the author."""
    with _lock:
        games: List[Dict] = _load_json(GAMES_FILE, [])
        target = None
        for g in games:
            if g.get("game_name") == game_name:
                target = g
                break
        if not target:
            return False, "GAME_NOT_FOUND"
        if author and target.get("author") and target.get("author") != author:
            return False, "NOT_OWNER"
        # remove from games list
        games = [g for g in games if g.get("game_name") != game_name]
        _save_json(GAMES_FILE, games)

        # prune users' ownership/download records
        users: List[Dict] = _load_json(USERS_FILE, [])
        changed_users = False
        for user in users:
            games_list = user.get("games") or []
            games_own = user.get("games_own") or []
            new_games = [g for g in games_list if g != game_name]
            new_games_own = [g for g in games_own if g != game_name]
            if new_games != games_list or new_games_own != games_own:
                user["games"] = new_games
                user["games_own"] = new_games_own
                changed_users = True
        if changed_users:
            _save_json(USERS_FILE, users)

        # prune comments
        comments = _load_json(COMMENTS_FILE, [])
        if isinstance(comments, list):
            comments = [c for c in comments if not (isinstance(c, dict) and c.get("game_name") == game_name)]
            _save_json(COMMENTS_FILE, comments)

    # remove storage directory outside of lock
    try:
        shutil.rmtree(STORAGE_DIR / game_name, ignore_errors=True)
    except Exception:
        pass
    return True, None


# Initialization helpers
def initialize_storage() -> None:
    """Ensure all expected directories and json files exist."""
    _ensure_dirs()
    for path, default in [
        (USERS_FILE, []),
        (GAMES_FILE, []),
        (COMMENTS_FILE, []),
    ]:
        if not path.exists():
            _save_json(path, default)
