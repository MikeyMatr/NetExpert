# NetExpert

FastAPI and PostgreSQL prototype for first-line internet connectivity diagnosis. SQL is written directly with psycopg; the frontend uses plain HTML, CSS, and JavaScript.

## Authentication update

This update adds `staff_users` with `operator`, `expert`, and `admin` roles. Passwords are hashed with Argon2. FastAPI protects ticket and diagnostic endpoints with bearer tokens. Operators see only tickets assigned to them; an administrator can view all tickets and assign old tickets through the API. Experts cannot access ticket endpoints in this version.

The UI keeps its access token only in memory. Reloading the page requires signing in again. Tokens expire after 30 minutes; disabled accounts are refused even if their token has not expired.

## Upgrade from v1 on macOS

1. Stop the running FastAPI process with **Control+C**. Keep the PostgreSQL container running.
2. Extract the update archive into your existing `NetExpert` folder, replacing the included files. The archive does **not** contain `.env` and does not remove existing tickets.
3. Activate the virtual environment and install the added dependencies:

   ```bash
   cd ~/Documents/NetExpert
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

4. Add a new `JWT_SECRET` line to your existing `.env`. Generate its value locally:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Paste the output after `JWT_SECRET=`. Keep your existing `PGPASSWORD` and other database settings. Do not send or commit these secrets.

5. Create an operator account. The command prompts for its password twice, without echoing it:

   ```bash
   python -m app.create_staff your_login --role operator
   ```

   Create further operator accounts with different logins. An administrator account is optional:

   ```bash
   python -m app.create_staff admin_login --role admin
   ```

6. Start the application:

   ```bash
   python -m uvicorn app.main:app --reload
   ```

   Open http://127.0.0.1:8000 and sign in. `/docs` has an **Authorize** button; unauthorized API calls return `401`. The documentation page itself is still visible in local development.

The first post-upgrade connection runs `app/002_staff_auth.sql` inside a transaction and records the migration in `schema_migrations`. New tables are `staff_users` and `schema_migrations`, so the application schema now has 19 tables. Original text audit fields remain, while new records also save foreign keys to verified staff accounts. Existing historical tickets retain `NULL` in those new FK columns; they are visible to the administrator until assigned.

To assign an old ticket, sign into Swagger as an administrator, call `GET /api/staff` to find an operator ID, then call `PATCH /api/tickets/{ticket_id}/assign` with `{"operator_id": 2}` (using the actual ID). Assigned tickets then appear in that operator's list.

This is a local prototype. Use HTTPS before exposing it beyond localhost. Knowledge-base editing and a staff-management interface are future work.
