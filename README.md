# Three in a Row

A browser game with guest play, three variants, and optional verified accounts. The backend uses only Python's standard library and SQLite. The original Tkinter project remains in `tictactoe.py`.

## Run locally

Requires Python 3.10 or newer. In PowerShell, from this folder:

```powershell
python server.py
```

Open [http://localhost:8000](http://localhost:8000). Guest play works immediately. SQLite creates `tictactoe.sqlite3` on first start.

## Email verification and local settings

`server.py` reads settings from `.env` at startup. Edit the ignored `.env` file in the project folder and fill in the SMTP values from your mail provider. Use an app password, not your normal mailbox password. **Never commit `.env`; `.gitignore` excludes it.** The safe template is `.env.example`.

```text
SMTP_HOST=smtp.your-provider.com
SMTP_PORT=587
SMTP_FROM=Three in a Row <games@yourdomain.com>
SMTP_USER=your-smtp-username
SMTP_PASSWORD=your-provider-app-password
```

Restart `python server.py` after editing `.env`. Until SMTP is configured, the app prints verification and password reset codes in the server terminal (local development only).

For hosting, enter secrets in the host's environment-variable settings rather than uploading `.env`. Set a unique `APP_SECRET` and `APP_ENV=production`; the app refuses to start in production without SMTP configured.

```powershell
$env:APP_ENV = "production"
$env:APP_SECRET = "a-long-random-secret-from-your-secret-manager"
$env:HOST = "127.0.0.1"
$env:PORT = "8000"
python server.py
```

## Included

- Classic, Misère, and 4×4 Four in a Row; play the computer or a friend.
- Verified registration, sign-in, sign-out, and email-code password recovery.
- Salted scrypt password hashes, expiring one-time codes, hashed session tokens, HttpOnly/SameSite cookies, same-origin checks, and per-IP rate limits.
- Account match history, wins, streaks, and simple rank milestones. Guest records are saved in that browser.
- Responsive layout, keyboard moves (`1`–`9`), win-line animations, and reduced-motion support.

The lightweight development server is intended to run behind an HTTPS reverse proxy for internet hosting. Accounts and match records persist in SQLite; active boards and the in-memory rate limiter reset when the server restarts.

`DATABASE_URL` is included in `.env.example` for the planned PostgreSQL migration. The current code does **not** connect to PostgreSQL yet; local storage still uses the ignored `tictactoe.sqlite3` file.
