import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash

from .db import connection

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl='/api/auth/token')


def create_token(user_id: int) -> str:
    secret = os.getenv('JWT_SECRET')
    if not secret or len(secret) < 32:
        raise RuntimeError('Set JWT_SECRET to a random value of at least 32 characters in .env')
    payload = {'sub': str(user_id), 'exp': datetime.now(timezone.utc) + timedelta(minutes=30)}
    return jwt.encode(payload, secret, algorithm='HS256')


def current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> dict:
    secret = os.getenv('JWT_SECRET')
    if not secret or len(secret) < 32:
        raise RuntimeError('JWT_SECRET is not configured')
    try:
        payload = jwt.decode(token, secret, algorithms=['HS256'], options={'require': ['sub', 'exp']})
        user_id = int(payload['sub'])
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid or expired token',
                            headers={'WWW-Authenticate': 'Bearer'}) from None
    with connection() as conn:
        user = conn.execute('''SELECT id, username, role, is_active FROM staff_users
            WHERE id=%s''', (user_id,)).fetchone()
    if not user or not user['is_active']:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Account is inactive or missing',
                            headers={'WWW-Authenticate': 'Bearer'})
    return user


def ticket_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user['role'] not in ('operator', 'admin'):
        raise HTTPException(403, 'Ticket access is limited to operators')
    return user


def admin_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user['role'] != 'admin':
        raise HTTPException(403, 'Administrator access required')
    return user
