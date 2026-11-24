"""Local CLI: python -m app.create_staff USERNAME --role operator|expert|admin"""
import argparse
from getpass import getpass

from .db import connection, initialize
from .security import password_hash


def main():
    parser = argparse.ArgumentParser(description='Create a NetExpert staff account')
    parser.add_argument('username')
    parser.add_argument('--role', choices=('operator', 'expert', 'admin'), default='operator')
    args = parser.parse_args()
    username = args.username.strip()
    if not username or len(username) > 100:
        parser.error('username must have 1 to 100 characters')
    password = getpass('Password: ')
    if len(password) < 12:
        parser.error('password must have at least 12 characters')
    if password != getpass('Repeat password: '):
        parser.error('passwords do not match')
    initialize()
    with connection() as conn:
        exists = conn.execute('SELECT 1 FROM staff_users WHERE username=%s', (username,)).fetchone()
        if exists:
            parser.error('username already exists')
        conn.execute('''INSERT INTO staff_users(username,password_hash,role)
            VALUES (%s,%s,%s)''', (username, password_hash.hash(password), args.role))
    print(f'Created {args.role}: {username}')


if __name__ == '__main__':
    main()
