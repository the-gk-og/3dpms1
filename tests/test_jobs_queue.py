import os
import tempfile
from datetime import date, datetime, timedelta

import pytest

from app import create_app, db
from app.models import (
    BusinessSettings, User, Client, Quote, QuoteItem, Invoice, InvoiceItem, Job, Printer,
)
from app.job_schedule import (
    due_badge, due_soon_jobs, job_print_hours, queue_board, queue_for, queued_hours,
)

MOBILE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'


@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}'})
    with app.app_context():
        db.create_all()
        db.session.add(BusinessSettings(name='Acme 3D'))
        user = User(username='admin', email='admin@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
    with app.test_client() as test_client:
        test_client.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
        yield test_client
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


_seq = [0]


def _job(title='Bracket', status='Queued', due=None, printer_id=None, hours=(), archived=False,
         invoice_hours=None):
    """A job with a quote whose print lines take the given hours (and optionally an
    invoice with its own print hours). Returns the job id."""
    _seq[0] += 1
    n = _seq[0]
    c = Client(name=f'Client {n}')
    db.session.add(c)
    db.session.flush()
    quote = Quote(quote_number=f'Q-{n}', client_id=c.id)
    db.session.add(quote)
    db.session.flush()
    for h in hours:
        db.session.add(QuoteItem(quote_id=quote.id, item_type='print', print_time_hours=h))
    # Non-print lines never count towards print time.
    db.session.add(QuoteItem(quote_id=quote.id, item_type='hourly', quantity=2, print_time_hours=99))
    invoice_id = None
    if invoice_hours is not None:
        inv = Invoice(invoice_number=f'I-{n}', client_id=c.id, quote_id=quote.id)
        db.session.add(inv)
        db.session.flush()
        for h in invoice_hours:
            db.session.add(InvoiceItem(invoice_id=inv.id, item_type='print', print_time_hours=h))
        invoice_id = inv.id
    job = Job(job_number=f'JOB-{n}', title=title, status=status, client_id=c.id, quote_id=quote.id,
              invoice_id=invoice_id, due_date=due, printer_id=printer_id, archived=archived)
    db.session.add(job)
    db.session.commit()
    return job.id


def _printer(name='P1', active=True):
    p = Printer(name=name, active=active)
    db.session.add(p)
    db.session.commit()
    return p.id


def test_due_badge_logic(client):
    today = date(2026, 11, 17)
    with client.application.app_context():
        def badge(**kw):
            return due_badge(Job.query.get(_job(**kw)), today)
        assert badge(due=None) is None
        assert badge(due=today - timedelta(days=1)) == ('Overdue', 'danger')
        assert badge(due=today) == ('Due today', 'warning')
        assert badge(due=today + timedelta(days=1)) == ('Due in 1 day', 'warning')
        assert badge(due=today + timedelta(days=3)) == ('Due in 3 days', 'warning')
        assert badge(due=today + timedelta(days=4)) == ('Due in 4 days', 'muted')
        # Finished jobs don't nag.
        assert badge(due=today - timedelta(days=5), status='Complete') is None
        assert badge(due=today - timedelta(days=5), status='Cancelled') is None


def test_dashboard_due_soon_list(client):
    today = date.today()
    with client.application.app_context():
        _job(title='Late one', due=today - timedelta(days=2))
        _job(title='Today one', due=today)
        _job(title='Next week', due=today + timedelta(days=7))
        _job(title='Far away', due=today + timedelta(days=8))
        _job(title='No date')
        _job(title='Done already', due=today, status='Complete')
        _job(title='Shelved', due=today, archived=True)
        titles = [j.title for j in due_soon_jobs(today)]
        assert titles == ['Late one', 'Today one', 'Next week']
        for i in range(6):
            _job(title=f'Extra {i}', due=today + timedelta(days=1))
        assert len(due_soon_jobs(today)) == 6

    r = client.get('/dash/')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Due soon' in html and 'Late one' in html and 'Overdue' in html
    assert 'Far away' not in html and 'Done already' not in html
    r = client.get('/dash/', headers={'User-Agent': MOBILE_UA})
    assert r.status_code == 200 and 'Late one' in r.get_data(as_text=True)


def test_due_date_set_on_create_and_edit_and_sort(client):
    r = client.post('/dash/jobs/new', data={'client_name': 'Ava', 'title': 'Vase', 'due_date': '2026-12-01'})
    assert r.status_code == 302
    with client.application.app_context():
        job = Job.query.filter_by(title='Vase').one()
        assert job.due_date == date(2026, 12, 1)
        job_id = job.id
        _job(title='Sooner', due=date(2026, 11, 20))
        _job(title='Undated')
    r = client.post(f'/dash/jobs/{job_id}/edit', data={'title': 'Vase', 'status': 'Queued', 'due_date': '2026-11-25'})
    assert r.status_code == 302
    with client.application.app_context():
        assert Job.query.get(job_id).due_date == date(2026, 11, 25)
    # Editing without the field leaves it alone; blanking it clears it.
    client.post(f'/dash/jobs/{job_id}/edit', data={'title': 'Vase', 'status': 'Queued'})
    with client.application.app_context():
        assert Job.query.get(job_id).due_date == date(2026, 11, 25)

    html = client.get('/dash/jobs?sort=due').get_data(as_text=True)
    assert html.index('Sooner') < html.index('Vase') < html.index('Undated')
    html = client.get('/dash/jobs').get_data(as_text=True)
    assert html.index('Undated') < html.index('Sooner') < html.index('Vase')
    assert client.get('/dash/jobs?sort=due', headers={'User-Agent': MOBILE_UA}).status_code == 200

    client.post(f'/dash/jobs/{job_id}/edit', data={'title': 'Vase', 'status': 'Queued', 'due_date': ''})
    with client.application.app_context():
        assert Job.query.get(job_id).due_date is None


def test_print_hours_prefers_invoice_and_ignores_non_print_lines(client):
    with client.application.app_context():
        assert job_print_hours(Job.query.get(_job(hours=(1.5, 2)))) == 3.5
        assert job_print_hours(Job.query.get(_job(hours=(1.5, 2), invoice_hours=(4,)))) == 4
        assert job_print_hours(Job.query.get(_job())) == 0


def test_printer_crud(client):
    r = client.post('/dash/printers/add', data={'name': 'Bambu #1', 'model': 'P1S'})
    assert r.status_code == 302
    with client.application.app_context():
        p = Printer.query.filter_by(name='Bambu #1').one()
        assert p.active and p.model == 'P1S'
        pid = p.id
        job_id = _job(printer_id=pid)
    client.post(f'/dash/printers/{pid}/edit', data={'name': 'Bambu A', 'model': 'P1S', 'notes': '0.4 nozzle'})
    client.post(f'/dash/printers/{pid}/deactivate')
    with client.application.app_context():
        p = Printer.query.get(pid)
        assert p.name == 'Bambu A' and p.notes == '0.4 nozzle' and not p.active
        # Its open job moved to Unassigned rather than vanishing from the board.
        assert Job.query.get(job_id).printer_id is None
    html = client.get('/dash/printers/').get_data(as_text=True)
    assert 'Inactive printers' in html and 'Bambu A' in html
    client.post(f'/dash/printers/{pid}/activate')
    with client.application.app_context():
        assert Printer.query.get(pid).active


def test_assign_job_to_printer(client):
    with client.application.app_context():
        p1, p2 = _printer('P1'), _printer('P2')
        a = _job(title='A', printer_id=p1, hours=(2,))
        Job.query.get(a).queue_position = 0
        db.session.commit()
        b = _job(title='B', hours=(3,))
    r = client.post(f'/dash/printers/jobs/{b}/assign', data={'printer_id': str(p1)})
    assert r.status_code == 302
    with client.application.app_context():
        assert [j.title for j in queue_for(p1)] == ['A', 'B']
        assert Job.query.get(b).queue_position == 1
    # From the job detail page's edit form.
    client.post(f'/dash/jobs/{a}/edit', data={'title': 'A', 'status': 'Queued', 'printer_id': str(p2)})
    with client.application.app_context():
        assert [j.title for j in queue_for(p1)] == ['B']
        assert Job.query.get(b).queue_position == 0
        assert [j.title for j in queue_for(p2)] == ['A']
    client.post(f'/dash/printers/jobs/{a}/assign', data={'printer_id': ''})
    with client.application.app_context():
        assert Job.query.get(a).printer_id is None
    # Inactive printers can't take jobs.
    with client.application.app_context():
        off = _printer('Off', active=False)
    client.post(f'/dash/printers/jobs/{a}/assign', data={'printer_id': str(off)})
    with client.application.app_context():
        assert Job.query.get(a).printer_id is None


def test_reorder_queue(client):
    with client.application.app_context():
        p = _printer()
        # No positions yet: falls back to due date order.
        a = _job(title='A', printer_id=p, due=date(2026, 11, 20))
        b = _job(title='B', printer_id=p, due=date(2026, 11, 21))
        c = _job(title='C', printer_id=p)
        assert [j.title for j in queue_for(p)] == ['A', 'B', 'C']
    client.post(f'/dash/printers/jobs/{c}/move', data={'direction': 'up'})
    with client.application.app_context():
        assert [j.title for j in queue_for(p)] == ['A', 'C', 'B']
    client.post(f'/dash/printers/jobs/{a}/move', data={'direction': 'down'})
    with client.application.app_context():
        assert [j.title for j in queue_for(p)] == ['C', 'A', 'B']
    # Moving past the ends does nothing.
    client.post(f'/dash/printers/jobs/{c}/move', data={'direction': 'up'})
    client.post(f'/dash/printers/jobs/{b}/move', data={'direction': 'down'})
    with client.application.app_context():
        assert [j.title for j in queue_for(p)] == ['C', 'A', 'B']


def test_queue_board_hours_and_finish(client):
    now = datetime(2026, 11, 17, 9, 0)
    with client.application.app_context():
        p = _printer('Prusa')
        _job(title='A', printer_id=p, hours=(1.5, 2))
        _job(title='B', printer_id=p, hours=(1,), invoice_hours=(2.5,))
        _job(title='Finished widget', printer_id=p, hours=(10,), status='Complete')
        _job(title='Loose', hours=(4,))
        assert queued_hours(queue_for(p)) == 6
        cols = queue_board(now)
        prusa, unassigned = cols
        assert prusa['printer'].name == 'Prusa' and unassigned['printer'] is None
        assert [c['job'].title for c in prusa['cards']] == ['A', 'B']
        assert prusa['total_hours'] == 6
        assert prusa['finish_at'] == now + timedelta(hours=6)
        assert prusa['cards'][0]['finish_at'] == now + timedelta(hours=3.5)
        assert unassigned['total_hours'] == 4 and unassigned['finish_at'] is None

    r = client.get('/dash/printers/')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Prusa' in html and '6h queued' in html and 'Unassigned' in html and 'Loose' in html
    assert 'Finished widget' not in html.split('class="queue-board"')[1]
    assert client.get('/dash/printers/', headers={'User-Agent': MOBILE_UA}).status_code == 200


def test_touched_pages_render(client):
    with client.application.app_context():
        p = _printer()
        job_id = _job(printer_id=p, due=date.today(), hours=(2,))
    for url in ['/dash/', '/dash/jobs', '/dash/jobs/new', f'/dash/jobs/{job_id}', '/dash/printers/']:
        for headers in ({}, {'User-Agent': MOBILE_UA}):
            assert client.get(url, headers=headers).status_code == 200, (url, headers)
    html = client.get(f'/dash/jobs/{job_id}').get_data(as_text=True)
    assert 'name="printer_id"' in html and 'name="due_date"' in html and 'Due today' in html
