import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main, security


class Result:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows if self.rows is not None else []


class Database:
    def __init__(self):
        self.users = {}
        self.tickets = {}
        self.next_id = 1

    def execute(self, sql, params=()):
        normalized = ' '.join(sql.split())
        if normalized.startswith('SELECT pg_advisory_xact_lock'):
            return Result()
        if normalized.startswith('SELECT id, role, password_hash, is_active FROM staff_users'):
            return Result(next((u.copy() for u in self.users.values() if u['username'] == params[0]), None))
        if normalized.startswith('SELECT id, username, role, is_active FROM staff_users ORDER BY username'):
            return Result(rows=[{k: u[k] for k in ('id', 'username', 'role', 'is_active')}
                                for u in self.users.values()])
        if normalized.startswith('SELECT id, username, role, is_active FROM staff_users'):
            return Result(self.users.get(params[0], {}).copy() or None)
        if normalized.startswith('SELECT id, username, is_active, created_at FROM staff_users'):
            return Result(rows=[self.public(u) for u in self.users.values() if u['role'] == 'operator'])
        if normalized.startswith('INSERT INTO staff_users'):
            username, hashed = params
            if any(u['username'] == username for u in self.users.values()):
                return Result()
            role = 'admin' if "'admin'" in normalized else 'operator'
            uid = self.next_id
            self.next_id += 1
            self.users[uid] = dict(id=uid, username=username, password_hash=hashed,
                                   role=role, is_active=True, created_at='2026-11-24T00:00:00Z')
            return Result(self.public(self.users[uid]))
        if normalized.startswith('UPDATE staff_users SET password_hash='):
            self.users[params[1]]['password_hash'] = params[0]
            return Result()
        if normalized.startswith('UPDATE staff_users SET is_active=TRUE WHERE id=%s') and "role='operator'" not in normalized:
            self.users[params[0]]['is_active'] = True
            return Result()
        if normalized.startswith('UPDATE staff_users SET is_active='):
            uid = params[0]
            user = self.users.get(uid)
            active = 'SET is_active=TRUE' in normalized
            if not user or user['role'] != 'operator' or user['is_active'] == active:
                return Result()
            user['is_active'] = active
            return Result(self.public(user))
        if normalized.startswith('SELECT count(*) AS total FROM tickets t'):
            return Result({'total': len(self.matched_tickets(params[0]))})
        if normalized.startswith('SELECT t.id, t.subscriber_id, t.problem_description'):
            matches = self.matched_tickets(params[0])
            limit, offset = params[-2:]
            rows = []
            for ticket in matches[offset:offset + limit]:
                row = ticket.copy()
                assigned = self.users.get(ticket['assigned_operator_id'])
                row['assigned_operator'] = assigned['username'] if assigned else None
                rows.append(row)
            return Result(rows=rows)
        if normalized.startswith('SELECT * FROM tickets WHERE id='):
            return Result(self.tickets.get(params[0]))
        if normalized.startswith('SELECT old_status, new_status, changed_by, comment, changed_at'):
            return Result(rows=[])
        if normalized.startswith('SELECT id, status, started_at, finished_at, suspected_cause'):
            return Result(rows=[])
        raise AssertionError(f'Unexpected SQL: {normalized}')

    def matched_tickets(self, search):
        return [t for t in sorted(self.tickets.values(), key=lambda row: row['id'], reverse=True)
                if not search or search.lower() in t['subscriber_id'].lower()
                or search.lower() in t['created_by'].lower() or search == str(t['id'])]

    @staticmethod
    def public(user):
        return {k: user[k] for k in ('id', 'username', 'is_active', 'created_at')}


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        @contextmanager
        def connect():
            yield self.database
        self.patches = [patch.object(module, 'connection', connect) for module in (db, main, security)]
        self.patches.append(patch.dict(os.environ, {
            'ADMIN_USERNAME': 'chief', 'ADMIN_PASSWORD': 'unique-admin-pass-12345',
            'JWT_SECRET': 'test-only-secret-0123456789-very-long',
        }))
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(main.app)

    def test_bootstrap_is_idempotent_and_syncs_password(self):
        db.bootstrap_admin()
        db.bootstrap_admin()
        self.assertEqual(len(self.database.users), 1)
        admin = next(iter(self.database.users.values()))
        self.assertEqual(admin['role'], 'admin')
        os.environ['ADMIN_PASSWORD'] = 'new-admin-pass-12345'
        db.bootstrap_admin()
        self.assertEqual(len(self.database.users), 1)
        self.assertTrue(security.password_hash.verify('new-admin-pass-12345', admin['password_hash']))
        self.assertFalse(security.password_hash.verify('unique-admin-pass-12345', admin['password_hash']))

    def test_bootstrap_rejects_operator_name_collision(self):
        self.database.execute("INSERT INTO staff_users(username, password_hash, role) VALUES (%s, %s, 'operator')",
                              ('chief', security.password_hash.hash('operator-pass-123')))
        with self.assertRaisesRegex(RuntimeError, 'already used'):
            db.bootstrap_admin()
        self.assertEqual(next(iter(self.database.users.values()))['role'], 'operator')

    def test_only_admin_manages_operators_and_deactivation_preserves_row(self):
        db.bootstrap_admin()
        admin_id = next(iter(self.database.users))
        admin_header = {'Authorization': 'Bearer ' + security.create_token(admin_id)}
        self.assertEqual(self.client.get('/adminpanel').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/operators').status_code, 401)
        created = self.client.post('/api/admin/operators', headers=admin_header,
                                   json={'username':'operator1', 'password':'operator-pass-123'} )
        self.assertEqual(created.status_code, 201)
        operator_id = created.json()['id']
        self.assertNotIn('password_hash', created.json())
        self.assertEqual(self.client.post('/api/admin/operators', headers=admin_header,
            json={'username':'operator1', 'password':'operator-pass-123'}).status_code, 409)
        operator_header = {'Authorization':'Bearer ' + security.create_token(operator_id)}
        self.assertEqual(self.client.get('/api/admin/operators', headers=operator_header).status_code, 403)
        self.assertEqual(self.client.delete(f'/api/admin/operators/{operator_id}', headers=operator_header).status_code, 403)
        self.assertEqual(self.client.delete(f'/api/admin/operators/{admin_id}', headers=admin_header).status_code, 404)
        self.assertEqual(self.client.delete(f'/api/admin/operators/{operator_id}', headers=admin_header).status_code, 200)
        self.assertIn(operator_id, self.database.users)
        self.assertFalse(self.database.users[operator_id]['is_active'])
        self.assertEqual(self.client.get('/api/auth/me', headers=operator_header).status_code, 401)
        self.assertEqual(self.client.post(f'/api/admin/operators/{operator_id}/restore', headers=admin_header).status_code, 200)
        self.assertEqual(self.client.get('/api/auth/me', headers=operator_header).status_code, 200)

    def test_admin_sees_all_tickets_with_pages_and_operator_cannot(self):
        db.bootstrap_admin()
        admin_id = next(iter(self.database.users))
        admin_header = {'Authorization':'Bearer ' + security.create_token(admin_id)}
        operator_id = self.database.next_id
        self.database.execute("INSERT INTO staff_users(username, password_hash, role) VALUES (%s, %s, 'operator')",
                              ('alice', security.password_hash.hash('operator-pass-123')))
        operator_header = {'Authorization':'Bearer ' + security.create_token(operator_id)}
        for number in range(1, 46):
            self.database.tickets[number] = dict(id=number, subscriber_id=f'subscriber-{number}',
                problem_description='No internet', status='new', created_at='2026-11-24T00:00:00Z',
                created_by='alice' if number % 2 else 'former-operator',
                assigned_operator_id=operator_id if number == 1 else None)
        self.assertEqual(self.client.get('/api/admin/tickets').status_code, 401)
        self.assertEqual(self.client.get('/api/admin/tickets', headers=operator_header).status_code, 403)
        self.assertEqual(self.client.get('/api/tickets/45', headers=operator_header).status_code, 404)
        first = self.client.get('/api/admin/tickets', headers=admin_header).json()
        self.assertEqual((first['total'], first['page_size'], len(first['items'])), (45, 20, 20))
        self.assertEqual(first['items'][0]['id'], 45)
        last = self.client.get('/api/admin/tickets?page=3', headers=admin_header).json()
        self.assertEqual([t['id'] for t in last['items']], [5, 4, 3, 2, 1])
        self.assertIsNone(first['items'][0]['assigned_operator'])
        self.assertEqual(self.client.get('/api/admin/tickets?search=45', headers=admin_header).json()['total'], 1)
        self.assertEqual(self.client.get('/api/admin/tickets?search=former-operator', headers=admin_header).json()['total'], 22)
        detail = self.client.get('/api/tickets/45', headers=admin_header)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['assigned_operator_id'], None)
        self.assertEqual(self.client.get('/api/admin/tickets?page=0', headers=admin_header).status_code, 422)


if __name__ == '__main__':
    unittest.main()
