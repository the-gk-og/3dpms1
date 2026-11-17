import os
import tempfile
from datetime import datetime, timedelta

import pytest

from app import create_app, db
from app.models import (
    BusinessSettings, User, Client, Job, Filament, FilamentSpool, FeedbackSurvey,
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


# ---- completed_at -------------------------------------------------------------

def _plain_job(status='Printing'):
    c = Client(name='Ava', email='ava@example.com')
    db.session.add(c)
    db.session.flush()
    job = Job(job_number='JOB-1', title='Bracket', client_id=c.id, status=status)
    db.session.add(job)
    db.session.commit()
    return job.id


@pytest.mark.parametrize('endpoint', ['edit', 'notes'])
def test_completed_at_set_and_cleared(client, endpoint):
    app = client.application
    with app.app_context():
        job_id = _plain_job()
    url = f'/dash/jobs/{job_id}/{endpoint}'

    client.post(url, data={'title': 'Bracket', 'status': 'Complete'})
    with app.app_context():
        first = db.session.get(Job, job_id).completed_at
        assert first is not None

    # Re-saving while already Complete keeps the original completion time.
    client.post(url, data={'title': 'Bracket', 'status': 'Complete'})
    with app.app_context():
        assert db.session.get(Job, job_id).completed_at == first

    client.post(url, data={'title': 'Bracket', 'status': 'Printing'})
    with app.app_context():
        assert db.session.get(Job, job_id).completed_at is None


# ---- send-feedback-surveys ----------------------------------------------------

def _seed_survey_jobs(enabled=True, delay=3):
    business = BusinessSettings.query.first()
    business.auto_survey_enabled = enabled
    business.auto_survey_delay_days = delay
    now = datetime.utcnow()
    with_email = Client(name='Ava', email='ava@example.com')
    no_email = Client(name='Ben')
    db.session.add_all([with_email, no_email])
    db.session.flush()

    def job(num, **kw):
        kw.setdefault('client_id', with_email.id)
        kw.setdefault('status', 'Complete')
        j = Job(job_number=num, title=num, **kw)
        db.session.add(j)
        return j

    job('ELIGIBLE', completed_at=now - timedelta(days=5))
    job('TOO-RECENT', completed_at=now - timedelta(days=1))
    job('NOT-COMPLETE', status='Printing', completed_at=None)
    job('ARCHIVED', completed_at=now - timedelta(days=5), archived=True)
    job('LEGACY', completed_at=None)  # completed before completed_at existed
    job('NO-EMAIL', client_id=no_email.id, completed_at=now - timedelta(days=5))
    surveyed = job('SURVEYED', completed_at=now - timedelta(days=5))
    db.session.flush()
    db.session.add(FeedbackSurvey(job_id=surveyed.id, client_id=with_email.id))
    db.session.commit()


@pytest.fixture()
def fake_send(monkeypatch):
    calls = []

    def fake(job, business=None, to_email=None):
        calls.append(job.job_number)
        survey = FeedbackSurvey(job_id=job.id, client_id=job.client_id, sent_at=datetime.utcnow())
        db.session.add(survey)
        db.session.commit()
        return survey

    monkeypatch.setattr('app.helpers.send_feedback_survey_email', fake)
    return calls


def _run(app, *args):
    return app.test_cli_runner().invoke(args=['send-feedback-surveys', '--base-url', 'https://print.example.com', *args])


def test_cli_sends_only_eligible_jobs_once(client, fake_send):
    app = client.application
    with app.app_context():
        _seed_survey_jobs()

    result = _run(app)
    assert result.exit_code == 0, result.output
    assert fake_send == ['ELIGIBLE']
    assert 'Sent 1 survey(s).' in result.output
    assert 'NO-EMAIL' in result.output  # reported as skipped

    result = _run(app)
    assert fake_send == ['ELIGIBLE']  # not sent again
    assert 'Sent 0 survey(s).' in result.output


def test_cli_dry_run_sends_nothing(client, fake_send):
    app = client.application
    with app.app_context():
        _seed_survey_jobs()
    result = _run(app, '--dry-run')
    assert result.exit_code == 0, result.output
    assert fake_send == []
    assert 'Would send 1 survey(s): ELIGIBLE' in result.output
    with app.app_context():
        assert FeedbackSurvey.query.count() == 1  # only the pre-existing one


def test_cli_does_nothing_when_disabled(client, fake_send):
    app = client.application
    with app.app_context():
        _seed_survey_jobs(enabled=False)
    result = _run(app)
    assert fake_send == []
    assert 'turned off' in result.output


def test_cli_real_send_builds_link_and_handles_smtp_missing(client, monkeypatch):
    app = client.application
    with app.app_context():
        _seed_survey_jobs()

    sent = []
    monkeypatch.setattr('app.helpers.send_plain_email',
                        lambda business, to, subject, body, html_body=None: sent.append((to, html_body)))
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert len(sent) == 1 and sent[0][0] == 'ava@example.com'
    assert 'href="https://print.example.com/' in sent[0][1]

    # SMTP not configured: graceful message, and no half-made survey left behind.
    from app.helpers import EmailNotConfiguredError

    def no_smtp(*a, **kw):
        raise EmailNotConfiguredError('no smtp')

    with app.app_context():
        late = Job(job_number='LATE', title='Late', status='Complete',
                   client_id=Client.query.filter_by(name='Ava').first().id,
                   completed_at=datetime.utcnow() - timedelta(days=4))
        db.session.add(late)
        db.session.commit()
        late_id = late.id
    monkeypatch.setattr('app.helpers.send_plain_email', no_smtp)
    result = _run(app)
    assert result.exit_code == 0, result.output
    assert 'SMTP is not configured' in result.output
    with app.app_context():
        assert FeedbackSurvey.query.filter_by(job_id=late_id).count() == 0


def test_auto_survey_settings_save(client):
    client.post('/dash/settings/save', data={'tab': 'email', 'auto_survey_enabled': 'on', 'auto_survey_delay_days': '5'})
    with client.application.app_context():
        b = BusinessSettings.query.first()
        assert b.auto_survey_enabled is True and b.auto_survey_delay_days == 5
    client.post('/dash/settings/save', data={'tab': 'email', 'auto_survey_delay_days': '5'})
    with client.application.app_context():
        assert BusinessSettings.query.first().auto_survey_enabled is False
    assert client.get('/dash/settings/?tab=email').status_code == 200


# ---- restock ------------------------------------------------------------------

def test_suggested_spools_maths():
    from app.restock import suggested_spools
    assert suggested_spools(150, 200, 1000) == 1
    assert suggested_spools(200, 200, 1000) == 1   # at threshold counts as low
    assert suggested_spools(0, 2000, 1000) == 3    # 2 spools would only reach 2000, not above
    assert suggested_spools(0, 2500, 1000) == 3
    assert suggested_spools(100, 1200, 500) == 3
    assert suggested_spools(0, 200, 0) == 1        # bad spool size falls back to 1000g


def test_restock_list_quantities_and_costs(client):
    app = client.application
    with app.app_context():
        old = datetime.utcnow() - timedelta(days=30)
        # Latest spool has a purchase cost -> used as unit cost and its size as spool size.
        a = Filament(name='PLA Black', cost_per_kg=20)
        # Latest spool has no cost -> cost_per_kg x spool kg.
        b = Filament(name='PETG Clear', cost_per_kg=30)
        # No spools at all -> 1000g spool at cost_per_kg.
        c = Filament(name='TPU Red', cost_per_kg=40)
        # Plenty in stock -> not listed.
        d = Filament(name='ABS White', cost_per_kg=25)
        db.session.add_all([a, b, c, d])
        db.session.flush()
        db.session.add_all([
            FilamentSpool(filament_id=a.id, initial_weight_g=1000, weight_remaining_g=50, purchase_cost=99, created_at=old),
            FilamentSpool(filament_id=a.id, initial_weight_g=750, weight_remaining_g=0, purchase_cost=18.5),
            FilamentSpool(filament_id=b.id, initial_weight_g=500, weight_remaining_g=120, purchase_cost=0),
            FilamentSpool(filament_id=d.id, initial_weight_g=1000, weight_remaining_g=900),
        ])
        db.session.commit()

        from app.restock import restock_list
        lines, total = restock_list(200)
        by_name = {l['filament'].name: l for l in lines}
        assert set(by_name) == {'PLA Black', 'PETG Clear', 'TPU Red'}
        assert (by_name['PLA Black']['spool_g'], by_name['PLA Black']['quantity'], by_name['PLA Black']['unit_cost']) == (750, 1, 18.5)
        assert (by_name['PETG Clear']['spool_g'], by_name['PETG Clear']['quantity'], by_name['PETG Clear']['unit_cost']) == (500, 1, 15.0)
        assert (by_name['TPU Red']['spool_g'], by_name['TPU Red']['quantity'], by_name['TPU Red']['unit_cost']) == (1000, 1, 40.0)
        assert total == 73.5

        lines, total = restock_list(1300)
        by_name = {l['filament'].name: l for l in lines}
        assert by_name['PETG Clear']['quantity'] == 3   # 120 + 3x500 = 1620 > 1300
        assert by_name['PETG Clear']['line_total'] == 45.0

    page = client.get('/dash/filament/restock')
    assert page.status_code == 200
    assert b'PLA Black' in page.data and b'$73.50' in page.data and b'Copy list' in page.data
    assert b'ABS White' not in page.data


def test_restock_pages_and_links_render(client):
    assert client.get('/dash/filament/restock').status_code == 200  # empty state
    filament_page = client.get('/dash/filament/')
    assert filament_page.status_code == 200 and b'/dash/filament/restock' in filament_page.data
    mobile = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148'}
    assert client.get('/dash/filament/restock', headers=mobile).status_code == 200
    assert client.get('/dash/filament/', headers=mobile).status_code == 200
