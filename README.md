# Three in a Row

A browser game with guest play, three game variations, verified accounts, and saved match history. The project uses FastAPI and PostgreSQL; the frontend is served from `web/`.

## Run locally

1. Create a Neon PostgreSQL database and copy its pooled connection string into `DATABASE_URL` in your ignored `.env` file.
2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Start the app:

   ```powershell
   uvicorn app:app --reload
   ```

Open [http://localhost:8000](http://localhost:8000). PostgreSQL creates its tables automatically when the app starts.

For local email testing, leave SMTP blank; verification and password reset codes print in the terminal. To send real emails, fill the SMTP settings in `.env`. Never commit `.env`; `.gitignore` excludes it. `.env.example` is the safe template.

## Deploy to Vercel

Vercel detects the FastAPI app in `app.py` and installs `requirements.txt`. The site and API deploy together. Connect Neon through the Vercel Marketplace integration and set the following in **Project → Settings → Environment Variables**:

```text
DATABASE_URL=<Neon pooled connection string>
APP_ENV=production
APP_SECRET=<unique random secret, at least 32 characters>
SMTP_HOST=smtp.resend.com
SMTP_PORT=465
SMTP_FROM=Three in a Row <no-reply@your-verified-domain.com>
SMTP_USER=resend
SMTP_PASSWORD=<Resend API key>
```

Verify your sending domain with Resend before accepting registrations. Do not upload `.env` or paste secrets into GitHub. Local SQLite and the old Tkinter app are not part of this project.
