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
        self.services = {}
        self.clients = {}
        self.addresses = {}
        self.tariffs = {}
        self.sessions = {}
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
        if normalized.startswith('SELECT u.id, u.username, u.is_active'):
            rows = []
            for u in self.users.values():
                if u['role'] != 'operator':
                    continue
                row = self.public(u)
                row['ticket_count'] = sum(t.get('created_by_id') == u['id'] or
                                          t['assigned_operator_id'] == u['id'] for t in self.tickets.values())
                row['can_delete'] = row['ticket_count'] == 0 and not self.sessions
                rows.append(row)
            return Result(rows=rows)
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
        if normalized.startswith('SELECT s.account_number, c.name AS client_name'):
            service = self.services.get(params[0])
            if not service: return Result()
            client, address = self.clients[service['client_id']], self.addresses[service['address_id']]
            return Result(dict(account_number=service['account_number'], client_name=client['name'],
                               phone=client['phone'], **address))
        if normalized.startswith('SELECT s.id, s.account_number, c.name AS client_name'):
            rows = []
            for service in self.services.values():
                client, address = self.clients[service['client_id']], self.addresses[service['address_id']]
                if not service['active']: continue
                if 's.account_number=%s' in normalized:
                    if service['account_number'] != params[0] or client['name'].lower() != params[1].lower(): continue
                elif ''.join(c for c in client['phone'] if c.isdigit()) != params[0]: continue
                rows.append(dict(id=service['id'], account_number=service['account_number'],
                    client_name=client['name'], phone=client['phone'], **address,
                    tariff_name=self.tariffs[service['tariff_id']]['name']))
            return Result(rows=rows)
        if normalized.startswith('SELECT account_number FROM internet_services WHERE id='):
            service = self.services.get(params[0])
            return Result(service if service and service['active'] else None)
        if normalized.startswith('INSERT INTO tickets'):
            subscriber, service_id, description, creator, creator_id, operator_id = params
            uid = max(self.tickets, default=0) + 1
            row = dict(id=uid, subscriber_id=subscriber, service_id=service_id,
                       problem_description=description, created_by=creator, created_by_id=creator_id,
                       assigned_operator_id=operator_id, status='new', created_at='2026-11-24T00:00:00Z')
            self.tickets[uid] = row
            return Result(row)
        if normalized.startswith('INSERT INTO ticket_status_events'):
            return Result()
        if normalized.startswith('SELECT id FROM tickets WHERE id='):
            return Result({'id': params[0]} if params[0] in self.tickets else None)
        if normalized.startswith('DELETE FROM diagnostic_sessions WHERE ticket_id='):
            self.sessions.pop(params[0], None)
            return Result()
        if normalized.startswith('DELETE FROM tickets WHERE id='):
            self.tickets.pop(params[0], None)
            return Result()
        if normalized.startswith("SELECT id FROM staff_users WHERE id=%s AND role='operator' FOR UPDATE"):
            user = self.users.get(params[0])
            return Result({'id': params[0]} if user and user['role'] == 'operator' else None)
        if normalized.startswith('SELECT (SELECT count(*) FROM tickets WHERE created_by_id='):
            uid = params[0]
            return Result({'total':sum(t.get('created_by_id') == uid or t['assigned_operator_id'] == uid
                                       for t in self.tickets.values()) + len(self.sessions)})
        if normalized.startswith('DELETE FROM staff_users WHERE id='):
            self.users.pop(params[0], None)
            return Result()
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
                assigned_operator_id=operator_id if number == 1 else None, service_id=None, created_by_id=None)
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

    def test_phone_lookup_returns_each_address_and_ticket_is_linked(self):
        db.bootstrap_admin()
        operator_id = self.database.next_id
        self.database.execute("INSERT INTO staff_users(username, password_hash, role) VALUES (%s, %s, 'operator')",
                              ('alice', security.password_hash.hash('operator-pass-123')))
        header = {'Authorization':'Bearer ' + security.create_token(operator_id)}
        self.database.clients[1] = dict(name='Иванов Иван Иванович', phone='+7 (999) 111-22-33')
        self.database.clients[2] = dict(name='Иванов Иван Иванович', phone='79991112233')
        self.database.addresses[1] = dict(city='Москва', street='Ленина', building='1', apartment='10')
        self.database.addresses[2] = dict(city='Москва', street='Мира', building='2', apartment='20')
        self.database.tariffs[1] = dict(name='100 Мбит/с')
        for number in (1, 2):
            self.database.services[number] = dict(id=number, client_id=number, address_id=number,
                tariff_id=1, account_number=f'contract-{number}', active=True)
        found = self.client.get('/api/services/lookup?mode=phone&phone=+7%20(999)%20111-22-33', headers=header)
        self.assertEqual(found.status_code, 200)
        self.assertEqual({s['street'] for s in found.json()}, {'Ленина', 'Мира'})
        by_contract = self.client.get('/api/services/lookup?mode=contract&account_number=contract-2&full_name=Иванов%20Иван%20Иванович', headers=header)
        self.assertEqual([s['id'] for s in by_contract.json()], [2])
        self.assertEqual(self.client.post('/api/tickets', headers=header,
            json={'service_id': 999, 'problem_description':'Нет интернета'}).status_code, 422)
        created = self.client.post('/api/tickets', headers=header,
            json={'service_id': 2, 'problem_description':'Нет интернета'})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()['subscriber_id'], 'contract-2')
        detail = self.client.get('/api/tickets/' + str(created.json()['id']), headers=header)
        self.assertEqual(detail.json()['service']['street'], 'Мира')

    def test_ticket_deletion_unlocks_permanent_operator_deletion(self):
        db.bootstrap_admin()
        admin_id = next(iter(self.database.users))
        admin_header = {'Authorization':'Bearer ' + security.create_token(admin_id)}
        operator_id = self.database.next_id
        self.database.execute("INSERT INTO staff_users(username, password_hash, role) VALUES (%s, %s, 'operator')",
                              ('alice', security.password_hash.hash('operator-pass-123')))
        self.database.tickets[1] = dict(id=1, subscriber_id='contract-1', service_id=None,
            problem_description='Нет интернета', status='new', created_at='2026-11-24T00:00:00Z',
            created_by='alice', created_by_id=operator_id, assigned_operator_id=operator_id)
        self.database.sessions[1] = True
        self.assertEqual(self.client.get('/api/admin/operators', headers=admin_header).json()[0]['can_delete'], False)
        self.assertEqual(self.client.delete(f'/api/admin/operators/{operator_id}/permanent', headers=admin_header).status_code, 409)
        self.assertEqual(self.client.delete('/api/admin/tickets/1', headers={'Authorization':'Bearer ' + security.create_token(operator_id)}).status_code, 403)
        self.assertEqual(self.client.delete('/api/admin/tickets/1', headers=admin_header).status_code, 200)
        self.assertNotIn(1, self.database.sessions)
        self.assertTrue(self.client.get('/api/admin/operators', headers=admin_header).json()[0]['can_delete'])
        self.assertEqual(self.client.delete(f'/api/admin/operators/{operator_id}/permanent', headers=admin_header).status_code, 200)
        self.assertNotIn(operator_id, self.database.users)

    def test_catalog_is_admin_only_and_all_creation_routes_work(self):
        db.bootstrap_admin()
        admin_id = next(iter(self.database.users))
        admin_header = {'Authorization':'Bearer ' + security.create_token(admin_id)}
        operator_id = self.database.next_id
        self.database.execute("INSERT INTO staff_users(username, password_hash, role) VALUES (%s, %s, 'operator')",
                              ('alice', security.password_hash.hash('operator-pass-123')))
        operator_header = {'Authorization':'Bearer ' + security.create_token(operator_id)}

        class CatalogDB:
            def execute(self, sql, params=()):
                normalized = ' '.join(sql.split())
                if normalized.startswith('SELECT id FROM routers WHERE id=') or normalized.startswith('SELECT id FROM internet_services WHERE id='):
                    return Result({'id': 1})
                if normalized.startswith('SELECT id FROM router_rentals WHERE router_id='):
                    return Result()
                if normalized.startswith('INSERT INTO') or normalized.startswith('UPDATE router_rentals'):
                    return Result({'id': 1})
                if normalized.startswith('SELECT'):
                    return Result(rows=[])
                raise AssertionError(normalized)

        @contextmanager
        def catalog_connection():
            yield CatalogDB()

        cases = [
            ('clients', {'name':'Иванов Иван Иванович', 'phone':'+7 999 123-45-67'}),
            ('addresses', {'city':'Москва', 'street':'Ленина', 'building':'1'}),
            ('tariffs', {'name':'Домашний', 'speed_mbps':100}),
            ('services', {'client_id':1, 'address_id':1, 'tariff_id':1, 'account_number':'0001'}),
            ('router-models', {'manufacturer':'Example', 'model':'R1'}),
            ('routers', {'router_model_id':1, 'serial_number':'SN1'}),
            ('rentals', {'service_id':1, 'router_id':1}),
        ]
        with patch.object(main, 'connection', catalog_connection):
            self.assertEqual(self.client.get('/api/admin/catalog', headers=operator_header).status_code, 403)
            self.assertEqual(self.client.get('/api/admin/catalog', headers=admin_header).status_code, 200)
            for name, payload in cases:
                self.assertEqual(self.client.post('/api/admin/' + name, headers=operator_header, json=payload).status_code, 403)
                response = self.client.post('/api/admin/' + name, headers=admin_header, json=payload)
                self.assertEqual(response.status_code, 201, (name, response.text))
            self.assertEqual(self.client.post('/api/admin/rentals/1/return', headers=admin_header).status_code, 200)


if __name__ == '__main__':
    unittest.main()
