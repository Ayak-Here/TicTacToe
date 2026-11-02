"""Vercel FastAPI app with durable PostgreSQL accounts, games, and rate limits."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row

from game_logic import APP_SECRET, SESSION_COOKIE, SESSION_SECONDS, best_move, code_digest, hash_password, player_name, send_code, verify_password, winner_for

ROOT = Path(__file__).parent
WEB = ROOT / "web"


def connection() -> psycopg.Connection:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing. Connect Neon and add its pooled connection string in Vercel.")
    return psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10)


def initialize_database() -> None:
    schema = """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            verified_at BIGINT,
            created_at BIGINT NOT NULL,
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
            expires_at BIGINT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(email, purpose)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matches (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            variant TEXT NOT NULL,
            mode TEXT NOT NULL,
            result TEXT NOT NULL,
            played_at BIGINT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS matches_user_date ON matches(user_id, played_at DESC);
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            owner_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            board JSONB NOT NULL,
            turn TEXT NOT NULL,
            mode TEXT NOT NULL,
            variant TEXT NOT NULL,
            size INTEGER NOT NULL,
            target INTEGER NOT NULL,
            status TEXT NOT NULL,
            winner TEXT,
            winning_line JSONB NOT NULL DEFAULT '[]'::jsonb,
            ai_mark TEXT,
            player_x TEXT NOT NULL,
            player_o TEXT NOT NULL,
            saved BOOLEAN NOT NULL DEFAULT FALSE,
            created_at BIGINT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS games_created_at ON games(created_at);
        CREATE TABLE IF NOT EXISTS rate_limits (
            scope TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            window_start BIGINT NOT NULL,
            hits INTEGER NOT NULL,
            PRIMARY KEY(scope, key_hash)
        );
    """
    with connection() as db:
        for statement in schema.split(";"):
            if statement.strip():
                db.execute(statement)


def public_game(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "board": row["board"],
        "turn": row["turn"],
        "mode": row["mode"],
        "variant": row["variant"],
        "size": row["size"],
        "target": row["target"],
        "status": row["status"],
        "winner": row["winner"],
        "winningLine": row["winning_line"],
        "aiMark": row["ai_mark"],
        "playerX": row["player_x"],
        "playerO": row["player_o"],
    }


def rate_limit(db: psycopg.Connection, request: Request, scope: str, maximum: int, seconds: int) -> None:
    now = int(time.time())
    window_start = now - now % seconds
    raw_ip = request.client.host if request.client else "unknown"
    key_hash = hmac.new(APP_SECRET, raw_ip.encode(), hashlib.sha256).hexdigest()
    row = db.execute(
        """INSERT INTO rate_limits(scope,key_hash,window_start,hits) VALUES(%s,%s,%s,1)
           ON CONFLICT(scope,key_hash) DO UPDATE SET
           hits=CASE WHEN rate_limits.window_start=EXCLUDED.window_start THEN rate_limits.hits+1 ELSE 1 END,
           window_start=EXCLUDED.window_start
           RETURNING hits""",
        (scope, key_hash, window_start),
    ).fetchone()
    db.execute("DELETE FROM rate_limits WHERE window_start < %s", (now - max(seconds * 4, 3600),))
    if row["hits"] > maximum:
        raise HTTPException(429, "Too many attempts. Please wait a little and try again.", headers={"Retry-After": str(seconds)})


def issue_code(db: psycopg.Connection, email: str, purpose: str) -> None:
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.execute(
        """INSERT INTO codes(email,purpose,code_hash,expires_at,attempts) VALUES(%s,%s,%s,%s,0)
           ON CONFLICT(email,purpose) DO UPDATE SET code_hash=EXCLUDED.code_hash,expires_at=EXCLUDED.expires_at,attempts=0""",
        (email, purpose, code_digest(email, purpose, code), int(time.time()) + 600),
    )
    send_code(email, purpose, code)


def consume_code(db: psycopg.Connection, email: str, purpose: str, code: str) -> bool:
    row = db.execute("SELECT * FROM codes WHERE email=%s AND purpose=%s", (email, purpose)).fetchone()
    if not row or row["expires_at"] < time.time() or row["attempts"] >= 5:
        return False
    expected = code_digest(email, purpose, code)
    if not hmac.compare_digest(expected, row["code_hash"]):
        db.execute("UPDATE codes SET attempts=attempts+1 WHERE email=%s AND purpose=%s", (email, purpose))
        return False
    db.execute("DELETE FROM codes WHERE email=%s AND purpose=%s", (email, purpose))
    return True


def issue_auth_token(db: psycopg.Connection, email: str, purpose: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    db.execute("DELETE FROM auth_tokens WHERE expires_at<%s", (now,))
    db.execute("INSERT INTO auth_tokens(token_hash,email,purpose,expires_at) VALUES(%s,%s,%s,%s)", (token_hash, email, purpose, now + 900))
    return token


def consume_auth_token(db: psycopg.Connection, token: str, purpose: str) -> str | None:
    if not token or len(token) > 128:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.execute("SELECT email FROM auth_tokens WHERE token_hash=%s AND purpose=%s AND expires_at>%s", (token_hash, purpose, int(time.time()))).fetchone()
    if not row:
        return None
    db.execute("DELETE FROM auth_tokens WHERE token_hash=%s", (token_hash,))
    return row["email"]


def session_user(request: Request, db: psycopg.Connection) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return db.execute(
        """SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id
           WHERE sessions.token_hash=%s AND sessions.expires_at>%s AND users.verified_at IS NOT NULL""",
        (token_hash, int(time.time())),
    ).fetchone()


def create_session(db: psycopg.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = int(time.time()) + SESSION_SECONDS
    db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(%s,%s,%s)", (token_hash, user_id, expires_at))
    db.execute("DELETE FROM sessions WHERE expires_at<%s", (int(time.time()),))
    secure = os.environ.get("APP_ENV") == "production"
    cookie = f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
    return cookie + ("; Secure" if secure else "")


def valid_email(email: str) -> bool:
    return len(email) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None


def checked_password(password: Any, confirmation: Any) -> None:
    if not isinstance(password, str) or not 10 <= len(password) <= 128:
        raise HTTPException(400, "Use a password between 10 and 128 characters.")
    if not isinstance(confirmation, str) or not hmac.compare_digest(password, confirmation):
        raise HTTPException(400, "The passwords do not match.")


app = FastAPI(title="Three in a Row API", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and origin:
        if urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "Request origin is not allowed."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    return response


@app.exception_handler(HTTPException)
def http_error_response(_: Request, exc: HTTPException):
    payload: dict[str, Any] = {"error": str(exc.detail)}
    if exc.headers and exc.headers.get("X-Verification-Required") == "true":
        payload["verificationRequired"] = True
        payload["email"] = exc.headers.get("X-Account-Email", "")
    return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    with connection() as db:
        user = session_user(request, db)
        if not user:
            return {"user": None}
        matches = db.execute("SELECT variant,mode,result,played_at FROM matches WHERE user_id=%s ORDER BY played_at DESC LIMIT 10", (user["id"],)).fetchall()
        return {
            "user": {"email": user["email"], "wins": user["wins"], "losses": user["losses"], "draws": user["draws"], "streak": user["streak"], "bestStreak": user["best_streak"]},
            "history": matches,
        }


@app.post("/api/auth/register")
def register(data: dict[str, Any], request: Request):
    email = str(data.get("email", "")).strip().lower()
    if not valid_email(email):
        raise HTTPException(400, "Enter a valid email address.")
    with connection() as db:
        rate_limit(db, request, "register", 5, 3600)
        old = db.execute("SELECT id,verified_at FROM users WHERE email=%s", (email,)).fetchone()
        if old and old["verified_at"]:
            raise HTTPException(409, "An account already uses that email. Try signing in.")
        user_id = old["id"] if old else secrets.token_hex(16)
        pending_hash = f"pending${secrets.token_urlsafe(32)}"
        db.execute(
            """INSERT INTO users(id,email,password_hash,created_at) VALUES(%s,%s,%s,%s)
               ON CONFLICT(email) DO UPDATE SET password_hash=EXCLUDED.password_hash""",
            (user_id, email, pending_hash, int(time.time())),
        )
        issue_code(db, email, "verify")
    return {"ok": True, "verificationRequired": True, "email": email}


@app.post("/api/auth/verify")
def verify_registration(data: dict[str, Any], request: Request):
    email = str(data.get("email", "")).strip().lower()
    with connection() as db:
        rate_limit(db, request, "verify", 10, 600)
        if not consume_code(db, email, "verify", str(data.get("code", ""))):
            raise HTTPException(400, "That code is invalid or has expired.")
        pending = db.execute("SELECT id FROM users WHERE email=%s AND verified_at IS NULL", (email,)).fetchone()
        if not pending:
            raise HTTPException(400, "That code is invalid or has expired.")
        token = issue_auth_token(db, email, "register")
    return {"ok": True, "setupToken": token}


@app.post("/api/auth/register/complete")
def complete_registration(data: dict[str, Any], request: Request):
    checked_password(data.get("password"), data.get("confirmPassword"))
    with connection() as db:
        rate_limit(db, request, "register-complete", 10, 600)
        email = consume_auth_token(db, str(data.get("setupToken", "")), "register")
        if not email:
            raise HTTPException(400, "Your verification session expired. Please start again.")
        user = db.execute("SELECT id FROM users WHERE email=%s AND verified_at IS NULL", (email,)).fetchone()
        if not user:
            raise HTTPException(400, "That registration could not be completed.")
        db.execute("UPDATE users SET password_hash=%s,verified_at=%s WHERE id=%s", (hash_password(data["password"]), int(time.time()), user["id"]))
        cookie = create_session(db, user["id"])
    response = JSONResponse({"ok": True})
    response.headers.append("Set-Cookie", cookie)
    return response


@app.post("/api/auth/resend")
def resend_registration(data: dict[str, Any], request: Request):
    email = str(data.get("email", "")).strip().lower()
    with connection() as db:
        rate_limit(db, request, "resend", 3, 600)
        user = db.execute("SELECT verified_at FROM users WHERE email=%s", (email,)).fetchone()
        if user and not user["verified_at"]:
            issue_code(db, email, "verify")
    return {"ok": True, "message": "If that account is awaiting verification, a new code was sent."}


@app.post("/api/auth/login")
def login(data: dict[str, Any], request: Request):
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    with connection() as db:
        rate_limit(db, request, "login", 10, 900)
        user = db.execute("SELECT * FROM users WHERE email=%s", (email,)).fetchone()
        if not user or not isinstance(password, str) or not verify_password(password, user["password_hash"]):
            raise HTTPException(401, "Email or password is incorrect.")
        if not user["verified_at"]:
            raise HTTPException(403, "Verify your email before signing in.", headers={"X-Verification-Required": "true", "X-Account-Email": email})
        cookie = create_session(db, user["id"])
    response = JSONResponse({"ok": True})
    response.headers.append("Set-Cookie", cookie)
    return response


@app.post("/api/auth/forgot")
def forgot(data: dict[str, Any], request: Request):
    email = str(data.get("email", "")).strip().lower()
    with connection() as db:
        rate_limit(db, request, "forgot", 5, 3600)
        user = db.execute("SELECT verified_at FROM users WHERE email=%s", (email,)).fetchone()
        if user and user["verified_at"]:
            issue_code(db, email, "reset")
    return {"ok": True, "message": "If a verified account uses that email, a reset code was sent."}


@app.post("/api/auth/reset/verify")
def verify_reset(data: dict[str, Any], request: Request):
    email = str(data.get("email", "")).strip().lower()
    with connection() as db:
        rate_limit(db, request, "reset-verify", 10, 600)
        if not consume_code(db, email, "reset", str(data.get("code", ""))):
            raise HTTPException(400, "That code is invalid or has expired.")
        user = db.execute("SELECT id FROM users WHERE email=%s AND verified_at IS NOT NULL", (email,)).fetchone()
        if not user:
            raise HTTPException(400, "That code is invalid or has expired.")
        token = issue_auth_token(db, email, "reset")
    return {"ok": True, "resetToken": token}


@app.post("/api/auth/reset")
def reset_password(data: dict[str, Any], request: Request):
    checked_password(data.get("password"), data.get("confirmPassword"))
    with connection() as db:
        rate_limit(db, request, "reset", 10, 600)
        email = consume_auth_token(db, str(data.get("resetToken", "")), "reset")
        if not email:
            raise HTTPException(400, "Your reset session expired. Request a new code.")
        user = db.execute("SELECT id FROM users WHERE email=%s AND verified_at IS NOT NULL", (email,)).fetchone()
        if not user:
            raise HTTPException(400, "That code is invalid or has expired.")
        db.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(data["password"]), user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=%s", (user["id"],))
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash,))
    response = JSONResponse({"ok": True})
    response.headers.append("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
    return response


@app.post("/api/games")
def create_game(data: dict[str, Any], request: Request):
    mode = data.get("mode", "cpu")
    variant = data.get("variant", "classic")
    if mode not in ("cpu", "local") or variant not in ("classic", "misere", "four"):
        raise HTTPException(400, "Choose a supported game mode and variant.")
    size, target = (4, 4) if variant == "four" else (3, 3)
    game = {
        "id": secrets.token_hex(16), "board": [None] * (size * size), "turn": "X", "mode": mode,
        "variant": variant, "size": size, "target": target, "status": "playing", "winner": None,
        "winning_line": [], "ai_mark": "O" if mode == "cpu" else None,
        "player_x": player_name(data.get("playerX"), "Player 1") if mode == "local" else "YOU",
        "player_o": player_name(data.get("playerO"), "Player 2") if mode == "local" else "COMPUTER",
        "saved": False, "created_at": int(time.time()),
    }
    with connection() as db:
        rate_limit(db, request, "new-game", 30, 60)
        user = session_user(request, db)
        game["owner_id"] = user["id"] if user else None
        db.execute(
            """INSERT INTO games(id,owner_id,board,turn,mode,variant,size,target,status,winner,winning_line,ai_mark,player_x,player_o,created_at)
               VALUES(%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)""",
            (game["id"], game["owner_id"], json.dumps(game["board"]), game["turn"], mode, variant, size, target,
             game["status"], game["winner"], json.dumps(game["winning_line"]), game["ai_mark"], game["player_x"], game["player_o"], game["created_at"]),
        )
    game["winning_line"] = []
    return JSONResponse(public_game(game), status_code=201)


def advance_game(game: dict[str, Any]) -> None:
    winner, line = winner_for(game["board"], game["size"], game["target"], game["variant"] == "misere")
    if winner:
        game.update(status="finished", winner=winner, winning_line=line)
        return
    game["turn"] = "O" if game["turn"] == "X" else "X"
    if game["mode"] == "cpu" and game["turn"] == game["ai_mark"]:
        cell = best_move(game["board"], game["ai_mark"], game["variant"], game["size"], game["target"])
        game["board"][cell] = game["ai_mark"]
        winner, line = winner_for(game["board"], game["size"], game["target"], game["variant"] == "misere")
        if winner:
            game.update(status="finished", winner=winner, winning_line=line)
        else:
            game["turn"] = "X"


def save_result(db: psycopg.Connection, game: dict[str, Any]) -> None:
    result = "draw" if game["winner"] == "draw" else "win" if game["mode"] == "local" or game["winner"] == "X" else "loss"
    db.execute("INSERT INTO matches(user_id,variant,mode,result,played_at) VALUES(%s,%s,%s,%s,%s)", (game["owner_id"], game["variant"], game["mode"], result, int(time.time())))
    if result == "win":
        db.execute("UPDATE users SET wins=wins+1,streak=streak+1,best_streak=GREATEST(best_streak,streak+1) WHERE id=%s", (game["owner_id"],))
    elif result == "loss":
        db.execute("UPDATE users SET losses=losses+1,streak=0 WHERE id=%s", (game["owner_id"],))
    else:
        db.execute("UPDATE users SET draws=draws+1 WHERE id=%s", (game["owner_id"],))


@app.post("/api/games/{game_id}/move")
def move(game_id: str, data: dict[str, Any], request: Request):
    try:
        cell = int(data.get("cell", -1))
    except (TypeError, ValueError):
        cell = -1
    with connection() as db:
        rate_limit(db, request, "move", 180, 60)
        user = session_user(request, db)
        row = db.execute("SELECT * FROM games WHERE id=%s FOR UPDATE", (game_id,)).fetchone()
        if not row or (row["owner_id"] and (not user or row["owner_id"] != user["id"])):
            raise HTTPException(404, "Game not found.")
        game = {
            **row,
            "board": row["board"],
            "winning_line": row["winning_line"],
        }
        if not 0 <= cell < len(game["board"]):
            raise HTTPException(400, "Choose a valid cell.")
        if game["status"] != "playing" or game["board"][cell] is not None:
            raise HTTPException(409, "That move is no longer available.")
        game["board"][cell] = game["turn"]
        advance_game(game)
        if game["status"] == "finished" and game["owner_id"] and not game["saved"]:
            save_result(db, game)
            game["saved"] = True
        db.execute(
            "UPDATE games SET board=%s::jsonb,turn=%s,status=%s,winner=%s,winning_line=%s::jsonb,saved=%s WHERE id=%s",
            (json.dumps(game["board"]), game["turn"], game["status"], game["winner"], json.dumps(game["winning_line"]), game["saved"], game_id),
        )
        return public_game(game)


@app.get("/api/games/{game_id}")
def get_game(game_id: str, request: Request):
    with connection() as db:
        user = session_user(request, db)
        row = db.execute("SELECT * FROM games WHERE id=%s", (game_id,)).fetchone()
        if not row or (row["owner_id"] and (not user or row["owner_id"] != user["id"])):
            raise HTTPException(404, "Game not found.")
        return public_game({**row, "winning_line": row["winning_line"]})


@app.on_event("startup")
def create_schema_on_startup() -> None:
    if os.environ.get("APP_ENV") == "production" and (APP_SECRET == b"local-development-only-change-me" or len(APP_SECRET) < 32 or not os.environ.get("SMTP_HOST") or not os.environ.get("SMTP_FROM")):
        raise RuntimeError("Set APP_SECRET and SMTP_HOST/SMTP_FROM before production startup.")
    initialize_database()


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
