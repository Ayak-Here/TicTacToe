"""Shared game, password, and email helpers for the Vercel FastAPI app."""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent


def load_environment_file(path: Path) -> None:
    """Read KEY=VALUE settings for local development without overriding env vars."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_environment_file(ROOT / ".env")
APP_SECRET = os.environ.get("APP_SECRET", "local-development-only-change-me").encode()
SESSION_COOKIE = "three_row_session"
SESSION_SECONDS = 60 * 60 * 24 * 30


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
    message.set_content(f"Your code is {code}. It expires in 10 minutes.\n\nIf you did not request this code, you can ignore this email.")
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
        for index, value in enumerate(board):
            if value is None:
                board[index] = turn
                score, _ = solve(opponent if turn == mark else mark, depth + 1)
                board[index] = None
                options.append((score, index))
        return max(options) if turn == mark else min(options)

    return solve(mark)[1]  # type: ignore[return-value]


def player_name(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    name = "".join(character for character in value.strip() if character.isprintable())[:16]
    return name or fallback
