import io
import os
import tempfile

import pytest

from app import create_app, db
from app.models import BusinessSettings, User, Client, Quote, Job, Invoice, Request as OrderRequest


@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({
        'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}', 'WTF_CSRF_ENABLED': False,
    })
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


def _make_chain(app):
    with app.app_context():
        c = Client(name='Jane Doe', email='jane@example.com')
        db.session.add(c)
        db.session.flush()
        req = OrderRequest(request_number='REQ-2026-0001', reference_number='REF-2026-0001', client_id=c.id)
        db.session.add(req)
        q = Quote(quote_number='Q-2026-0001', reference_number='REF-2026-0001', client_id=c.id, upload_token='tok1')
        db.session.add(q)
        db.session.flush()
        req.quote_id = q.id
        job = Job(job_number='JOB-2026-0001', reference_number='REF-2026-0001', title='Test job', client_id=c.id, quote_id=q.id)
        db.session.add(job)
        inv = Invoice(invoice_number='INV-2026-0001', reference_number='OLD-LEGACY-9', client_id=c.id, quote_id=q.id)
        db.session.add(inv)
        db.session.commit()
        return q.id, job.id, inv.id, req.id


def test_reference_edit_flags_conflict_without_writing(client):
    quote_id, job_id, invoice_id, req_id = _make_chain(client.application)

    r = client.post(f'/dash/reference/quote/{quote_id}/edit', data={'reference_number': 'REF-2026-9999'})
    assert r.status_code == 200
    assert b'disagree' in r.data.lower()

    with client.application.app_context():
        inv = db.session.get(Invoice, invoice_id)
        assert inv.reference_number == 'OLD-LEGACY-9'


def test_reference_edit_propagates_once_confirmed(client):
    quote_id, job_id, invoice_id, req_id = _make_chain(client.application)

    r = client.post(f'/dash/reference/quote/{quote_id}/edit', data={
        'reference_number': 'REF-2026-9999', 'confirmed': '1',
        'overwrite': [f'invoice:{invoice_id}', f'job:{job_id}', f'request:{req_id}'],
    }, follow_redirects=True)
    assert r.status_code == 200

    with client.application.app_context():
        assert db.session.get(Quote, quote_id).reference_number == 'REF-2026-9999'
        assert db.session.get(Job, job_id).reference_number == 'REF-2026-9999'
        assert db.session.get(Invoice, invoice_id).reference_number == 'REF-2026-9999'
        assert db.session.get(OrderRequest, req_id).reference_number == 'REF-2026-9999'


def test_reference_edit_partial_overwrite_leaves_unselected_alone(client):
    quote_id, job_id, invoice_id, req_id = _make_chain(client.application)

    client.post(f'/dash/reference/quote/{quote_id}/edit', data={
        'reference_number': 'REF-2026-9999', 'confirmed': '1', 'overwrite': f'invoice:{invoice_id}',
    })

    with client.application.app_context():
        assert db.session.get(Invoice, invoice_id).reference_number == 'REF-2026-9999'
        assert db.session.get(Job, job_id).reference_number == 'REF-2026-0001'
        assert db.session.get(OrderRequest, req_id).reference_number == 'REF-2026-0001'


def test_reference_audit_lists_mismatched_chains(client):
    with client.application.app_context():
        c = Client(name='Bob Smith')
        db.session.add(c)
        db.session.flush()
        q = Quote(quote_number='Q-2026-0002', reference_number='REF-A', client_id=c.id, upload_token='tok2')
        db.session.add(q)
        db.session.flush()
        db.session.add(Job(job_number='JOB-2026-0002', reference_number='REF-B', title='Mismatched job', client_id=c.id, quote_id=q.id))
        db.session.commit()

    r = client.get('/dash/reference-audit')
    assert r.status_code == 200
    assert b'REF-A' in r.data and b'REF-B' in r.data


def test_csv_import_dry_run_does_not_persist(client):
    csv_text = 'Name,Email,Phone,Notes\nNew Customer,new@example.com,555-1234,Imported via CSV\n'
    r = client.post('/dash/import/run', data={
        'type': 'customers', 'dry_run': '1',
        'file': (io.BytesIO(csv_text.encode('utf-8')), 'customers.csv'),
    }, content_type='multipart/form-data')
    assert r.status_code == 200

    with client.application.app_context():
        assert Client.query.filter_by(name='New Customer').first() is None


def test_csv_import_commit_creates_and_updates(client):
    with client.application.app_context():
        db.session.add(Client(name='Jane Doe', email='jane@example.com'))
        db.session.commit()

    csv_text = (
        'Name,Email,Phone,Notes\n'
        'New Customer,new@example.com,555-1234,Imported via CSV\n'
        'Jane Doe,jane@example.com,,Updated notes for existing customer\n'
    )
    r = client.post('/dash/import/run', data={'type': 'customers', 'csv_content': csv_text}, follow_redirects=True)
    assert r.status_code == 200

    with client.application.app_context():
        new_customer = Client.query.filter_by(name='New Customer').first()
        assert new_customer is not None and new_customer.phone == '555-1234'
        jane = Client.query.filter_by(email='jane@example.com').first()
        assert jane.notes == 'Updated notes for existing customer'
        assert Client.query.filter_by(name='Jane Doe').count() == 1


def test_csv_import_jobs_link_to_existing_quote(client):
    quote_id, _, _, _ = _make_chain(client.application)

    csv_text = (
        'Reference Number,Job Number,Title,Status,Client,Created At,Quote\n'
        'REF-LINKED,JOB-2026-0003,Second job on same quote,Queued,Jane Doe,,Q-2026-0001\n'
    )
    r = client.post('/dash/import/run', data={'type': 'jobs', 'csv_content': csv_text}, follow_redirects=True)
    assert r.status_code == 200

    with client.application.app_context():
        job = Job.query.filter_by(job_number='JOB-2026-0003').first()
        assert job is not None
        assert job.quote_id == quote_id
        assert job.reference_number == 'REF-LINKED'


def test_csv_import_reports_unresolvable_row_as_error(client):
    csv_bad = 'Filament,Initial Weight (g)\nNonexistentFilament,1000\n'
    r = client.post('/dash/import/run', data={'type': 'spools', 'csv_content': csv_bad}, follow_redirects=True)
    assert r.status_code == 200
    assert b'need fixing' in r.data.lower()


def test_job_number_generation_tolerates_gaps(client):
    """Regression test: generate_job_number() must not collide when the sequence
    has a gap (e.g. a manually-numbered row from a CSV import, or a deleted job) \u2014
    a plain row-count-based generator produces a duplicate in that situation."""
    from app import helpers
    from datetime import datetime

    with client.application.app_context():
        c = Client(name='Gap Test')
        db.session.add(c)
        db.session.flush()
        year = datetime.utcnow().year
        # Only 1 row exists, but its number is far ahead of the count -> a
        # count-based generator would produce a number that's already taken
        # further down the line; the fix must look at the highest suffix instead.
        db.session.add(Job(job_number=f'JOB-{year}-0006', title='Existing', client_id=c.id))
        db.session.commit()

        next_number = helpers.generate_job_number()
        assert next_number == f'JOB-{year}-0007', next_number

