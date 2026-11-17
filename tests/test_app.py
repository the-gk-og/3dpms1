import os
import tempfile

import pytest

from app import create_app, db
from app.models import BusinessSettings, Filament


@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}'})
    with app.app_context():
        db.create_all()
    with app.test_client() as client:
        yield client
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


def test_public_home_is_accessible(client):
    response = client.get('/')
    assert response.status_code == 200


def test_dashboard_requires_login(client):
    response = client.get('/dash/')
    assert response.status_code == 302
    assert '/dash/auth/login' in response.headers['Location']


def test_filament_and_settings_creation(client):
    with client.application.app_context():
        settings = BusinessSettings(name='Acme 3D', contact_email='hello@example.com')
        db.session.add(settings)
        filament = Filament(name='PLA', cost_per_kg=22.5, charge_per_gram=0.08)
        db.session.add(filament)
        db.session.commit()

    with client.application.app_context():
        assert BusinessSettings.query.count() == 1
        assert Filament.query.count() == 1


def test_uploaded_logo_shows_in_public_nav_and_footer(client):
    with client.application.app_context():
        db.session.add(BusinessSettings(name='Acme 3D', logo_path='abc123.png'))
        db.session.commit()
    html = client.get('/').get_data(as_text=True)
    assert html.count('/static/uploads/abc123.png') == 2  # header + footer
    assert 'pub-brand-mark' not in html


def test_legal_pages_hide_builtin_date_when_custom_text_is_set(client):
    with client.application.app_context():
        db.session.add(BusinessSettings(name='Acme 3D'))
        db.session.commit()
    assert 'Last updated: 29 July 2026' in client.get('/privacy').get_data(as_text=True)
    with client.application.app_context():
        s = BusinessSettings.query.first()
        s.privacy_policy = '*Last updated: 1 Jan 2027*\n\nOur policy.'
        s.terms_of_service = 'Our terms.'
        db.session.commit()
    for path in ('/privacy', '/terms'):
        html = client.get(path).get_data(as_text=True)
        assert '29 July 2026' not in html
    assert 'Last updated: 1 Jan 2027' in client.get('/privacy').get_data(as_text=True)
