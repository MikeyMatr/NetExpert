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

from .db import connection, initialize
from .security import admin_user, create_token, current_user, password_hash, ticket_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(os.getenv('JWT_SECRET', '')) < 32:
        raise RuntimeError('Set JWT_SECRET in .env to a random value of at least 32 characters')
    initialize()
    yield


app = FastAPI(title='NetExpert', version='0.1.0', lifespan=lifespan)
FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'
app.mount('/static', StaticFiles(directory=FRONTEND), name='static')


class TicketCreate(BaseModel):
    subscriber_id: str = Field(min_length=1, max_length=100)
    problem_description: str = Field(min_length=5, max_length=2000)


class AnswerCreate(BaseModel):
    question_id: int
    answer_code: str


class StatusUpdate(BaseModel):
    status: str
    comment: str = Field(default='', max_length=1000)


class Assignment(BaseModel):
    operator_id: int


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


@app.post('/api/tickets', status_code=201)
def create_ticket(data: TicketCreate, user: Annotated[dict, Depends(ticket_user)]):
    with connection() as conn:
        row = conn.execute('''INSERT INTO tickets
            (subscriber_id, problem_description, created_by, created_by_id, assigned_operator_id)
            VALUES (%s, %s, %s, %s, %s) RETURNING *''',
            (data.subscriber_id.strip(), data.problem_description.strip(),
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
