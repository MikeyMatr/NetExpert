import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(Path(__file__).resolve().parents[1] / '.env')


@contextmanager
def connection():
    password = os.getenv('PGPASSWORD')
    if password is None or password == '':
        raise RuntimeError('PGPASSWORD is missing. Copy .env.example to .env and set your password.')
    with psycopg.connect(
        host=os.getenv('PGHOST', '127.0.0.1'),
        port=int(os.getenv('PGPORT', '5432')),
        dbname=os.getenv('PGDATABASE', 'netexpert'),
        user=os.getenv('PGUSER', 'netexpert'),
        password=password,
        row_factory=dict_row,
        connect_timeout=5,
    ) as conn:
        conn.execute('SET search_path TO netexpert_app, public')
        yield conn


def initialize():
    with connection() as conn:
        # Transaction and lock make first-run setup safe across multiple processes.
        conn.execute('SELECT pg_advisory_xact_lock(712345091)')
        conn.execute('CREATE SCHEMA IF NOT EXISTS netexpert_app')
        base = Path(__file__).resolve().parent
        conn.execute((base / 'schema.sql').read_text(encoding='utf-8'))
        conn.execute((base / 'seed.sql').read_text(encoding='utf-8'))
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())''')
        version = '002_staff_auth'
        applied = conn.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (version,)).fetchone()
        if not applied:
            conn.execute((base / '002_staff_auth.sql').read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations(version) VALUES (%s)', (version,))


def bootstrap_admin():
    """Create or synchronize the administrator configured on this installation."""
    username = os.getenv('ADMIN_USERNAME', '').strip()
    password = os.getenv('ADMIN_PASSWORD', '')
    if not username or len(username) > 100 or len(password) < 12:
        raise RuntimeError('Set ADMIN_USERNAME (1-100 characters) and ADMIN_PASSWORD (at least 12 characters) in .env')

    # Imported here because security imports connection from this module.
    from .security import password_hash

    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(712345091)')
        row = conn.execute('SELECT id, role, password_hash, is_active FROM staff_users WHERE username=%s',
                           (username,)).fetchone()
        if row is None:
            conn.execute('''INSERT INTO staff_users(username, password_hash, role)
                VALUES (%s, %s, 'admin')''', (username, password_hash.hash(password)))
        elif row['role'] != 'admin':
            raise RuntimeError('ADMIN_USERNAME is already used by an operator or expert')
        else:
            # A changed .env password takes effect on the next start; old sessions
            # still expire normally and can be invalidated by rotating JWT_SECRET.
            if not password_hash.verify(password, row['password_hash']):
                conn.execute('UPDATE staff_users SET password_hash=%s WHERE id=%s',
                             (password_hash.hash(password), row['id']))
            if not row['is_active']:
                conn.execute('UPDATE staff_users SET is_active=TRUE WHERE id=%s', (row['id'],))
