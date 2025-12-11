"""Tkinter client for the dummy game store system."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import zipfile


CENTRAL_HOST = "linux2.cs.nycu.edu.tw"
CENTRAL_PORT = 12345

ROOT_DIR = Path(__file__).resolve().parent.parent
DEV_DIR = ROOT_DIR / "dev"
PLAY_DIR = ROOT_DIR / "play"


def player_root(username: str | None) -> Path:
    return PLAY_DIR / username

class CentralConnection:
    """Single TCP connection to the central lobby."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sock = socket.create_connection((host, port))
        self.reader = self.sock.makefile("rb")
        self.lock = threading.Lock()
        self.username: str | None = None


    def close(self) -> None:
        try:
            self.reader.close()
        finally:
            self.sock.close()

    def request(self, payload: dict) -> dict:
        line = (json.dumps(payload) + "\n").encode("utf-8")
        with self.lock:
            self.sock.sendall(line)
            resp_line = self.reader.readline()
        if not resp_line:
            raise ConnectionError("Connection closed by server")
        return json.loads(resp_line.decode("utf-8"))

    def upload_game_file(self, game_name: str, version: str, file_path: Path, min_players: int | None = None, max_players: int | None = None) -> dict:
        data = file_path.read_bytes()
        header = {
            "type": "dev",
            "action": "upload_game_file",
            "data": {
                "game_name": game_name,
                "version": version,
                "filename": file_path.name,
                "filesize": len(data),
                "min_players": min_players,
                "max_players": max_players,
            },
        }
        header_line = (json.dumps(header) + "\n").encode("utf-8")
        with self.lock:
            self.sock.sendall(header_line)
            self.sock.sendall(data)
            resp_line = self.reader.readline()
        if not resp_line:
            raise ConnectionError("Connection closed during upload")
        return json.loads(resp_line.decode("utf-8"))

    def download_game_file(self, game_name: str, dest_dir: Path) -> tuple[dict, Path | None]:
        header_req = {"type": "store", "action": "download_game_file", "data": {"game_name": game_name}}
        header_line = (json.dumps(header_req) + "\n").encode("utf-8")
        with self.lock:
            self.sock.sendall(header_line)
            resp_line = self.reader.readline()
            if not resp_line:
                raise ConnectionError("Connection closed while waiting for download header")
            header = json.loads(resp_line.decode("utf-8"))
            if header.get("status") != "ok":
                return header, None
            data = header.get("data", {})
            filesize = data.get("filesize")
            filename = data.get("filename")
            if not isinstance(filesize, int) or not filename:
                return {"status": "error", "error": {"code": "INVALID_HEADER", "message": "Bad header"}}, None
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_path = dest_dir / filename
            remaining = filesize
            with open(dest_path, "wb") as fh:
                while remaining > 0:
                    chunk = self.reader.read(min(4096, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
            if remaining != 0:
                return {
                    "status": "error",
                    "error": {"code": "DOWNLOAD_INCOMPLETE", "message": "Did not receive full file"},
                }, None
        return header, dest_path


def launch_lobby(conn: CentralConnection, game_name: str) -> tuple[bool, str | tuple[str, int]]:
    req = {"type": "dev", "action": "launch_game_server", "data": {"game_name": game_name}}
    resp = conn.request(req)
    if resp.get("status") == "ok":
        data = resp.get("data", {})
        return True, (data.get("lobby_host"), data.get("lobby_port"))
    return False, resp.get("error", {}).get("message", "Failed to launch lobby")


def lobby_request(host: str, port: int, payload: dict) -> dict:
    with socket.create_connection((host, port)) as sock:
        reader = sock.makefile("rb")
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        line = reader.readline()
        if not line:
            raise ConnectionError("Lobby closed connection")
        return json.loads(line.decode("utf-8"))


def join_room_server(room_host: str, room_port: int, username: str) -> dict:
    payload = {"type": "room", "action": "join", "data": {"username": username}}
    last_exc = None
    for _ in range(3):
        try:
            with socket.create_connection((room_host, room_port), timeout=3) as sock:
                reader = sock.makefile("rb")
                sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                line = reader.readline()
                if not line:
                    raise ConnectionError("Room server closed connection")
                return json.loads(line.decode("utf-8"))
        except OSError as exc:
            last_exc = exc
            time.sleep(0.3)
    raise ConnectionError(f"Failed to connect to room server: {last_exc}")


class LoginFrame(ttk.Frame):
    def __init__(self, app: "GameStoreApp", conn: CentralConnection) -> None:
        super().__init__(app)
        self.app = app
        self.conn = conn
        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()

        ttk.Label(self, text="Game Store Login").grid(row=0, column=0, columnspan=2, pady=8)
        ttk.Label(self, text="Username").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(self, textvariable=self.username_var).grid(row=1, column=1, padx=4, pady=4)
        ttk.Label(self, text="Password").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        ttk.Entry(self, textvariable=self.password_var, show="*").grid(row=2, column=1, padx=4, pady=4)

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="Login", command=self.login).grid(row=0, column=0, padx=5)
        ttk.Button(btn_frame, text="Register", command=self.register).grid(row=0, column=1, padx=5)

    def login(self) -> None:
        self._submit("login")

    def register(self) -> None:
        self._submit("register")

    def _submit(self, action: str) -> None:
        username = self.username_var.get().strip()
        password = self.password_var.get().strip()
        if not username or not password:
            messagebox.showerror("Error", "Please enter username and password")
            return

        def work():
            payload = {
                "type": "auth",
                "action": action,
                "data": {"username": username, "password": password},
            }
            return self.conn.request(payload)

        def done(resp, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if resp.get("status") == "ok":
                if action == "login":
                    self.conn.username = username
                    self.app.on_login(username)
                else:
                    messagebox.showinfo("Registered", "Registration successful, please login.")
            else:
                err_msg = resp.get("error", {}).get("message", "Unknown error")
                messagebox.showerror("Error", err_msg)

        self.app.run_async(work, done)


class StoreFrame(ttk.Frame):
    def __init__(self, app: "GameStoreApp", conn: CentralConnection) -> None:
        super().__init__(app)
        self.app = app
        self.conn = conn
        self.username_var = tk.StringVar()
        self.selected_game = tk.StringVar()
        self.games: list[dict] = []
        self.dev_mode = tk.BooleanVar(value=False)
        self.rooms_output = tk.StringVar()
        self.game_radio_buttons: dict[str, ttk.Radiobutton] = {}
        self.player_lobbies: dict[str, tuple[str, int]] = {}
        self.dev_lobbies: dict[str, tuple[str, int]] = {}
        self.player_active: bool = False
        self.room_membership: dict[str, str] = {}
        self.running_clients: dict[str, subprocess.Popen] = {}
        self.details_holder = ttk.Frame(self)
        self.room_controls_holder = ttk.Frame(self)

        top = ttk.Frame(self)
        top.pack(fill="x", pady=6)
        ttk.Label(top, textvariable=self.username_var).pack(side="left", padx=6)
        self.refresh_btn = ttk.Button(top, text="Refresh Games", command=self.refresh_games)
        self.refresh_btn.pack(side="left", padx=4)
        self.dev_check = ttk.Checkbutton(top, text="Developer mode", variable=self.dev_mode, command=self.toggle_dev)
        self.dev_check.pack(side="left", padx=4)
        ttk.Button(top, text="Logout", command=self.app.logout).pack(side="right", padx=6)

        self.games_container = ttk.Frame(self)
        self.games_container.pack(fill="both", expand=True, padx=6, pady=6)

        self.room_controls = ttk.LabelFrame(self.room_controls_holder, text="Room controls")
        ttk.Label(self.room_controls, text="Room ID").grid(row=0, column=0, padx=4, pady=4)
        self.room_id_var = tk.StringVar()
        ttk.Entry(self.room_controls, textvariable=self.room_id_var, width=10).grid(row=0, column=1, padx=4, pady=4)
        ttk.Label(self.room_controls, text="Players").grid(row=0, column=2, padx=4, pady=4)
        self.room_players_var = tk.IntVar(value=2)
        ttk.Entry(self.room_controls, textvariable=self.room_players_var, width=6).grid(row=0, column=3, padx=4, pady=4)
        ttk.Button(self.room_controls, text="Create Room", command=self.create_room).grid(row=0, column=4, padx=4, pady=4)
        ttk.Button(self.room_controls, text="List Rooms", command=self.list_rooms).grid(row=0, column=5, padx=4, pady=4)
        ttk.Button(self.room_controls, text="Join Room", command=self.join_room).grid(row=0, column=6, padx=4, pady=4)
        ttk.Label(self.room_controls, textvariable=self.rooms_output, foreground="gray").grid(
            row=1, column=0, columnspan=7, sticky="w", padx=4, pady=4
        )

        self.dev_frame = ttk.LabelFrame(self, text="Developer upload / update")
        self.dev_frame.pack(fill="x", padx=6, pady=6)
        self.dev_frame.pack_forget()
        self.dev_game_var = tk.StringVar()
        self.dev_version_var = tk.StringVar()
        self.dev_min_var = tk.IntVar(value=1)
        self.dev_max_var = tk.IntVar(value=4)
        self.dev_file_var = tk.StringVar()
        ttk.Label(self.dev_frame, text="Game name").grid(row=0, column=0, padx=4, pady=4, sticky="e")
        ttk.Entry(self.dev_frame, textvariable=self.dev_game_var, width=20).grid(row=0, column=1, padx=4, pady=4)
        ttk.Label(self.dev_frame, text="Version").grid(row=1, column=0, padx=4, pady=4, sticky="e")
        ttk.Entry(self.dev_frame, textvariable=self.dev_version_var, width=20).grid(row=1, column=1, padx=4, pady=4)
        ttk.Label(self.dev_frame, text="Players:").grid(row=1, column=2, padx=4, pady=4, sticky="e")
        ttk.Entry(self.dev_frame, textvariable=self.dev_min_var, width=4).grid(row=1, column=3, padx=2, pady=4)
        ttk.Label(self.dev_frame, text="~").grid(row=1, column=4, padx=2, pady=4)
        ttk.Entry(self.dev_frame, textvariable=self.dev_max_var, width=4).grid(row=1, column=5, padx=2, pady=4)
        ttk.Entry(self.dev_frame, textvariable=self.dev_file_var, width=40).grid(row=2, column=0, columnspan=2, padx=4, pady=4)
        ttk.Button(self.dev_frame, text="Choose Zip", command=self.choose_zip).grid(row=2, column=2, padx=4, pady=4)
        ttk.Button(self.dev_frame, text="Upload Zip", command=self.upload_zip).grid(row=3, column=1, padx=4, pady=6)

        self.dev_games_label = ttk.Label(self.dev_frame, text="My games")
        self.dev_games_label.grid(row=4, column=0, columnspan=3, sticky="w", padx=4, pady=(10, 4))
        self.dev_games_container = ttk.Frame(self.dev_frame)
        self.dev_games_container.grid(row=5, column=0, columnspan=3, sticky="nsew", padx=4, pady=4)
        self.dev_frame.grid_rowconfigure(5, weight=1)

        self.details_frame = ttk.LabelFrame(self.details_holder, text="Details")
        self.detail_title = tk.StringVar()
        self.detail_rating = tk.StringVar()
        self.detail_desc = tk.StringVar()
        self.detail_comments = tk.Text(self.details_frame, height=6, width=60, state="disabled")
        ttk.Label(self.details_frame, textvariable=self.detail_title, font=("TkDefaultFont", 10, "bold")).pack(
            anchor="w", padx=4, pady=2
        )
        self.detail_rating_label = ttk.Label(self.details_frame, textvariable=self.detail_rating)
        self.detail_rating_label.pack(anchor="w", padx=4, pady=2)
        self.detail_desc_label = ttk.Label(self.details_frame, textvariable=self.detail_desc, wraplength=500)
        self.detail_desc_label.pack(anchor="w", padx=4, pady=2)
        controls = ttk.Frame(self.details_frame)
        controls.pack(anchor="w", padx=4, pady=4)
        self.detail_start_btn = ttk.Button(controls, text="Start Game", command=self.detail_toggle_game)
        self.detail_start_btn.grid(row=0, column=0, padx=4)
        self.detail_uninstall_btn = ttk.Button(controls, text="Uninstall", command=self.detail_uninstall_game)
        self.detail_uninstall_btn.grid(row=0, column=1, padx=4)
        self.detail_comments.pack(fill="x", padx=4, pady=4)
        comment_form = ttk.Frame(self.details_frame)
        comment_form.pack(fill="x", padx=4, pady=4)
        ttk.Label(comment_form, text="Score (1-5):").grid(row=0, column=0, padx=2, pady=2)
        self.comment_score_var = tk.IntVar(value=5)
        ttk.Entry(comment_form, textvariable=self.comment_score_var, width=5).grid(row=0, column=1, padx=2, pady=2)
        ttk.Label(comment_form, text="Comment:").grid(row=0, column=2, padx=2, pady=2)
        self.comment_text_var = tk.StringVar()
        ttk.Entry(comment_form, textvariable=self.comment_text_var, width=40).grid(row=0, column=3, padx=2, pady=2)
        self.add_comment_btn = ttk.Button(comment_form, text="Add", command=self.add_comment)
        self.add_comment_btn.grid(row=0, column=4, padx=4, pady=2)
        self.details_frame.pack(fill="x", padx=6, pady=6)
        self.details_holder.pack_forget()

    def _set_room_player_default(self, game_name: str) -> None:
        min_p, _ = self._get_game_limits(game_name)
        self.room_players_var.set(min_p)

    def toggle_dev(self) -> None:
        if self.dev_mode.get():
            self.dev_frame.pack(fill="x", padx=6, pady=6)
            self.hide_store_area()
            self.render_dev_games()
            self.hide_details()
        else:
            self.dev_frame.pack_forget()
            self.show_store_area()
            self.show_details_for_selection()
        self.refresh_games()

    def hide_room_controls(self) -> None:
        if self.room_controls.winfo_manager():
            self.room_controls.pack_forget()
        if self.room_controls_holder.winfo_manager():
            self.room_controls_holder.pack_forget()
        self._repack_bottom_sections()
        self._update_radio_locking()

    def hide_store_area(self) -> None:
        self.games_container.pack_forget()
        self.hide_room_controls()
        self._update_radio_locking()

    def show_store_area(self) -> None:
        self.games_container.pack(fill="both", expand=True, padx=6, pady=6)
        self._repack_bottom_sections()
        self._update_radio_locking()

    def leave_all_rooms(self) -> None:
        for game in list(self.room_membership.keys()):
            self._leave_room_on_server(game)
        self.room_membership.clear()
        for proc in list(self.running_clients.values()):
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        self.running_clients.clear()

    def refresh_games(self) -> None:
        def work():
            return self.conn.request({"type": "store", "action": "list_games", "data": {}})

        def done(resp, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if resp.get("status") == "ok":
                self.games = resp.get("data", {}).get("games", [])
                if self.dev_mode.get():
                    self.render_dev_games()
                else:
                    self.selected_game.set("")
                    self.render_games()
                    self.show_details_for_selection()
            else:
                messagebox.showerror("Error", resp.get("error", {}).get("message", "Failed to load games"))

        self.app.run_async(work, done)

    def render_games(self) -> None:
        for child in self.games_container.winfo_children():
            child.destroy()
        self.game_radio_buttons.clear()
        if not self.games:
            ttk.Label(self.games_container, text="No games available").pack(anchor="w")
            return
        for game in self.games:
            frame = ttk.Frame(self.games_container, relief="groove", borderwidth=1)
            frame.pack(fill="x", pady=3, padx=2)
            name = game.get("game_name", "unknown")
            version = game.get("version", "-")
            installed = (player_root(self.conn.username) / name / "game").exists()
            lobby_host = game.get("lobby_host")
            lobby_port = game.get("lobby_port")
            info = f"{name} (v{version})"
            if installed: info += " [installed]"
            rb = ttk.Radiobutton(frame, text=info, variable=self.selected_game, value=name, command=lambda n=name: self.on_select_game(n))
            rb.pack(side="left", padx=4)
            self.game_radio_buttons[name] = rb
            if lobby_host and lobby_port:
                ttk.Label(frame, text=f"Lobby: {lobby_host}:{lobby_port}", foreground="green").pack(
                    side="right", padx=4
                )
        self._update_radio_locking()

    def on_select_game(self, game_name: str) -> None:
        self.selected_game.set(game_name)
        self._set_room_player_default(game_name)
        self.show_details_for_selection()

    def render_dev_games(self) -> None:
        for child in self.dev_games_container.winfo_children():
            child.destroy()
        my_games = [g for g in self.games if g.get("author") == self.conn.username]
        if not my_games:
            ttk.Label(self.dev_games_container, text="No uploaded games").pack(anchor="w")
            return
        for game in my_games:
            frame = ttk.Frame(self.dev_games_container, relief="groove", borderwidth=1)
            frame.pack(fill="x", pady=3, padx=2)
            name = game.get("game_name", "unknown")
            version = game.get("version", "-")
            lobby_host = game.get("lobby_host")
            lobby_port = game.get("lobby_port")
            info = f"{name} (v{version})"
            ttk.Label(frame, text=info).pack(side="left", padx=4)
            is_running = bool(lobby_host and lobby_port) or name in self.dev_lobbies
            if is_running and name not in self.dev_lobbies and lobby_host and lobby_port:
                self.dev_lobbies[name] = (lobby_host, lobby_port)
            btn = ttk.Button(frame, text="Launch", command=lambda n=name: self.dev_toggle_game(n))
            if is_running:
                btn.config(text="Stop")
            btn.pack(side="right", padx=4)
            ttk.Button(frame, text="Delete", command=lambda n=name: self.delete_game(n)).pack(side="right", padx=4)
            if lobby_host and lobby_port:
                ttk.Label(frame, text=f"Lobby: {lobby_host}:{lobby_port}", foreground="green").pack(
                    side="right", padx=4
                )

    def choose_zip(self) -> None:
        initial = str(DEV_DIR) if DEV_DIR.exists() else str(ROOT_DIR)
        path = filedialog.askopenfilename(title="Choose game zip", initialdir=initial, filetypes=[("Zip files", "*.zip"), ("All files", "*.*")])
        if path:
            self.dev_file_var.set(path)

    def upload_zip(self) -> None:
        game_name = self.dev_game_var.get().strip()
        version = self.dev_version_var.get().strip()
        file_path = Path(self.dev_file_var.get())
        try:
            min_players = int(self.dev_min_var.get())
            max_players = int(self.dev_max_var.get())
        except Exception:
            messagebox.showerror("Error", "Players must be integers")
            return
        if not game_name or not version or not file_path.exists():
            messagebox.showerror("Error", "Please provide game name, version, and zip file")
            return
        if min_players < 1 or max_players < min_players:
            messagebox.showerror("Error", "Players range is invalid (min <= max and min >= 1).")
            return

        def work():
            return self.conn.upload_game_file(game_name, version, file_path, min_players, max_players)

        def done(resp, err):
            if err:
                messagebox.showerror("Upload failed", str(err))
                return
            if resp.get("status") == "ok":
                messagebox.showinfo("Upload", "Upload succeeded")
                self.refresh_games()
            else:
                messagebox.showerror("Upload failed", resp.get("error", {}).get("message", "Unknown error"))

        self.app.run_async(work, done)

    def download_game(self, game_name: str) -> None:
        dest_dir = player_root(self.conn.username) / game_name / "game"
        shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)

        def work():
            return self.conn.download_game_file(game_name, dest_dir)

        def done(result, err):
            if err:
                messagebox.showerror("Download failed", str(err))
                return
            header, dest_path = result
            if header.get("status") != "ok":
                messagebox.showerror("Download failed", header.get("error", {}).get("message", "Unknown error"))
                return
            if not dest_path:
                messagebox.showerror("Download failed", "No file path returned")
                return
            try:
                with zipfile.ZipFile(dest_path, "r") as zf:
                    zf.extractall(dest_dir)
                messagebox.showinfo("Download", f"Downloaded and extracted to {dest_dir}")
            except Exception as exc:
                messagebox.showerror("Extract error", str(exc))
            self.refresh_games()

        self.app.run_async(work, done)

    def uninstall_game(self, game_name: str) -> None:
        base_dir = player_root(self.conn.username) / game_name
        try:
            shutil.rmtree(base_dir, ignore_errors=True)
            messagebox.showinfo("Uninstall", f"Removed {base_dir}")
        except Exception as exc:
            messagebox.showerror("Uninstall failed", str(exc))
        self.refresh_games()

    def stop_lobby_server(self, game_name: str) -> None:
        def work():
            payload = {"type": "dev", "action": "stop_game_server", "data": {"game_name": game_name}}
            return self.conn.request(payload)

        def done(resp, err):
            if err:
                messagebox.showerror("Stop failed", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Stop failed", resp.get("error", {}).get("message", "Unknown error"))
                return
            self.dev_lobbies.pop(game_name, None)
            if self.player_active:
                self.player_active = False
            self._set_start_button(game_name, running=False)
            self.hide_room_controls()
            self.room_id_var.set("")
            self.rooms_output.set("")
            self.refresh_games()
            self.render_dev_games()
            self._update_detail_buttons()
            self._update_radio_locking()

        self.app.run_async(work, done)

    def player_toggle_game(self, game_name: str) -> None:
        if self.player_active:
            self.stop_player_game(game_name)
        else:
            self.start_player_game(game_name)

    def start_player_game(self, game_name: str) -> None:
        game_info = next((g for g in self.games if g.get("game_name") == game_name), None)
        if not game_info:
            messagebox.showerror("Error", "Game info not found, refresh and try again.")
            return

        def download_latest() -> dict:
            dest_dir = player_root(self.conn.username) / game_name / "game"
            shutil.rmtree(dest_dir, ignore_errors=True)
            dest_dir.mkdir(parents=True, exist_ok=True)
            header, dest_path = self.conn.download_game_file(game_name, dest_dir)
            if header.get("status") != "ok":
                return {"error": header.get("error", {}).get("message", "Download failed"), "downloaded": True}
            if not dest_path:
                return {"error": "No file path returned", "downloaded": True}
            try:
                with zipfile.ZipFile(dest_path, "r") as zf:
                    zf.extractall(dest_dir)
            except Exception as exc:
                return {"error": str(exc), "downloaded": True}
            return {"ok": True, "downloaded": True}

        def work():
            # ensure latest download
            dest_dir = player_root(self.conn.username) / game_name / "game"
            local_version = None
            cfg_path = dest_dir / "game_config.json"
            if cfg_path.exists():
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    local_version = cfg.get("version")
                except Exception:
                    local_version = None
            if (not dest_dir.exists()) or (local_version != game_info.get("version")):
                return download_latest()
            return {"ok": True, "downloaded": False}

        def done(result, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if result and result.get("error"):
                messagebox.showerror("Error", result.get("error", "Failed to download latest version."))
                return
            host, port = game_info.get("lobby_host"), game_info.get("lobby_port")
            if not host or not port:
                messagebox.showwarning("Lobby not running", "Developer has not launched the game lobby yet.")
                return
            self.player_lobbies[game_name] = (host, port)
            self.player_active = True
            self.app.run_async(lambda: self.conn.request({"type": "store", "action": "mark_owned", "data": {"game_name": game_name}}), lambda *_: None)
            self._repack_bottom_sections()
            self._set_start_button(game_name, running=True)
            self._update_detail_buttons()
            self._update_radio_locking()

        self.app.run_async(work, done)

    def stop_player_game(self, game_name: str) -> None:
        self._leave_room_on_server(game_name)
        proc = self.running_clients.pop(game_name, None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self.player_active:
            self.player_active = False
        self.player_lobbies.pop(game_name, None)
        self._set_start_button(game_name, running=False)
        self.room_id_var.set("")
        self.rooms_output.set("")
        self.hide_room_controls()
        self.room_membership.pop(game_name, None)
        self._update_detail_buttons()
        self._update_radio_locking()

    def _set_start_button(self, game_name: str, running: bool) -> None:
        if self.selected_game.get() == game_name:
            if running:
                self.detail_start_btn.config(text="Stop Game")
            else:
                self.detail_start_btn.config(text="Start Game")
        self._update_radio_locking()

    def _get_lobby_addr(self, game_name: str) -> tuple[str, int] | None:
        if game_name in self.player_lobbies:
            return self.player_lobbies[game_name]
        if game_name in self.dev_lobbies:
            return self.dev_lobbies[game_name]
        game_info = next((g for g in self.games if g.get("game_name") == game_name), None)
        if game_info:
            host, port = game_info.get("lobby_host"), game_info.get("lobby_port")
            if host and port:
                return host, port
        return None

    def _get_game_limits(self, game_name: str) -> tuple[int, int]:
        info = next((g for g in self.games if g.get("game_name") == game_name), {}) or {}
        min_p = info.get("min_players") or 1
        max_p = info.get("max_players") or max(min_p, 4)
        try:
            min_p = int(min_p)
        except Exception:
            min_p = 1
        try:
            max_p = int(max_p)
        except Exception:
            max_p = max(min_p, 4)
        if max_p < min_p:
            max_p = min_p
        return min_p, max_p


    def update_detail_view(self, data: dict) -> None:
        name = data.get("game_name", "Unknown")
        version = data.get("version", "")
        rating = data.get("rating", None)
        desc = data.get("description", "")
        rating_str = "Rating: N/A"
        color = "gray"
        if rating is not None:
            rating_str = f"Rating: {rating:.1f}"
            if rating >= 4:
                color = "green"
            elif rating >= 3:
                color = "goldenrod"
            else:
                color = "red"
        if version is not None:
            self.detail_title.set(f"{name} (v{version})")
        self.detail_rating.set(rating_str)
        self.detail_rating_label.configure(foreground=color)
        if desc is not None:
            self.detail_desc.set(desc)
        comments = data.get("comments", [])
        self.detail_comments.configure(state="normal")
        self.detail_comments.delete("1.0", tk.END)
        for c in comments:
            score = c.get("score", "-")
            user = c.get("username", "?")
            text = c.get("comment", "")
            color_comment = "gray"
            try:
                s_val = float(score)
                if s_val >= 4:
                    color_comment = "score_green"
                elif s_val >= 3:
                    color_comment = "score_yellow"
                else:
                    color_comment = "score_red"
            except Exception:
                pass
            line_prefix = f"[{score}] "
            self.detail_comments.insert(tk.END, line_prefix, (color_comment,))
            self.detail_comments.insert(tk.END, f"{user}: {text}\n")
        self.detail_comments.configure(state="disabled")
        # configure tag colors
        self.detail_comments.tag_configure("score_green", foreground="green")
        self.detail_comments.tag_configure("score_yellow", foreground="goldenrod")
        self.detail_comments.tag_configure("score_red", foreground="red")
        self._update_comment_button_state()
        self._update_detail_buttons()

    def add_comment(self) -> None:
        game = self.selected_game.get()
        if not game:
            messagebox.showwarning("Details", "Select a game first.")
            return
        score = self.comment_score_var.get()
        if score < 1 or score > 5:
            messagebox.showerror("Error", "Score must be between 1 and 5.")
            return
        comment_text = self.comment_text_var.get()

        def work():
            return self.conn.request(
                {"type": "store", "action": "add_comment", "data": {"game_name": game, "score": score, "comment": comment_text}}
            )

        def done(resp, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Error", resp.get("error", {}).get("message", "Failed to add comment"))
                return
            data = resp.get("data", {})
            self.update_detail_view(data)

        self.app.run_async(work, done)

    def hide_details(self) -> None:
        self._update_detail_buttons()

    def show_details_for_selection(self) -> None:
        game = self.selected_game.get()
        if game == "": 
            self.update_detail_view({})
            return
        self._set_room_player_default(game)

        def work():
            return self.conn.request({"type": "store", "action": "get_game_detail", "data": {"game_name": game}})

        def done(resp, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Error", resp.get("error", {}).get("message", "Failed to load detail"))
                return
            data = resp.get("data", {})
            self.update_detail_view(data)
            self._update_comment_button_state()
            self._update_detail_buttons()

        self.app.run_async(work, done)

    def _repack_bottom_sections(self) -> None:
        # ensure details (if visible) always appears before room controls
        # clear current packing
        if self.details_holder.winfo_manager():
            self.details_holder.pack_forget()
        if self.room_controls_holder.winfo_manager():
            self.room_controls_holder.pack_forget()

        if not self.dev_mode.get():
            self.details_holder.pack(fill="x", padx=0, pady=0)
            if not self.details_frame.winfo_manager():
                self.details_frame.pack(fill="x", padx=6, pady=6)
            if self.player_active:
                if not self.room_controls.winfo_manager():
                    self.room_controls.pack(fill="x", padx=6, pady=6)
                self.room_controls_holder.pack(fill="x", padx=0, pady=0)
        self._update_detail_buttons()

    def _update_comment_button_state(self) -> None:
        installed = (player_root(self.conn.username) / self.selected_game.get() / "game").exists()
        if installed:
            self.add_comment_btn.state(["!disabled"])
        else:
            self.add_comment_btn.state(["disabled"])

    def _update_detail_buttons(self) -> None:
        game = self.selected_game.get()
        if not game:
            self.detail_start_btn.state(["disabled"])
            self.detail_uninstall_btn.state(["disabled"])
            return
        running = self.player_active
        self.detail_start_btn.config(text="Stop Game" if running else "Start Game")
        self.detail_start_btn.state(["!disabled"])
        installed = (player_root(self.conn.username) / game / "game").exists()
        if installed and not running:
            self.detail_uninstall_btn.state(["!disabled"])
        else:
            self.detail_uninstall_btn.state(["disabled"])
        self._update_radio_locking()

    def _update_radio_locking(self) -> None:
        locked = bool(self.player_active)
        for rb in self.game_radio_buttons.values():
            if locked:
                rb.state(["disabled"])
            else:
                rb.state(["!disabled"])
        self._update_top_controls()

    def _update_top_controls(self) -> None:
        if self.player_active:
            self.refresh_btn.state(["disabled"])
            self.dev_check.state(["disabled"])
        else:
            self.refresh_btn.state(["!disabled"])
            self.dev_check.state(["!disabled"])

    def dev_toggle_game(self, game_name: str) -> None:
        if game_name in self.dev_lobbies:
            self.stop_lobby_server(game_name)
        else:
            def work():
                return launch_lobby(self.conn, game_name)

            def done(result, err):
                if err:
                    messagebox.showerror("Error", str(err))
                    return
                success, info = result
                if success:
                    host, port = info
                    messagebox.showinfo("Lobby", f"Lobby running at {host}:{port}")
                    self.dev_lobbies[game_name] = (host, port)
                    self.render_dev_games()
                else:
                    messagebox.showerror("Error", str(info))
            self.app.run_async(work, done)

    def delete_game(self, game_name: str) -> None:
        if not messagebox.askyesno("Delete game", f"Are you sure you want to delete {game_name}?"):
            return

        def work():
            payload = {"type": "dev", "action": "delete_game", "data": {"game_name": game_name}}
            return self.conn.request(payload)

        def done(resp, err):
            if err:
                messagebox.showerror("Delete failed", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Delete failed", resp.get("error", {}).get("message", "Failed to delete"))
                return
            self.dev_lobbies.pop(game_name, None)
            if self.player_active:
                self.player_active = False
            self.room_membership.pop(game_name, None)
            self.running_clients.pop(game_name, None)
            self.refresh_games()
            self.render_dev_games()

        self.app.run_async(work, done)

    def detail_toggle_game(self) -> None:
        game = self.selected_game.get()
        if not game:
            messagebox.showwarning("Select game", "Please select a game first")
            return
        self.player_toggle_game(game)
        self._update_detail_buttons()

    def detail_uninstall_game(self) -> None:
        game = self.selected_game.get()
        if not game:
            messagebox.showwarning("Select game", "Please select a game first")
            return
        self.uninstall_game(game)
        self._update_detail_buttons()

    def _ensure_selection(self) -> str | None:
        sel = self.selected_game.get()
        if not sel:
            messagebox.showwarning("Select game", "Please select a game first")
            return None
        return sel

    def list_rooms(self) -> None:
        game = self._ensure_selection()
        if not game:
            return
        if not self.player_active:
            messagebox.showwarning("Start game", "Please start the game first.")
            return

        def work():
            addr = self._get_lobby_addr(game)
            if not addr:
                return {"status": "error", "error": {"message": "Lobby not available"}}
            host, port = addr
            payload = {"type": "lobby", "action": "list_rooms", "data": {"game_name": game}}
            return lobby_request(host, port, payload)

        def done(resp, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Error", resp.get("error", {}).get("message", "List failed"))
                return
            rooms = resp.get("data", {}).get("rooms", [])
            text = "; ".join([f"{r.get('room_id')} ({len(r.get('players', []))}/{r.get('max_players')})" for r in rooms]) or "No rooms"
            self.rooms_output.set(text)

        self.app.run_async(work, done)

    def create_room(self) -> None:
        game = self._ensure_selection()
        if not game:
            return
        if not self.player_active:
            messagebox.showwarning("Start game", "Please start the game first.")
            return
        try:
            requested_players = int(self.room_players_var.get())
        except Exception:
            messagebox.showerror("Players", "Enter a valid player count.")
            return
        min_p, max_p = self._get_game_limits(game)
        if requested_players < min_p or requested_players > max_p:
            messagebox.showerror("Players", f"Players must be between {min_p} and {max_p}.")
            return

        def work():
            addr = self._get_lobby_addr(game)
            if not addr:
                return {"status": "error", "error": {"message": "Lobby not available"}}
            host, port = addr
            payload = {
                "type": "lobby",
                "action": "create_room",
                "data": {"game_name": game, "username": self.conn.username, "max_players": requested_players},
            }
            resp = lobby_request(host, port, payload)
            # small wait to let room server boot
            time.sleep(0.3)
            return resp

        def done(resp, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Error", resp.get("error", {}).get("message", "Create failed"))
                return
            data = resp.get("data", {})
            self.room_id_var.set(data.get("room_id", ""))
            self.rooms_output.set(f"Created room {data.get('room_id')} at {data.get('room_server_host')}:{data.get('room_server_port')}")
            self._connect_room_and_maybe_launch(game, data)

        self.app.run_async(work, done)

    def join_room(self) -> None:
        game = self._ensure_selection()
        if not game:
            return
        room_id = self.room_id_var.get().strip()
        if not room_id:
            messagebox.showwarning("Room ID", "Enter a room ID to join")
            return
        if not self.player_active:
            messagebox.showwarning("Start game", "Please start the game first.")
            return

        def work():
            addr = self._get_lobby_addr(game)
            if not addr:
                return {"status": "error", "error": {"message": "Lobby not available"}}
            host, port = addr
            payload = {"type": "lobby", "action": "join_room", "data": {"room_id": room_id, "username": self.conn.username}}
            return lobby_request(host, port, payload)

        def done(resp, err):
            if err:
                messagebox.showerror("Error", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Error", resp.get("error", {}).get("message", "Join failed"))
                return
            data = resp.get("data", {})
            self.rooms_output.set(f"Joined room {data.get('room_id')} at {data.get('room_server_host')}:{data.get('room_server_port')}")
            self._connect_room_and_maybe_launch(game, data)

        self.app.run_async(work, done)

    def _connect_room_and_maybe_launch(self, game: str, room_info: dict) -> None:
        host = room_info.get("room_server_host")
        port = room_info.get("room_server_port")
        room_id = room_info.get("room_id")
        if not host or not port:
            messagebox.showwarning("Room", "Missing room server info")
            return

        def work():
            return join_room_server(host, port, self.conn.username or "player")

        def detect_game_client_entry(game_name: str) -> Path | None:
            base_dir = player_root(self.conn.username) / game_name / "game"
            config_path = base_dir / "game_config.json"
            entry_name = "game_client.py"
            if config_path.exists():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                    entry_name = cfg.get("entry_client", entry_name)
                except Exception:
                    pass
            entry_path = base_dir / entry_name
            return entry_path if entry_path.exists() else None

        def done(resp, err):
            if err:
                messagebox.showerror("Room error", str(err))
                return
            if resp.get("status") != "ok":
                messagebox.showerror("Room error", resp.get("error", {}).get("message", "Failed to join room server"))
                return
            if room_id:
                self.room_membership[game] = room_id
            entry = detect_game_client_entry(game)
            if entry:
                try:
                    proc = subprocess.Popen(
                        [sys.executable, str(entry), "--room-host", host, "--room-port", str(port), "--username", self.conn.username or "player"],
                        cwd=str(entry.parent),
                    )
                    self.running_clients[game] = proc
                    def watcher():
                        proc.wait()
                        self.app.after(0, lambda: self._on_game_process_exit(game))
                    threading.Thread(target=watcher, daemon=True).start()
                    messagebox.showinfo("Game client", f"Launched game client {entry.name}")
                except Exception as exc:
                    messagebox.showerror("Launch failed", str(exc))
            else:
                messagebox.showinfo("Room connected", "Joined room server (no local game client found)")

        self.app.run_async(work, done)

    def _on_game_process_exit(self, game: str) -> None:
        self.running_clients.pop(game, None)
        self._leave_room_on_server(game)
        self.room_membership.pop(game, None)
        # Keep game marked active so user can create/join another room without re-starting.
        self.rooms_output.set("Game client closed. You may join or create another room.")
        self.room_id_var.set("")

    def _leave_room_on_server(self, game: str) -> None:
        room_id = self.room_membership.get(game)
        addr = self._get_lobby_addr(game)
        if not room_id or not addr:
            return

        def work():
            host, port = addr
            payload = {
                "type": "lobby",
                "action": "leave_room",
                "data": {"room_id": room_id, "username": self.conn.username},
            }
            return lobby_request(host, port, payload)

        def done(resp, err):
            return

        self.app.run_async(work, done)


class GameStoreApp(tk.Tk):
    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self.title("Game Store Client")
        self.restart = False
        self.conn = CentralConnection(host, port)
        self.login_frame = LoginFrame(self, self.conn)
        self.store_frame = StoreFrame(self, self.conn)
        self.login_frame.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_login(self, username: str) -> None:
        self.login_frame.pack_forget()
        self.store_frame.username_var.set(f"Logged in as: {username}")
        self.store_frame.pack(fill="both", expand=True)
        if not self.store_frame.details_holder.winfo_manager():
            self.store_frame.details_holder.pack(fill="x", padx=0, pady=0)
        if not self.store_frame.details_frame.winfo_manager():
            self.store_frame.details_frame.pack(fill="x", padx=6, pady=6)
        self.store_frame.refresh_games()

    def logout(self) -> None:
        self.store_frame.pack_forget()
        self.store_frame.hide_room_controls()
        self.store_frame.leave_all_rooms()
        self.store_frame.player_lobbies.clear()
        self.store_frame.dev_lobbies.clear()
        self.store_frame.player_active = False
        self.store_frame.dev_mode.set(False)
        self.store_frame.hide_details()
        self.store_frame.show_store_area()
        self.login_frame.pack(fill="both", expand=True)
        try:
            self.conn.request({"type": "auth", "action": "logout", "data": {}})
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn.username = None
        self.restart = True
        self.destroy()

    def run_async(self, work, done) -> None:
        def runner():
            result = None
            err = None
            try:
                result = work()
            except Exception as exc:
                err = exc
            self.after(0, lambda: done(result, err))

        threading.Thread(target=runner, daemon=True).start()

    def on_close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self.restart = False
        self.destroy()

def main() -> None:
    parser = argparse.ArgumentParser(description="Tkinter client for game store")
    parser.add_argument("--host", default=CENTRAL_HOST)
    parser.add_argument("--port", type=int, default=CENTRAL_PORT)
    args = parser.parse_args()
    while True:
        app = GameStoreApp(args.host, args.port)
        app.mainloop()
        if not getattr(app, "restart", False):
            break

if __name__ == "__main__":
    main()
