import os
import tempfile
from datetime import datetime

import pytest

from app import create_app, db
from app.models import (
    BusinessSettings, User, Client, Quote, QuoteItem, Invoice, InvoiceItem, Job,
    Filament, FilamentSpool, FeedbackSurvey,
)


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


def _job_with_filament(spool_weights=(300, 1000), grams=450, via_invoice=False):
    """A job whose quote (or invoice) has one 450g print line on a PLA filament
    with spools of the given remaining weights. Returns (job_id, filament_id)."""
    pla = Filament(name='PLA Black', cost_per_kg=20)
    db.session.add(pla)
    db.session.flush()
    for w in spool_weights:
        db.session.add(FilamentSpool(filament_id=pla.id, initial_weight_g=1000, weight_remaining_g=w))
    client = Client(name='Ava Thompson')
    db.session.add(client)
    db.session.flush()
    quote = Quote(quote_number='Q-1', client_id=client.id)
    db.session.add(quote)
    db.session.flush()
    db.session.add(QuoteItem(quote_id=quote.id, item_type='print', filament_id=pla.id, weight_g=grams))
    db.session.add(QuoteItem(quote_id=quote.id, item_type='flat', quantity=1, rate=10))
    invoice_id = None
    if via_invoice:
        invoice = Invoice(invoice_number='I-1', client_id=client.id, quote_id=quote.id)
        db.session.add(invoice)
        db.session.flush()
        # The invoice was revised to use less filament than the quote.
        db.session.add(InvoiceItem(invoice_id=invoice.id, item_type='print', filament_id=pla.id, weight_g=100))
        invoice_id = invoice.id
    job = Job(job_number='JOB-1', title='Bracket', quote_id=quote.id, invoice_id=invoice_id,
              client_id=client.id, status='Printing')
    db.session.add(job)
    db.session.commit()
    return job.id, pla.id


def _remaining(filament_id):
    return sorted(s.weight_remaining_g for s in Filament.query.get(filament_id).spools)


def test_completing_job_deducts_filament_smallest_spool_first(client):
    with client.application.app_context():
        job_id, fid = _job_with_filament()
    r = client.post(f'/dash/jobs/{job_id}/edit', data={'status': 'Complete'})
    assert r.status_code == 302
    with client.application.app_context():
        # 450g: the 300g spool is emptied first, then 150g from the full spool.
        assert _remaining(fid) == [0, 850]
        job = Job.query.get(job_id)
        assert job.filament_deducted_at is not None
        assert job.filament_usage_summary == [('PLA Black', 450)]


def test_deduction_only_happens_once(client):
    with client.application.app_context():
        job_id, fid = _job_with_filament()
    client.post(f'/dash/jobs/{job_id}/edit', data={'status': 'Complete'})
    client.post(f'/dash/jobs/{job_id}/edit', data={'status': 'Printing'})
    client.post(f'/dash/jobs/{job_id}/edit', data={'status': 'Complete'})
    client.post(f'/dash/jobs/{job_id}/filament/deduct')
    with client.application.app_context():
        assert _remaining(fid) == [0, 850]


def test_invoice_items_take_priority_over_quote(client):
    with client.application.app_context():
        job_id, fid = _job_with_filament(via_invoice=True)
    client.post(f'/dash/jobs/{job_id}/edit', data={'status': 'Complete'})
    with client.application.app_context():
        assert _remaining(fid) == [200, 1000]


def test_shortfall_is_reported_and_spools_stop_at_zero(client):
    with client.application.app_context():
        job_id, fid = _job_with_filament(spool_weights=(100,), grams=450)
    r = client.post(f'/dash/jobs/{job_id}/edit', data={'status': 'Complete'}, follow_redirects=True)
    assert b'short 350g PLA Black' in r.data
    with client.application.app_context():
        assert _remaining(fid) == [0]


def test_undo_puts_filament_back_on_the_same_spools(client):
    with client.application.app_context():
        job_id, fid = _job_with_filament()
    client.post(f'/dash/jobs/{job_id}/edit', data={'status': 'Complete'})
    client.post(f'/dash/jobs/{job_id}/filament/restore')
    with client.application.app_context():
        assert _remaining(fid) == [300, 1000]
        job = Job.query.get(job_id)
        assert job.filament_deducted_at is None
    # ...and it can be deducted again afterwards.
    client.post(f'/dash/jobs/{job_id}/filament/deduct')
    with client.application.app_context():
        assert _remaining(fid) == [0, 850]


def test_low_stock_shows_on_dashboard(client):
    with client.application.app_context():
        f = Filament(name='PETG Clear', cost_per_kg=30)
        db.session.add(f)
        db.session.flush()
        db.session.add(FilamentSpool(filament_id=f.id, initial_weight_g=1000, weight_remaining_g=150))
        db.session.add(Filament(name='TPU Red', cost_per_kg=30))  # no spools at all
        db.session.commit()
    html = client.get('/dash/').get_data(as_text=True)
    assert '2 filaments are running low' in html
    assert 'TPU Red (0g)' in html and 'PETG Clear (150g)' in html


def _survey(name='Mia Chen', rating=5, comments='Great prints!', ok=True, featured=False):
    s = FeedbackSurvey(respondent_name=name, rating=rating, comments=comments,
                       testimonial_ok=ok, featured=featured, responded_at=datetime.utcnow())
    db.session.add(s)
    db.session.commit()
    return s.id


def test_featuring_requires_customer_consent(client):
    with client.application.app_context():
        no_consent = _survey(ok=False)
    client.post(f'/dash/feedback/{no_consent}/feature')
    with client.application.app_context():
        assert not FeedbackSurvey.query.get(no_consent).featured


def test_featured_testimonial_shows_on_public_site_with_short_name(client):
    with client.application.app_context():
        sid = _survey(name='Mia Louise Chen', comments='Fast and friendly, parts fit perfectly.')
    assert b'Fast and friendly' not in client.get('/').data
    client.post(f'/dash/feedback/{sid}/feature')
    home = client.get('/').get_data(as_text=True)
    assert 'Fast and friendly, parts fit perfectly.' in home
    assert 'Mia C.' in home and 'Mia Louise Chen' not in home
    assert 'Fast and friendly' in client.get('/about').get_data(as_text=True)


def test_revoked_consent_hides_featured_testimonial(client):
    with client.application.app_context():
        sid = _survey(comments='Lovely work', featured=True, ok=False)
    assert b'Lovely work' not in client.get('/').data


def test_average_rating_needs_three_reviews(client):
    with client.application.app_context():
        _survey(rating=5, featured=True)
        _survey(rating=4)
    assert b'average from' not in client.get('/').data
    with client.application.app_context():
        _survey(rating=3)
    assert b'average from 3 customer reviews' in client.get('/').data
