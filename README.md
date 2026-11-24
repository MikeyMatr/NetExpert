# NetExpert

FastAPI and PostgreSQL prototype for first-line internet connectivity diagnosis. SQL is written directly with psycopg; the frontend uses plain HTML, CSS, and JavaScript.

## Authentication update

This update adds `staff_users` with `operator`, `expert`, and `admin` roles. Passwords are hashed with Argon2. FastAPI protects ticket and diagnostic endpoints with bearer tokens. Operators see only tickets assigned to them; an administrator can view all tickets and assign old tickets through the API. Experts cannot access ticket endpoints in this version.

The UI keeps its access token only in memory. Reloading the page requires signing in again. Tokens expire after 30 minutes; disabled accounts are refused even if their token has not expired.

## Administrator and operators

The administrator is configured locally through `.env`. Copy `.env.example` to `.env` if you do not already have one, and keep your existing PostgreSQL settings. Set `ADMIN_USERNAME` and a unique `ADMIN_PASSWORD` of at least 12 characters. Generate a separate JWT signing secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"` and set `JWT_SECRET` to its output. Do not publish `.env` or reuse previously exposed secrets.

On every application start, the account named by `ADMIN_USERNAME` is created as an administrator if missing. If it already exists as an administrator, its password is synchronized with `ADMIN_PASSWORD` and the account is activated. An existing operator or expert with that username causes startup to fail: choose another username. Restart the application after changing either setting. The administrator can sign in at `/` or directly at `/adminpanel`; a page reload requires another sign-in.

Only an administrator can list, create, disable, restore, or permanently remove operators at `/adminpanel`. Use the **Операторы** button after signing in. New operator passwords need at least 12 characters; the database stores only password hashes. **Отключить** prevents login and rejects existing tokens on their next request. **Удалить аккаунт** appears when no tickets or other records reference the operator. The administrator cannot be deleted through this panel. Authorization is enforced by the API, including requests made outside the UI.

The **Все обращения** tab in `/adminpanel` shows every ticket, including tickets assigned to other operators and older tickets without an assignment. It supports searches by ticket number, subscriber identifier, or creator and shows 20 tickets per page. The administrator can open and permanently delete a ticket. Deleting it also removes its diagnostic sessions, answers and status history; it does not remove the linked service or client. Once all references to an operator are gone, the account can be permanently removed from **Операторы**.

The **Подключения** tab lets the administrator create and list clients, addresses, tariffs and internet services. The **Аренда роутеров** tab does the same for router models, routers and rentals and can record a router's return. A router can have only one active rental through this API. These catalogs are restricted to administrators.

An operator creating a ticket must first look up an active connection using either the exact contract number and client full name, or the client's phone number. Phone formatting is ignored; several clients can share a name or phone number. The matching connections list displays each service's address and contract number, and the operator explicitly selects the correct one. New tickets store its `service_id` and a snapshot of the contract number in `subscriber_id`. Older tickets with a null `service_id` remain readable.

## Upgrade from v1 on macOS

1. Stop the running FastAPI process with **Control+C**. Keep the PostgreSQL container running.
2. Extract the update archive into your existing `NetExpert` folder, replacing the included files. The archive does **not** contain `.env` and does not remove existing tickets.
3. Activate the virtual environment and install the added dependencies:

   ```bash
   cd ~/Documents/NetExpert
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

4. Set `JWT_SECRET`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` in your existing `.env`. Generate the JWT secret locally:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   Paste the output after `JWT_SECRET=`. For example, set `ADMIN_USERNAME=admin` and `ADMIN_PASSWORD` to a separate password of at least 12 characters. Keep your existing `PGPASSWORD` and other database settings. Do not send or commit these secrets.

5. Start the application:

   ```bash
   python -m uvicorn app.main:app --reload
   ```

   Open http://127.0.0.1:8000/adminpanel and sign in with the administrator account. Create operators there. `/docs` has an **Authorize** button; unauthorized API calls return `401`. The documentation page itself is still visible in local development.

The first post-upgrade connection runs `app/002_staff_auth.sql` inside a transaction and records the migration in `schema_migrations`. New tables are `staff_users` and `schema_migrations`, so the application schema now has 19 tables. Original text audit fields remain, while new records also save foreign keys to verified staff accounts. Existing historical tickets retain `NULL` in those new FK columns; they are visible to the administrator until assigned.

To assign an old ticket, sign into Swagger as an administrator, call `GET /api/staff` to find an operator ID, then call `PATCH /api/tickets/{ticket_id}/assign` with `{"operator_id": 2}` (using the actual ID). Assigned tickets then appear in that operator's list.

This is a local prototype. Use HTTPS before exposing it beyond localhost. Knowledge-base editing is future work.
