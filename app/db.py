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
