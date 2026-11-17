"""Forms behind HTTPS with CSRF switched ON — the way production runs. The normal
test fixtures disable CSRF, which is how a referrer-policy bug on the portal
sign-in page slipped through (every real sign-in failed with HTTP 400)."""
import os
import re
import tempfile

import pytest

import app.portal as portal
from app import create_app, db
from app.models import BusinessSettings, Client

BASE = 'https://shop.example.test'


@pytest.fixture()
def https_client(monkeypatch):
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': True,
                      'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}'})
    with app.app_context():
        db.create_all()
        db.session.add(BusinessSettings(name='Acme 3D', portal_enabled=True))
        db.session.add(Client(name='Ava Thompson', email='ava@example.com'))
        db.session.commit()
    sent = []
    monkeypatch.setattr(portal, '_deliver_email', lambda b, to, subject, text, html: sent.append(text))
    with app.test_client() as c:
        c.sent = sent
        yield c
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _magic_link(c):
    page = c.get(f'{BASE}/account/sign-in').get_data(as_text=True)
    r = c.post(f'{BASE}/account/sign-in', data={'csrf_token': _csrf(page), 'email': 'ava@example.com'},
               headers={'Referer': f'{BASE}/account/sign-in'})
    assert r.status_code in (200, 302)
    return re.search(r'/account/verify/[A-Za-z0-9_\-]+', c.sent[-1]).group(0)


def test_verify_page_referrer_policy_still_sends_origin(https_client):
    html = https_client.get(BASE + _magic_link(https_client)).get_data(as_text=True)
    assert 'content="strict-origin"' in html and 'no-referrer' not in html


def test_magic_link_sign_in_works_over_https_with_csrf(https_client):
    link = _magic_link(https_client)
    page = https_client.get(BASE + link).get_data(as_text=True)
    # "strict-origin" makes the browser send just the origin as the Referer.
    r = https_client.post(BASE + link, data={'csrf_token': _csrf(page)}, headers={'Referer': BASE + '/'})
    assert r.status_code == 302 and r.headers['Location'].endswith('/account/')
    assert https_client.get(f'{BASE}/account/').status_code == 200


def test_post_without_referrer_is_rejected_over_https(https_client):
    """Documents the original failure mode: no Referer at all → 400 from Flask-WTF."""
    link = _magic_link(https_client)
    page = https_client.get(BASE + link).get_data(as_text=True)
    r = https_client.post(BASE + link, data={'csrf_token': _csrf(page)})
    assert r.status_code == 400
