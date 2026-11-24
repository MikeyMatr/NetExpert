from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from .db import bootstrap_admin, connection, initialize
from .security import admin_user, create_token, current_user, password_hash, ticket_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(os.getenv('JWT_SECRET', '')) < 32:
        raise RuntimeError('Set JWT_SECRET in .env to a random value of at least 32 characters')
    initialize()
    bootstrap_admin()
    yield


app = FastAPI(title='NetExpert', version='0.1.0', lifespan=lifespan)
FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'
app.mount('/static', StaticFiles(directory=FRONTEND), name='static')


class TicketCreate(BaseModel):
    service_id: int = Field(gt=0)
    problem_description: str = Field(min_length=5, max_length=2000)


class AnswerCreate(BaseModel):
    question_id: int
    answer_code: str


class StatusUpdate(BaseModel):
    status: str
    comment: str = Field(default='', max_length=1000)


class Assignment(BaseModel):
    operator_id: int


class OperatorCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=128)


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=1, max_length=40)


class AddressCreate(BaseModel):
    city: str = Field(min_length=1, max_length=100)
    street: str = Field(min_length=1, max_length=100)
    building: str = Field(min_length=1, max_length=30)
    apartment: str | None = Field(default=None, max_length=30)


class TariffCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    speed_mbps: int = Field(gt=0)


class ServiceCreate(BaseModel):
    client_id: int = Field(gt=0)
    address_id: int = Field(gt=0)
    tariff_id: int = Field(gt=0)
    account_number: str = Field(min_length=1, max_length=100)


class RouterModelCreate(BaseModel):
    manufacturer: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)


class RouterCreate(BaseModel):
    router_model_id: int = Field(gt=0)
    serial_number: str = Field(min_length=1, max_length=100)


class RentalCreate(BaseModel):
    service_id: int = Field(gt=0)
    router_id: int = Field(gt=0)


def insert_catalog(sql: str, params: tuple):
    try:
        with connection() as conn:
            return conn.execute(sql, params).fetchone()
    except UniqueViolation:
        raise HTTPException(409, 'Запись с такими данными уже существует') from None
    except (ForeignKeyViolation, CheckViolation):
        raise HTTPException(422, 'Проверьте выбранные связанные записи') from None


def required_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise HTTPException(422, 'Заполните обязательные поля')
    return text


def allowed_ticket(conn, ticket_id: int, user: dict, lock=False):
    row = conn.execute('SELECT * FROM tickets WHERE id=%s' + (' FOR UPDATE' if lock else ''),
                       (ticket_id,)).fetchone()
    if not row or (user['role'] != 'admin' and row['assigned_operator_id'] != user['id']):
        raise HTTPException(404, 'Ticket not found')
    return row


@app.post('/api/auth/token', include_in_schema=False)
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()]):
    with connection() as conn:
        row = conn.execute('SELECT * FROM staff_users WHERE username=%s', (form.username,)).fetchone()
    if not row or not row['is_active'] or not password_hash.verify(form.password, row['password_hash']):
        raise HTTPException(401, 'Incorrect username or password', headers={'WWW-Authenticate':'Bearer'})
    return {'access_token': create_token(row['id']), 'token_type': 'bearer'}


@app.get('/api/auth/me')
def me(user: Annotated[dict, Depends(current_user)]):
    return {'id': user['id'], 'username': user['username'], 'role': user['role']}


@app.get('/api/staff')
def staff(user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        return conn.execute('''SELECT id, username, role, is_active FROM staff_users
            ORDER BY username''').fetchall()


@app.patch('/api/tickets/{ticket_id}/assign')
def assign_ticket(ticket_id: int, data: Assignment,
                  user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        allowed_ticket(conn, ticket_id, user, lock=True)
        operator = conn.execute('''SELECT id FROM staff_users WHERE id=%s
            AND role='operator' AND is_active''', (data.operator_id,)).fetchone()
        if not operator:
            raise HTTPException(422, 'Active operator not found')
        if conn.execute('''SELECT 1 FROM diagnostic_sessions WHERE ticket_id=%s
            AND status='active' LIMIT 1''', (ticket_id,)).fetchone():
            raise HTTPException(409, 'Complete the active session before reassigning')
        return conn.execute('''UPDATE tickets SET assigned_operator_id=%s, updated_at=now()
            WHERE id=%s RETURNING *''', (data.operator_id, ticket_id)).fetchone()


@app.get('/')
def homepage():
    return FileResponse(FRONTEND / 'index.html')


@app.get('/adminpanel', include_in_schema=False)
def adminpanel():
    # The shell and login are public; all account data and writes require an admin JWT.
    return FileResponse(FRONTEND / 'index.html')


@app.get('/api/admin/operators')
def list_operators(user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        return conn.execute('''SELECT u.id, u.username, u.is_active, u.created_at,
            (SELECT count(*) FROM tickets t WHERE t.created_by_id=u.id OR t.assigned_operator_id=u.id) AS ticket_count,
            NOT EXISTS (SELECT 1 FROM tickets t WHERE t.created_by_id=u.id OR t.assigned_operator_id=u.id)
            AND NOT EXISTS (SELECT 1 FROM ticket_status_events e WHERE e.changed_by_id=u.id)
            AND NOT EXISTS (SELECT 1 FROM diagnostic_sessions s WHERE s.operator_user_id=u.id) AS can_delete
            FROM staff_users u WHERE u.role='operator' ORDER BY u.created_at DESC, u.id DESC''').fetchall()


@app.post('/api/admin/operators', status_code=201)
def add_operator(data: OperatorCreate, user: Annotated[dict, Depends(admin_user)]):
    username = data.username.strip()
    if not username:
        raise HTTPException(422, 'Enter an operator login')
    with connection() as conn:
        row = conn.execute('''INSERT INTO staff_users(username, password_hash, role)
            VALUES (%s, %s, 'operator') ON CONFLICT (username) DO NOTHING
            RETURNING id, username, is_active, created_at''',
            (username, password_hash.hash(data.password))).fetchone()
        if row is None:
            raise HTTPException(409, 'This login is already in use')
        return row


@app.delete('/api/admin/operators/{operator_id}')
def remove_operator(operator_id: int, user: Annotated[dict, Depends(admin_user)]):
    # Keep the row: tickets and audit records reference this operator.
    with connection() as conn:
        row = conn.execute('''UPDATE staff_users SET is_active=FALSE
            WHERE id=%s AND role='operator' AND is_active=TRUE RETURNING id, username''',
            (operator_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Active operator not found')
        return row


@app.post('/api/admin/operators/{operator_id}/restore')
def restore_operator(operator_id: int, user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        row = conn.execute('''UPDATE staff_users SET is_active=TRUE
            WHERE id=%s AND role='operator' AND is_active=FALSE
            RETURNING id, username''', (operator_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Inactive operator not found')
        return row


@app.delete('/api/admin/operators/{operator_id}/permanent')
def delete_operator(operator_id: int, user: Annotated[dict, Depends(admin_user)]):
    try:
        with connection() as conn:
            row = conn.execute("SELECT id FROM staff_users WHERE id=%s AND role='operator' FOR UPDATE",
                               (operator_id,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Оператор не найден')
            refs = conn.execute('''SELECT
                (SELECT count(*) FROM tickets WHERE created_by_id=%s OR assigned_operator_id=%s)
              + (SELECT count(*) FROM ticket_status_events WHERE changed_by_id=%s)
              + (SELECT count(*) FROM diagnostic_sessions WHERE operator_user_id=%s) AS total''',
                                (operator_id,) * 4).fetchone()['total']
            if refs:
                raise HTTPException(409, 'Сначала удалите связанные обращения и записи истории')
            conn.execute('DELETE FROM staff_users WHERE id=%s', (operator_id,))
        return {'id': operator_id}
    except ForeignKeyViolation:
        raise HTTPException(409, 'У оператора остались связанные записи') from None


@app.get('/api/admin/tickets')
def admin_tickets(user: Annotated[dict, Depends(admin_user)],
                  page: int = Query(default=1, ge=1),
                  search: str = Query(default='', max_length=100)):
    """Paginate across every ticket, including unassigned historical records."""
    search = search.strip()
    where = '''WHERE (%s = '' OR t.subscriber_id ILIKE %s
        OR t.id::text = %s OR t.created_by ILIKE %s)'''
    term = f'%{search}%'
    params = (search, term, search, term)
    page_size = 20
    with connection() as conn:
        total = conn.execute('SELECT count(*) AS total FROM tickets t ' + where, params).fetchone()['total']
        rows = conn.execute('''SELECT t.id, t.subscriber_id, t.problem_description,
            t.status, t.created_at, t.created_by, s.username AS assigned_operator
            FROM tickets t LEFT JOIN staff_users s ON s.id=t.assigned_operator_id ''' + where + '''
            ORDER BY t.created_at DESC, t.id DESC LIMIT %s OFFSET %s''',
            params + (page_size, (page - 1) * page_size)).fetchall()
    return {'items': rows, 'total': total, 'page': page, 'page_size': page_size}


@app.delete('/api/admin/tickets/{ticket_id}')
def delete_ticket(ticket_id: int, user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        row = conn.execute('SELECT id FROM tickets WHERE id=%s FOR UPDATE', (ticket_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Обращение не найдено')
        # session_answers and ticket_status_events cascade through their parent rows.
        conn.execute('DELETE FROM diagnostic_sessions WHERE ticket_id=%s', (ticket_id,))
        conn.execute('DELETE FROM tickets WHERE id=%s', (ticket_id,))
    return {'id': ticket_id}


@app.get('/api/admin/catalog')
def admin_catalog(user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        return {
            'clients': conn.execute('SELECT id, name, phone FROM clients ORDER BY id DESC').fetchall(),
            'addresses': conn.execute('SELECT id, city, street, building, apartment FROM addresses ORDER BY id DESC').fetchall(),
            'tariffs': conn.execute('SELECT id, name, speed_mbps FROM tariffs ORDER BY id DESC').fetchall(),
            'services': conn.execute('''SELECT s.id, s.account_number, s.active, s.client_id,
                s.address_id, s.tariff_id, c.name AS client_name, a.city, a.street, a.building,
                t.name AS tariff_name FROM internet_services s JOIN clients c ON c.id=s.client_id
                JOIN addresses a ON a.id=s.address_id JOIN tariffs t ON t.id=s.tariff_id
                ORDER BY s.id DESC''').fetchall(),
            'router_models': conn.execute('SELECT id, manufacturer, model FROM router_models ORDER BY id DESC').fetchall(),
            'routers': conn.execute('''SELECT r.id, r.serial_number, r.router_model_id,
                m.manufacturer, m.model FROM routers r JOIN router_models m ON m.id=r.router_model_id
                ORDER BY r.id DESC''').fetchall(),
            'rentals': conn.execute('''SELECT rr.id, rr.service_id, rr.router_id, rr.rented_at,
                rr.returned_at, s.account_number, r.serial_number FROM router_rentals rr
                JOIN internet_services s ON s.id=rr.service_id JOIN routers r ON r.id=rr.router_id
                ORDER BY rr.id DESC''').fetchall(),
        }


@app.post('/api/admin/clients', status_code=201)
def add_client(data: ClientCreate, user: Annotated[dict, Depends(admin_user)]):
    phone = required_text(data.phone)
    if len([ch for ch in phone if ch.isascii() and ch.isdigit()]) < 6:
        raise HTTPException(422, 'Укажите телефон с не менее чем 6 цифрами')
    return insert_catalog('INSERT INTO clients(name,phone) VALUES (%s,%s) RETURNING id',
                          (required_text(data.name), phone))


@app.post('/api/admin/addresses', status_code=201)
def add_address(data: AddressCreate, user: Annotated[dict, Depends(admin_user)]):
    return insert_catalog('''INSERT INTO addresses(city,street,building,apartment)
        VALUES (%s,%s,%s,%s) RETURNING id''',
        (required_text(data.city), required_text(data.street), required_text(data.building),
         data.apartment.strip() if data.apartment else None))


@app.post('/api/admin/tariffs', status_code=201)
def add_tariff(data: TariffCreate, user: Annotated[dict, Depends(admin_user)]):
    return insert_catalog('INSERT INTO tariffs(name,speed_mbps) VALUES (%s,%s) RETURNING id',
                          (required_text(data.name), data.speed_mbps))


@app.post('/api/admin/services', status_code=201)
def add_service(data: ServiceCreate, user: Annotated[dict, Depends(admin_user)]):
    return insert_catalog('''INSERT INTO internet_services(client_id,address_id,tariff_id,account_number)
        VALUES (%s,%s,%s,%s) RETURNING id''',
        (data.client_id, data.address_id, data.tariff_id, required_text(data.account_number)))


@app.post('/api/admin/router-models', status_code=201)
def add_router_model(data: RouterModelCreate, user: Annotated[dict, Depends(admin_user)]):
    return insert_catalog('''INSERT INTO router_models(manufacturer,model)
        VALUES (%s,%s) RETURNING id''', (required_text(data.manufacturer), required_text(data.model)))


@app.post('/api/admin/routers', status_code=201)
def add_router(data: RouterCreate, user: Annotated[dict, Depends(admin_user)]):
    return insert_catalog('''INSERT INTO routers(router_model_id,serial_number)
        VALUES (%s,%s) RETURNING id''', (data.router_model_id, required_text(data.serial_number)))


@app.post('/api/admin/rentals', status_code=201)
def add_rental(data: RentalCreate, user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        router = conn.execute('SELECT id FROM routers WHERE id=%s FOR UPDATE', (data.router_id,)).fetchone()
        service = conn.execute('SELECT id FROM internet_services WHERE id=%s AND active',
                               (data.service_id,)).fetchone()
        if not router or not service:
            raise HTTPException(422, 'Выберите действующее подключение и существующий роутер')
        active = conn.execute('SELECT id FROM router_rentals WHERE router_id=%s AND returned_at IS NULL',
                              (data.router_id,)).fetchone()
        if active:
            raise HTTPException(409, 'Роутер уже находится в аренде')
        return conn.execute('''INSERT INTO router_rentals(service_id,router_id)
            VALUES (%s,%s) RETURNING id''', (data.service_id, data.router_id)).fetchone()


@app.post('/api/admin/rentals/{rental_id}/return')
def return_router(rental_id: int, user: Annotated[dict, Depends(admin_user)]):
    with connection() as conn:
        row = conn.execute('''UPDATE router_rentals SET returned_at=now()
            WHERE id=%s AND returned_at IS NULL RETURNING id''', (rental_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Активная аренда не найдена')
        return row


@app.get('/api/health')
def health():
    with connection() as conn:
        conn.execute('SELECT 1')
    return {'status': 'ok'}


@app.get('/api/tickets')
def tickets(user: Annotated[dict, Depends(ticket_user)],
            search: str = Query(default='', max_length=100)):
    with connection() as conn:
        owner_filter = '' if user['role'] == 'admin' else 'assigned_operator_id=%s AND '
        owner_params = () if user['role'] == 'admin' else (user['id'],)
        if search.strip():
            term = '%' + search.strip() + '%'
            rows = conn.execute('''SELECT id, subscriber_id, problem_description, status, created_at
                FROM tickets WHERE ''' + owner_filter + '''(subscriber_id ILIKE %s OR id::text = %s)
                ORDER BY created_at DESC LIMIT 100''', owner_params + (term, search.strip())).fetchall()
        else:
            rows = conn.execute('''SELECT id, subscriber_id, problem_description, status, created_at
                FROM tickets WHERE ''' + owner_filter + '''TRUE
                ORDER BY created_at DESC LIMIT 100''', owner_params).fetchall()
    return rows


@app.get('/api/services/lookup')
def lookup_services(user: Annotated[dict, Depends(ticket_user)],
                    mode: str = Query(pattern='^(contract|phone)$'),
                    account_number: str = Query(default='', max_length=100),
                    full_name: str = Query(default='', max_length=200),
                    phone: str = Query(default='', max_length=40)):
    if mode == 'contract':
        if not account_number.strip() or not full_name.strip():
            raise HTTPException(422, 'Укажите номер договора и ФИО')
        condition = 's.account_number=%s AND lower(btrim(c.name))=lower(%s)'
        params = (account_number.strip(), full_name.strip())
    else:
        digits = ''.join(ch for ch in phone if ch.isascii() and ch.isdigit())
        if len(digits) < 6:
            raise HTTPException(422, 'Укажите номер телефона (не менее 6 цифр)')
        condition = "regexp_replace(c.phone, '[^0-9]', '', 'g')=%s"
        params = (digits,)
    with connection() as conn:
        return conn.execute('''SELECT s.id, s.account_number, c.name AS client_name, c.phone,
            a.city, a.street, a.building, a.apartment, t.name AS tariff_name
            FROM internet_services s JOIN clients c ON c.id=s.client_id
            JOIN addresses a ON a.id=s.address_id JOIN tariffs t ON t.id=s.tariff_id
            WHERE s.active AND ''' + condition + ' ORDER BY s.id LIMIT 50', params).fetchall()


@app.post('/api/tickets', status_code=201)
def create_ticket(data: TicketCreate, user: Annotated[dict, Depends(ticket_user)]):
    with connection() as conn:
        service = conn.execute('SELECT account_number FROM internet_services WHERE id=%s AND active',
                               (data.service_id,)).fetchone()
        if service is None:
            raise HTTPException(422, 'Выбранное подключение не найдено или отключено')
        row = conn.execute('''INSERT INTO tickets
            (subscriber_id, service_id, problem_description, created_by, created_by_id, assigned_operator_id)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING *''',
            (service['account_number'], data.service_id, data.problem_description.strip(),
             user['username'], user['id'], user['id'] if user['role'] == 'operator' else None)).fetchone()
        conn.execute('''INSERT INTO ticket_status_events
            (ticket_id, old_status, new_status, changed_by, changed_by_id, comment)
            VALUES (%s, NULL, 'new', %s, %s, 'Ticket created')''',
            (row['id'], user['username'], user['id']))
    return row


@app.get('/api/tickets/{ticket_id}')
def ticket_detail(ticket_id: int, user: Annotated[dict, Depends(ticket_user)]):
    with connection() as conn:
        row = allowed_ticket(conn, ticket_id, user)
        row['service'] = conn.execute('''SELECT s.account_number, c.name AS client_name, c.phone,
            a.city, a.street, a.building, a.apartment FROM internet_services s
            JOIN clients c ON c.id=s.client_id JOIN addresses a ON a.id=s.address_id
            WHERE s.id=%s''', (row['service_id'],)).fetchone() if row['service_id'] else None
        row['history'] = conn.execute('''SELECT old_status, new_status, changed_by, comment, changed_at
            FROM ticket_status_events WHERE ticket_id=%s ORDER BY changed_at, id''', (ticket_id,)).fetchall()
        row['sessions'] = conn.execute('''SELECT id, status, started_at, finished_at, suspected_cause
            FROM diagnostic_sessions WHERE ticket_id=%s ORDER BY started_at DESC''', (ticket_id,)).fetchall()
    return row


@app.patch('/api/tickets/{ticket_id}/status')
def change_status(ticket_id: int, data: StatusUpdate,
                  user: Annotated[dict, Depends(ticket_user)]):
    allowed = {'new': {'in_progress', 'escalated'},
               'in_progress': {'resolved', 'escalated'},
               'resolved': set(), 'escalated': set()}
    with connection() as conn:
        current = allowed_ticket(conn, ticket_id, user, lock=True)
        if data.status not in allowed[current['status']]:
            raise HTTPException(409, f"Cannot change {current['status']} to {data.status}")
        if data.status == 'resolved':
            active = conn.execute('''SELECT 1 FROM diagnostic_sessions
                WHERE ticket_id=%s AND status='active' LIMIT 1''', (ticket_id,)).fetchone()
            if active:
                raise HTTPException(409, 'Complete the diagnostic session before closing the ticket')
        row = conn.execute('''UPDATE tickets SET status=%s, updated_at=now(),
            closed_at=CASE WHEN %s='resolved' THEN now() ELSE NULL END
            WHERE id=%s RETURNING *''', (data.status, data.status, ticket_id)).fetchone()
        conn.execute('''INSERT INTO ticket_status_events
            (ticket_id, old_status, new_status, changed_by, changed_by_id, comment)
            VALUES (%s,%s,%s,%s,%s,%s)''',
            (ticket_id, current['status'], data.status, user['username'], user['id'], data.comment.strip()))
    return row


@app.post('/api/tickets/{ticket_id}/sessions', status_code=201)
def start_session(ticket_id: int, user: Annotated[dict, Depends(ticket_user)]):
    with connection() as conn:
        ticket = allowed_ticket(conn, ticket_id, user, lock=True)
        if ticket['status'] not in ('new', 'in_progress'):
            raise HTTPException(409, 'Ticket is already closed or escalated')
        active = conn.execute('''SELECT id FROM diagnostic_sessions
            WHERE ticket_id=%s AND status='active' LIMIT 1''', (ticket_id,)).fetchone()
        if active:
            raise HTTPException(409, 'This ticket already has an active session')
        version = conn.execute('SELECT id FROM knowledge_versions WHERE is_active').fetchone()
        if version is None:
            raise HTTPException(503, 'No published knowledge version')
        row = conn.execute('''INSERT INTO diagnostic_sessions
            (id, ticket_id, operator_id, operator_user_id, knowledge_version_id)
            VALUES (%s,%s,%s,%s,%s) RETURNING id''',
            (uuid4(), ticket_id, user['username'], user['id'], version['id'])).fetchone()
        if ticket['status'] == 'new':
            conn.execute("UPDATE tickets SET status='in_progress', updated_at=now() WHERE id=%s", (ticket_id,))
            conn.execute('''INSERT INTO ticket_status_events
                (ticket_id, old_status, new_status, changed_by, changed_by_id, comment)
                VALUES (%s,'new','in_progress',%s,%s,'Diagnosis started')''',
                (ticket_id, user['username'], user['id']))
        return _view(conn, _session(conn, row['id'], user))


def _session(conn, session_id: UUID, user: dict, lock=False):
    row = conn.execute('''SELECT * FROM diagnostic_sessions WHERE id=%s'''+
                       (' FOR UPDATE' if lock else ''), (session_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Diagnostic session not found')
    allowed_ticket(conn, row['ticket_id'], user)
    return row


def _view(conn, row):
    ticket = conn.execute('SELECT status FROM tickets WHERE id=%s', (row['ticket_id'],)).fetchone()
    answers = conn.execute('''SELECT sa.question_id, q.prompt, sa.answer_code, sa.step_no
        FROM session_answers sa JOIN questions q ON q.id=sa.question_id
        WHERE sa.session_id=%s ORDER BY sa.step_no''', (row['id'],)).fetchall()
    question = None
    if row['status'] == 'active':
        question = conn.execute('''SELECT q.id, q.prompt, q.code FROM questions q
            WHERE NOT EXISTS (SELECT 1 FROM session_answers sa
                WHERE sa.session_id=%s AND sa.question_id=q.id)
            ORDER BY q.sort_order LIMIT 1''', (row['id'],)).fetchone()
        if question:
            question['options'] = conn.execute('''SELECT code, label FROM answer_options
                WHERE question_id=%s ORDER BY sort_order''', (question['id'],)).fetchall()
    return {'session': row, 'ticket_status': ticket['status'], 'answers': answers, 'question': question}


@app.get('/api/sessions/{session_id}')
def session_detail(session_id: UUID, user: Annotated[dict, Depends(ticket_user)]):
    with connection() as conn:
        return _view(conn, _session(conn, session_id, user))


@app.post('/api/sessions/{session_id}/answers')
def submit_answer(session_id: UUID, data: AnswerCreate,
                  user: Annotated[dict, Depends(ticket_user)]):
    with connection() as conn:
        row = _session(conn, session_id, user, lock=True)
        if row['status'] != 'active':
            raise HTTPException(409, 'Session is already completed')
        current = _view(conn, row)['question']
        if current is None or current['id'] != data.question_id:
            raise HTTPException(409, 'Answer the current question')
        if data.answer_code not in {o['code'] for o in current['options']}:
            raise HTTPException(422, 'Unknown answer option')
        count = conn.execute('SELECT count(*) AS n FROM session_answers WHERE session_id=%s',
                             (session_id,)).fetchone()['n']
        conn.execute('''INSERT INTO session_answers (session_id, question_id, answer_code, step_no)
            VALUES (%s,%s,%s,%s)''', (session_id, data.question_id, data.answer_code, count+1))
        recorded = conn.execute('''SELECT question_id, answer_code FROM session_answers
            WHERE session_id=%s''', (session_id,)).fetchall()
        values = {r['question_id']: r['answer_code'] for r in recorded}
        rules = conn.execute('''SELECT r.code, f.title, r.recommendation,
            r.explanation, r.escalation_required, r.id
            FROM production_rules r JOIN fault_types f ON f.id=r.fault_type_id
            WHERE r.knowledge_version_id=%s ORDER BY r.priority, r.id''',
            (row['knowledge_version_id'],)).fetchall()
        matched = None
        for rule in rules:
            conditions = conn.execute('''SELECT question_id, answer_code FROM rule_conditions
                WHERE rule_id=%s''', (rule['id'],)).fetchall()
            if conditions and all(values.get(c['question_id']) == c['answer_code'] for c in conditions):
                matched = rule
                break
        remaining = conn.execute('''SELECT 1 FROM questions q WHERE NOT EXISTS
            (SELECT 1 FROM session_answers sa WHERE sa.session_id=%s AND sa.question_id=q.id)
            LIMIT 1''', (session_id,)).fetchone()
        if matched or not remaining:
            cause = matched['title'] if matched else 'Причина не определена'
            rec = matched['recommendation'] if matched else 'Передайте обращение второй линии для дополнительной проверки.'
            explanation = matched['explanation'] if matched else 'Ни одно правило не совпало с записанными ответами.'
            escalation = matched['escalation_required'] if matched else True
            conn.execute('''UPDATE diagnostic_sessions SET status='completed', finished_at=now(),
                matched_rule_code=%s, suspected_cause=%s, recommendation=%s,
                explanation=%s, escalation_required=%s WHERE id=%s''',
                (matched['code'] if matched else None, cause, rec, explanation, escalation, session_id))
        row = _session(conn, session_id, user)
        return _view(conn, row)
