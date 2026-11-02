# Three in a Row

A browser game with guest play, three game variations, accounts, and saved match history. The project uses FastAPI and PostgreSQL; the frontend is served from `web/`.

## Accounts

Registration asks for an email address, password, and password confirmation. The app checks that the address has a valid email-like format, hashes passwords with scrypt, and signs the user in after registration. It does not confirm that the mailbox exists or belongs to the user. Existing addresses are prompted to sign in.

Email delivery and password-reset codes are not configured. Users who forget their password cannot currently recover the account, so keep the password safe. Password recovery can be added later with a verified email provider or another recovery method.

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

## Deploy to Vercel

Vercel detects the FastAPI app in `app.py` and installs `requirements.txt`. The site and API deploy together. Connect Neon through the Vercel Marketplace integration or set these variables in **Project → Settings → Environment Variables**:

```text
DATABASE_URL=<Neon pooled connection string>
APP_ENV=production
APP_SECRET=<unique random secret, at least 32 characters>
```

Keep `.env` private and out of GitHub. `APP_SECRET` must be a strong, unique value. Local SQLite and the old Tkinter app are not part of this project.
