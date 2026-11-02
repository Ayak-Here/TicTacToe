"""Tic Tac Toe web app: game API, accounts, email verification, and SQLite storage."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
import uuid
from collections import defaultdict, deque
from email.message import EmailMessage
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

ROOT = Path(__file__).parent
WEB = ROOT / "web"
DATABASE = ROOT / "tictactoe.sqlite3"


def load_environment_file(path: Path) -> None:
    """Load simple KEY=VALUE settings without replacing real environment values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_environment_file(ROOT / ".env")
APP_SECRET = os.environ.get("APP_SECRET", "local-development-only-change-me").encode()
SESSION_COOKIE = "three_row_session"
SESSION_SECONDS = 60 * 60 * 24 * 30
GAMES: dict[str, dict] = {}
GAME_LOCK = Lock()
LIMIT_LOCK = Lock()
REQUESTS: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_db() -> None:
    with connect_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                verified_at INTEGER,
                created_at INTEGER NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                draws INTEGER NOT NULL DEFAULT 0,
                streak INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS codes (
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(email, purpose)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                variant TEXT NOT NULL,
                mode TEXT NOT NULL,
                result TEXT NOT NULL,
                played_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS matches_user_date ON matches(user_id, played_at DESC);
        """)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = stored.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def code_digest(email: str, purpose: str, code: str) -> str:
    return hmac.new(APP_SECRET, f"{email}:{purpose}:{code}".encode(), hashlib.sha256).hexdigest()


def send_code(email: str, purpose: str, code: str) -> None:
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_FROM")
    if not host or not sender:
        if os.environ.get("APP_ENV") == "production":
            raise RuntimeError("Email delivery is not configured")
        print(f"[development only] {purpose} code for {email}: {code}")
        return

    message = EmailMessage()
    message["Subject"] = "Your Three in a Row verification code"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Your code is {code}. It expires in 10 minutes.\n\n"
        "If you did not request this code, you can ignore this email."
    )
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls(context=context)
            if username:
                smtp.login(username, password)
            smtp.send_message(message)


def rate_limited(bucket: str, address: str, maximum: int, window: int) -> bool:
    now = time.time()
    key = (bucket, address)
    with LIMIT_LOCK:
        attempts = REQUESTS[key]
        while attempts and attempts[0] <= now - window:
            attempts.popleft()
        if len(attempts) >= maximum:
            return True
        attempts.append(now)
        return False


def winner_for(board: list[str | None], size: int, target: int, misere: bool = False) -> tuple[str | None, list[int]]:
    directions = ((1, 0), (0, 1), (1, 1), (1, -1))
    for index, mark in enumerate(board):
        if not mark:
            continue
        row, col = divmod(index, size)
        for dr, dc in directions:
            cells = []
            for step in range(target):
                r, c = row + step * dr, col + step * dc
                if not (0 <= r < size and 0 <= c < size) or board[r * size + c] != mark:
                    break
                cells.append(r * size + c)
            if len(cells) == target:
                return ("O" if mark == "X" else "X", cells) if misere else (mark, cells)
    if all(board):
        return "draw", []
    return None, []


def best_move(board: list[str | None], mark: str, variant: str, size: int, target: int) -> int:
    if variant == "four":
        available = [i for i, value in enumerate(board) if value is None]
        for candidate in available:
            board[candidate] = mark
            if winner_for(board, size, target)[0] == mark:
                board[candidate] = None
                return candidate
            board[candidate] = None
        opponent = "X" if mark == "O" else "O"
        for candidate in available:
            board[candidate] = opponent
            blocks = winner_for(board, size, target)[0] == opponent
            board[candidate] = None
            if blocks:
                return candidate
        return random.choice(available)

    opponent = "O" if mark == "X" else "X"

    def solve(turn: str, depth: int = 0) -> tuple[int, int | None]:
        winner, _ = winner_for(board, size, target, variant == "misere")
        if winner == mark:
            return 10 - depth, None
        if winner == opponent:
            return depth - 10, None
        if winner == "draw":
            return 0, None
        options = []
        for i, value in enumerate(board):
            if value is None:
                board[i] = turn
                score, _ = solve(opponent if turn == mark else mark, depth + 1)
                board[i] = None
                options.append((score, i))
        return max(options) if turn == mark else min(options)

    return solve(mark)[1]  # type: ignore[return-value]


def public_game(game: dict) -> dict:
    return {key: game[key] for key in ("id", "board", "turn", "mode", "variant", "size", "target", "status", "winner", "winningLine", "aiMark", "playerX", "playerO")}


def player_name(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    name = "".join(character for character in value.strip() if character.isprintable())[:16]
    return name or fallback


class Handler(BaseHTTPRequestHandler):
    server_version = "ThreeInARow"
    sys_version = ""

    def send_json(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid content length") from exc
        if length < 0 or length > 8192:
            raise ValueError("Request is too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def session_user(self) -> sqlite3.Row | None:
        cookie = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        digest = hashlib.sha256(morsel.value.encode()).hexdigest()
        with connect_db() as db:
            return db.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id "
                "WHERE sessions.token_hash=? AND sessions.expires_at>? AND users.verified_at IS NOT NULL",
                (digest, int(time.time())),
            ).fetchone()

    def session_cookie(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires = int(time.time()) + SESSION_SECONDS
        with connect_db() as db:
            db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (digest, user_id, expires))
            db.execute("DELETE FROM sessions WHERE expires_at<?", (int(time.time()),))
        secure = "; Secure" if os.environ.get("APP_ENV") == "production" else ""
        return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}{secure}"

    def clear_cookie(self) -> str:
        return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"

    def get(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/api/me":
            self.me()
            return
        if path.startswith("/api/games/"):
            game_id = path.rsplit("/", 1)[-1]
            user = self.session_user()
            with GAME_LOCK:
                game = GAMES.get(game_id)
                if game and (not game["owner"] or user and game["owner"] == user["id"]):
                    self.send_json(200, public_game(game))
                    return
            self.send_json(404, {"error": "Game not found"})
            return
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB / relative).resolve()
        if not target.is_relative_to(WEB.resolve()) or not target.is_file():
            self.send_error(404)
            return
        types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def me(self) -> None:
        user = self.session_user()
        if not user:
            self.send_json(200, {"user": None})
            return
        with connect_db() as db:
            history = db.execute(
                "SELECT variant,mode,result,played_at FROM matches WHERE user_id=? ORDER BY played_at DESC LIMIT 10",
                (user["id"],),
            ).fetchall()
        self.send_json(200, {
            "user": {"email": user["email"], "wins": user["wins"], "losses": user["losses"], "draws": user["draws"], "streak": user["streak"], "bestStreak": user["best_streak"]},
            "history": [dict(row) for row in history],
        })

    def auth_rate_limit(self, route: str, maximum: int, window: int) -> bool:
        if rate_limited(route, self.client_address[0], maximum, window):
            self.send_json(429, {"error": "Too many attempts. Please wait a little and try again."}, {"Retry-After": str(window)})
            return True
        return False

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def post(self) -> None:
        path = urlparse(self.path).path
        if not self.same_origin():
            self.send_json(403, {"error": "Request origin is not allowed"})
            return
        data = self.read_json()

        if path == "/api/auth/register":
            if self.auth_rate_limit(path, 5, 3600): return
            email = str(data.get("email", "")).strip().lower()
            if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                self.send_json(400, {"error": "Enter a valid email address."}); return
            with connect_db() as db:
                existing = db.execute("SELECT id,verified_at FROM users WHERE email=?", (email,)).fetchone()
                if existing and existing["verified_at"]:
                    self.send_json(409, {"error": "An account already uses that email. Try signing in."}); return
                user_id = existing["id"] if existing else uuid.uuid4().hex
                pending_password = f"pending${secrets.token_urlsafe(32)}"
                db.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?) ON CONFLICT(email) DO UPDATE SET password_hash=excluded.password_hash", (user_id, email, pending_password, int(time.time())))
                self.create_code(db, email, "verify")
            self.send_json(200, {"ok": True, "verificationRequired": True, "email": email})
            return

        if path == "/api/auth/verify":
            if self.auth_rate_limit(path, 10, 600): return
            email = str(data.get("email", "")).strip().lower()
            code = str(data.get("code", ""))
            with connect_db() as db:
                if not self.consume_code(db, email, "verify", code):
                    self.send_json(400, {"error": "That code is invalid or has expired."}); return
                user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
                if not user:
                    self.send_json(400, {"error": "That code is invalid or has expired."}); return
                setup_token = self.issue_auth_token(db, email, "register")
            self.send_json(200, {"ok": True, "setupToken": setup_token})
            return

        if path == "/api/auth/register/complete":
            if self.auth_rate_limit(path, 10, 600): return
            password = data.get("password", "")
            confirmation = data.get("confirmPassword", "")
            if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
                self.send_json(400, {"error": "Use a password between 10 and 128 characters."}); return
            if not isinstance(confirmation, str) or not hmac.compare_digest(password, confirmation):
                self.send_json(400, {"error": "The passwords do not match."}); return
            with connect_db() as db:
                email = self.consume_auth_token(db, str(data.get("setupToken", "")), "register")
                if not email:
                    self.send_json(400, {"error": "Your verification session expired. Please start again."}); return
                user = db.execute("SELECT id FROM users WHERE email=? AND verified_at IS NULL", (email,)).fetchone()
                if not user:
                    self.send_json(400, {"error": "That registration could not be completed."}); return
                db.execute("UPDATE users SET password_hash=?,verified_at=? WHERE id=?", (hash_password(password), int(time.time()), user["id"]))
            self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie(user["id"])})
            return

        if path == "/api/auth/resend":
            if self.auth_rate_limit(path, 3, 600): return
            email = str(data.get("email", "")).strip().lower()
            with connect_db() as db:
                user = db.execute("SELECT verified_at FROM users WHERE email=?", (email,)).fetchone()
                if user and not user["verified_at"]:
                    self.create_code(db, email, "verify")
            self.send_json(200, {"ok": True, "message": "If that account is awaiting verification, a new code was sent."})
            return

        if path == "/api/auth/login":
            if self.auth_rate_limit(path, 10, 900): return
            email = str(data.get("email", "")).strip().lower()
            password = data.get("password", "")
            with connect_db() as db:
                user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not user or not isinstance(password, str) or not verify_password(password, user["password_hash"]):
                self.send_json(401, {"error": "Email or password is incorrect."}); return
            if not user["verified_at"]:
                self.send_json(403, {"error": "Verify your email before signing in.", "verificationRequired": True, "email": email}); return
            self.send_json(200, {"ok": True}, {"Set-Cookie": self.session_cookie(user["id"])})
            return

        if path == "/api/auth/forgot":
            if self.auth_rate_limit(path, 5, 3600): return
            email = str(data.get("email", "")).strip().lower()
            with connect_db() as db:
                user = db.execute("SELECT id,verified_at FROM users WHERE email=?", (email,)).fetchone()
                if user and user["verified_at"]:
                    self.create_code(db, email, "reset")
            self.send_json(200, {"ok": True, "message": "If a verified account uses that email, a reset code was sent."})
            return

        if path == "/api/auth/reset/verify":
            if self.auth_rate_limit(path, 10, 600): return
            email = str(data.get("email", "")).strip().lower()
            code = str(data.get("code", ""))
            with connect_db() as db:
                if not self.consume_code(db, email, "reset", code):
                    self.send_json(400, {"error": "That code is invalid or has expired."}); return
                user = db.execute("SELECT id FROM users WHERE email=? AND verified_at IS NOT NULL", (email,)).fetchone()
                if not user:
                    self.send_json(400, {"error": "That code is invalid or has expired."}); return
                reset_token = self.issue_auth_token(db, email, "reset")
            self.send_json(200, {"ok": True, "resetToken": reset_token})
            return

        if path == "/api/auth/reset":
            if self.auth_rate_limit(path, 10, 600): return
            password = data.get("password", "")
            confirmation = data.get("confirmPassword", "")
            if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
                self.send_json(400, {"error": "Use a password between 10 and 128 characters."}); return
            if not isinstance(confirmation, str) or not hmac.compare_digest(password, confirmation):
                self.send_json(400, {"error": "The passwords do not match."}); return
            with connect_db() as db:
                email = self.consume_auth_token(db, str(data.get("resetToken", "")), "reset")
                if not email:
                    self.send_json(400, {"error": "Your reset session expired. Request a new code."}); return
                user = db.execute("SELECT id FROM users WHERE email=? AND verified_at IS NOT NULL", (email,)).fetchone()
                if not user:
                    self.send_json(400, {"error": "That code is invalid or has expired."}); return
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user["id"]))
                db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            self.send_json(200, {"ok": True})
            return

        if path == "/api/auth/logout":
            token = cookies.SimpleCookie(self.headers.get("Cookie", "")).get(SESSION_COOKIE)
            if token:
                digest = hashlib.sha256(token.value.encode()).hexdigest()
                with connect_db() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
            self.send_json(200, {"ok": True}, {"Set-Cookie": self.clear_cookie()})
            return

        if path == "/api/games":
            if rate_limited("new-game", self.client_address[0], 30, 60):
                self.send_json(429, {"error": "Too many new games. Please wait a moment."}); return
            mode = data.get("mode", "cpu")
            variant = data.get("variant", "classic")
            if mode not in ("cpu", "local") or variant not in ("classic", "misere", "four"):
                self.send_json(400, {"error": "Choose a supported game mode and variant."}); return
            size, target = (4, 4) if variant == "four" else (3, 3)
            player_x = player_name(data.get("playerX"), "Player 1") if mode == "local" else "YOU"
            player_o = player_name(data.get("playerO"), "Player 2") if mode == "local" else "COMPUTER"
            game = {"id": uuid.uuid4().hex, "board": [None] * (size * size), "turn": "X", "mode": mode, "variant": variant, "size": size, "target": target, "status": "playing", "winner": None, "winningLine": [], "aiMark": "O" if mode == "cpu" else None, "playerX": player_x, "playerO": player_o, "owner": (user["id"] if (user := self.session_user()) else None), "saved": False}
            with GAME_LOCK:
                GAMES[game["id"]] = game
            self.send_json(201, public_game(game)); return

        if path.startswith("/api/games/") and path.endswith("/move"):
            if rate_limited("move", self.client_address[0], 180, 60):
                self.send_json(429, {"error": "Too many moves. Please wait a moment."}); return
            segments = path.split("/")
            if len(segments) != 5 or segments[2] != "games":
                self.send_json(404, {"error": "Endpoint not found"}); return
            game_id = segments[3]
            try: cell = int(data.get("cell", -1))
            except (TypeError, ValueError): cell = -1
            user = self.session_user()
            with GAME_LOCK:
                game = GAMES.get(game_id)
                if not game or game["owner"] and (not user or game["owner"] != user["id"]):
                    self.send_json(404, {"error": "Game not found"}); return
                if not 0 <= cell < len(game["board"]):
                    self.send_json(400, {"error": "Choose a valid cell."}); return
                if game["status"] != "playing" or game["board"][cell] is not None:
                    self.send_json(409, {"error": "That move is no longer available."}); return
                game["board"][cell] = game["turn"]
                self.advance_game(game)
                if game["status"] == "finished" and game["owner"] and not game["saved"]:
                    self.save_result(game)
                self.send_json(200, public_game(game)); return

        self.send_json(404, {"error": "Endpoint not found"})

    def create_code(self, db: sqlite3.Connection, email: str, purpose: str) -> None:
        code = f"{secrets.randbelow(1_000_000):06d}"
        db.execute("INSERT INTO codes(email,purpose,code_hash,expires_at,attempts) VALUES(?,?,?,?,0) ON CONFLICT(email,purpose) DO UPDATE SET code_hash=excluded.code_hash,expires_at=excluded.expires_at,attempts=0", (email, purpose, code_digest(email, purpose, code), int(time.time()) + 600))
        send_code(email, purpose, code)

    def consume_code(self, db: sqlite3.Connection, email: str, purpose: str, code: str) -> bool:
        row = db.execute("SELECT * FROM codes WHERE email=? AND purpose=?", (email, purpose)).fetchone()
        if not row or row["expires_at"] < time.time() or row["attempts"] >= 5:
            return False
        digest = code_digest(email, purpose, code)
        if not hmac.compare_digest(digest, row["code_hash"]):
            db.execute("UPDATE codes SET attempts=attempts+1 WHERE email=? AND purpose=?", (email, purpose))
            return False
        db.execute("DELETE FROM codes WHERE email=? AND purpose=?", (email, purpose))
        return True

    def issue_auth_token(self, db: sqlite3.Connection, email: str, purpose: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires_at = int(time.time()) + 900
        db.execute("DELETE FROM auth_tokens WHERE expires_at<?", (int(time.time()),))
        db.execute("INSERT INTO auth_tokens(token_hash,email,purpose,expires_at) VALUES(?,?,?,?)", (digest, email, purpose, expires_at))
        return token

    def consume_auth_token(self, db: sqlite3.Connection, token: str, purpose: str) -> str | None:
        if not token or len(token) > 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = db.execute("SELECT email FROM auth_tokens WHERE token_hash=? AND purpose=? AND expires_at>?", (digest, purpose, int(time.time()))).fetchone()
        if not row:
            return None
        db.execute("DELETE FROM auth_tokens WHERE token_hash=?", (digest,))
        return row["email"]

    def advance_game(self, game: dict) -> None:
        winner, line = winner_for(game["board"], game["size"], game["target"], game["variant"] == "misere")
        if winner:
            game.update(status="finished", winner=winner, winningLine=line)
            return
        game["turn"] = "O" if game["turn"] == "X" else "X"
        if game["mode"] == "cpu" and game["turn"] == game["aiMark"]:
            ai_cell = best_move(game["board"], game["aiMark"], game["variant"], game["size"], game["target"])
            game["board"][ai_cell] = game["aiMark"]
            winner, line = winner_for(game["board"], game["size"], game["target"], game["variant"] == "misere")
            if winner:
                game.update(status="finished", winner=winner, winningLine=line)
            else:
                game["turn"] = "X"

    def save_result(self, game: dict) -> None:
        user_result = "draw" if game["winner"] == "draw" else "win" if game["mode"] == "local" or game["winner"] == "X" else "loss"
        with connect_db() as db:
            db.execute("INSERT INTO matches(user_id,variant,mode,result,played_at) VALUES(?,?,?,?,?)", (game["owner"], game["variant"], game["mode"], user_result, int(time.time())))
            if user_result == "win":
                db.execute("UPDATE users SET wins=wins+1,streak=streak+1,best_streak=MAX(best_streak,streak+1) WHERE id=?", (game["owner"],))
            elif user_result == "loss":
                db.execute("UPDATE users SET losses=losses+1,streak=0 WHERE id=?", (game["owner"],))
            elif user_result == "draw":
                db.execute("UPDATE users SET draws=draws+1 WHERE id=?", (game["owner"],))
        game["saved"] = True

    def do_GET(self) -> None:
        try:
            self.get()
        except (sqlite3.Error, OSError) as error:
            print(f"Request error: {error}")
            self.send_json(500, {"error": "The server could not complete that request."})

    def do_POST(self) -> None:
        try:
            self.post()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error) or "Invalid request."})
        except (sqlite3.Error, OSError, RuntimeError) as error:
            print(f"Request error: {error}")
            self.send_json(500, {"error": "The server could not complete that request."})

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


if __name__ == "__main__":
    if os.environ.get("APP_ENV") == "production" and (APP_SECRET == b"local-development-only-change-me" or not os.environ.get("SMTP_HOST") or not os.environ.get("SMTP_FROM")):
        raise SystemExit("Set a strong APP_SECRET and SMTP_HOST/SMTP_FROM before production startup.")
    initialize_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Three in a Row is ready at http://localhost:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
