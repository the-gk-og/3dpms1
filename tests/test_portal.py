import os
import re
import tempfile
from datetime import datetime, timedelta, date

import pytest

import app.portal as portal
from app import create_app, db
from app.models import (
    BusinessSettings, User, Client, Quote, Invoice, Job, Request,
    PortalLoginToken, PortalSession,
)


@pytest.fixture()
def app_ctx():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}'})
    with app.app_context():
        db.create_all()
        db.session.add(BusinessSettings(name='Acme 3D', portal_enabled=True,
                                        stripe_secret_key='sk_test_x'))
        user = User(username='admin', email='admin@example.com')
        user.set_password('password123')
        db.session.add(user)
        ava = Client(name='Ava Thompson', email='Ava@Example.com')
        ben = Client(name='Ben Stone', email='ben@example.com')
        db.session.add_all([ava, ben])
        db.session.flush()
        ava_quote = Quote(quote_number='Q-1', reference_number='REF-AVA', client_id=ava.id,
                          status='Sent', total=120, internal_notes='SECRET-AVA-QUOTE-NOTE',
                          markup_percent=40, upload_token='a' * 64)
        draft_quote = Quote(quote_number='Q-2', reference_number='REF-DRAFT', client_id=ava.id,
                            status='Draft', total=999)
        db.session.add_all([ava_quote, draft_quote])
        db.session.flush()
        db.session.add(Request(request_number='R-1', reference_number='REF-AVA', client_id=ava.id,
                               status='Converted', quote_id=ava_quote.id,
                               internal_notes='SECRET-AVA-REQUEST-NOTE',
                               order_details='{"description": "A small bracket"}'))
        db.session.add(Invoice(invoice_number='I-1', reference_number='REF-AVA', client_id=ava.id,
                               quote_id=ava_quote.id, status='Sent', total=120,
                               due_date=date.today() + timedelta(days=7),
                               internal_notes='SECRET-AVA-INVOICE-NOTE'))
        db.session.add(Job(job_number='J-1', title='Bracket', client_id=ava.id, quote_id=ava_quote.id,
                           status='Printing', internal_notes='SECRET-AVA-JOB-NOTE'))
        db.session.add(Invoice(invoice_number='I-9', reference_number='REF-BEN', client_id=ben.id,
                               status='Sent', total=55))
        db.session.commit()
    yield app
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


@pytest.fixture()
def sent_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(portal, '_deliver_email',
                        lambda business, to, subject, text, html: sent.append(
                            {'to': to, 'subject': subject, 'text': text, 'html': html}))
    return sent


def _link_from(mail):
    m = re.search(r'/account/verify/([A-Za-z0-9_\-]+)', mail['text'])
    assert m, mail['text']
    return m.group(0)


def _sign_in(c, sent_mail, email='ava@example.com', remember=False):
    c.post('/account/sign-in', data={'email': email})
    link = _link_from(sent_mail[-1])
    data = {'remember': 'on'} if remember else {}
    r = c.post(link, data=data)
    assert r.status_code == 302 and r.headers['Location'].endswith('/account/')
    return link


def _ids(app):
    with app.app_context():
        return {
            'ava_invoice': Invoice.query.filter_by(invoice_number='I-1').first().id,
            'ben_invoice': Invoice.query.filter_by(invoice_number='I-9').first().id,
            'ava_quote': Quote.query.filter_by(quote_number='Q-1').first().id,
            'draft_quote': Quote.query.filter_by(quote_number='Q-2').first().id,
            'ava_order': Request.query.filter_by(request_number='R-1').first().id,
        }


def test_sign_in_response_is_identical_for_known_and_unknown_email(app_ctx, sent_mail):
    c = app_ctx.test_client()
    known = c.post('/account/sign-in', data={'email': '  AVA@example.com '})
    unknown = c.post('/account/sign-in', data={'email': 'nobody@example.com'})
    assert known.status_code == unknown.status_code == 200
    assert known.data == unknown.data
    assert b'If we have an account for that address' in known.data
    # Only the real client got an email — case-insensitive match, sent to the stored address.
    assert len(sent_mail) == 1 and sent_mail[0]['to'] == 'Ava@Example.com'
    with app_ctx.app_context():
        token = PortalLoginToken.query.one()
        raw = _link_from(sent_mail[0]).rsplit('/', 1)[1]
        assert raw not in token.token_hash and len(token.token_hash) == 64  # only the hash is stored


def test_magic_link_is_single_use(app_ctx, sent_mail):
    c = app_ctx.test_client()
    c.post('/account/sign-in', data={'email': 'ava@example.com'})
    link = _link_from(sent_mail[0])
    # GET (what a mail scanner does) must not consume the token.
    assert b'Sign in to your account' in c.get(link).data
    assert c.post(link).status_code == 302
    assert c.get('/account/').status_code == 200

    other = app_ctx.test_client()
    assert b'This link has expired' in other.get(link).data
    r = other.post(link)
    assert r.status_code == 302 and r.headers['Location'].endswith('/account/sign-in')
    assert other.get('/account/').status_code == 302


def test_magic_link_expires(app_ctx, sent_mail):
    c = app_ctx.test_client()
    c.post('/account/sign-in', data={'email': 'ava@example.com'})
    link = _link_from(sent_mail[0])
    with app_ctx.app_context():
        token = PortalLoginToken.query.one()
        token.expires_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()
    c.post(link)
    assert c.get('/account/').status_code == 302


def test_sign_in_emails_are_capped_per_client(app_ctx, sent_mail):
    c = app_ctx.test_client()
    for _ in range(portal.MAX_LINKS_PER_WINDOW + 3):
        r = c.post('/account/sign-in', data={'email': 'ava@example.com'})
        assert b'If we have an account for that address' in r.data
    assert len(sent_mail) == portal.MAX_LINKS_PER_WINDOW


def test_overview_shows_own_records_without_staff_data(app_ctx, sent_mail):
    c = app_ctx.test_client()
    _sign_in(c, sent_mail)
    ids = _ids(app_ctx)
    pages = [c.get('/account/'), c.get(f'/account/orders/{ids["ava_order"]}')]
    for r in pages:
        assert r.status_code == 200
        assert r.headers['Cache-Control'] == 'no-store'
        body = r.data.decode()
        assert 'REF-AVA' in body
        assert 'SECRET-' not in body          # no internal_notes of any kind
        assert 'REF-BEN' not in body          # nothing of another client's
        assert 'REF-DRAFT' not in body        # unsent drafts stay hidden
        assert 'markup' not in body.lower()
    overview = pages[0].data.decode()
    assert '/pay/' in overview                # unpaid invoice gets a Pay link
    assert '/q/' + 'a' * 64 + '/upload' in overview
    assert 'Printing' in overview
    assert c.get(f'/account/quotes/{ids["ava_quote"]}/pdf').mimetype == 'application/pdf'
    assert c.get(f'/account/invoices/{ids["ava_invoice"]}/pdf').mimetype == 'application/pdf'


def test_cannot_see_another_clients_records(app_ctx, sent_mail):
    c = app_ctx.test_client()
    _sign_in(c, sent_mail)
    ids = _ids(app_ctx)
    assert c.get(f'/account/invoices/{ids["ben_invoice"]}/pdf').status_code == 404
    assert c.get(f'/account/quotes/{ids["draft_quote"]}/pdf').status_code == 404
    assert c.get('/account/orders/99999').status_code == 404


def test_customer_session_cannot_reach_dashboard(app_ctx, sent_mail):
    c = app_ctx.test_client()
    _sign_in(c, sent_mail)
    for path in ('/dash/', '/dash/invoices', '/dash/settings/'):
        r = c.get(path)
        assert r.status_code == 302 and '/dash/auth/login' in r.headers['Location'], path


def test_staff_session_alone_cannot_reach_account(app_ctx):
    c = app_ctx.test_client()
    r = c.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
    assert r.status_code == 302
    assert c.get('/dash/').status_code == 200
    ids = _ids(app_ctx)
    r = c.get('/account/')
    assert r.status_code == 302 and r.headers['Location'].endswith('/account/sign-in')
    assert c.get(f'/account/invoices/{ids["ava_invoice"]}/pdf').status_code == 302


def test_sign_out_revokes_server_side_session(app_ctx, sent_mail):
    c = app_ctx.test_client()
    _sign_in(c, sent_mail, remember=True)
    assert c.get_cookie('portal_remember', path='/account') is not None
    with c.session_transaction() as s:
        stolen = dict(s)
    c.post('/account/sign-out')
    assert c.get('/account/').status_code == 302
    with app_ctx.app_context():
        assert PortalSession.query.one().revoked_at is not None

    # Replaying the copied cookie contents after sign-out doesn't work.
    thief = app_ctx.test_client()
    with thief.session_transaction() as s:
        s.update(stolen)
    assert thief.get('/account/').status_code == 302


def test_remember_cookie_restores_session(app_ctx, sent_mail):
    c = app_ctx.test_client()
    _sign_in(c, sent_mail, remember=True)
    with c.session_transaction() as s:
        s.clear()  # browser closed: session cookie gone, remember cookie survives
    assert c.get('/account/').status_code == 200


def test_session_ends_when_client_email_removed(app_ctx, sent_mail):
    c = app_ctx.test_client()
    _sign_in(c, sent_mail)
    with app_ctx.app_context():
        Client.query.filter_by(name='Ava Thompson').one().email = ''
        db.session.commit()
    assert c.get('/account/').status_code == 302


def test_portal_disabled_returns_404_and_hides_nav(app_ctx, sent_mail):
    c = app_ctx.test_client()
    assert b'/account/sign-in' in c.get('/').data
    _sign_in(c, sent_mail)
    with app_ctx.app_context():
        BusinessSettings.query.first().portal_enabled = False
        db.session.commit()
    for path in ('/account/', '/account/sign-in', '/account/verify/abc'):
        assert c.get(path).status_code == 404, path
    assert c.post('/account/sign-in', data={'email': 'ava@example.com'}).status_code == 404
    assert b'/account' not in c.get('/').data


def test_nav_shows_name_when_signed_in(app_ctx, sent_mail):
    c = app_ctx.test_client()
    _sign_in(c, sent_mail)
    body = c.get('/').data.decode()
    assert 'Ava' in body and 'href="/account/"' in body
