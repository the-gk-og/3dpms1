import io
import os
import tempfile

import pytest

from app import create_app, db
from app.models import BusinessSettings, User


@pytest.fixture()
def app():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
                      'MAX_CONTENT_LENGTH': 1024})
    with app.app_context():
        db.create_all()
        db.session.add(BusinessSettings(name='Acme 3D'))
        u = User(username='admin', email='admin@example.test')
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()

    @app.route('/boom')
    def boom():
        raise RuntimeError('kaboom')

    yield app
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


def test_public_404_is_branded(app):
    r = app.test_client().get('/no-such-page')
    html = r.get_data(as_text=True)
    assert r.status_code == 404
    assert 'We couldn’t find that page' in html and 'Start an order' in html and 'pub-nav' in html


def test_dashboard_404_uses_dashboard_layout(app):
    c = app.test_client()
    c.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
    r = c.get('/dash/quotes/99999')
    html = r.get_data(as_text=True)
    assert r.status_code == 404 and 'sidebar' in html and 'Dashboard' in html and 'pub-nav' not in html


def test_405_and_413_have_friendly_messages(app):
    c = app.test_client()
    assert 'can’t be used that way' in c.post('/about').get_data(as_text=True)
    r = c.post('/contact', data={'message': 'x' * 5000})
    assert r.status_code == 413 and '1 KB' in r.get_data(as_text=True)


def test_json_endpoints_get_json_errors(app):
    r = app.test_client().post('/order/estimate')  # estimates disabled → 404
    assert r.status_code == 404 and r.is_json and r.get_json()['ok'] is False


def test_unhandled_exception_renders_500_page_in_production_mode(app):
    app.testing = False
    r = app.test_client().get('/boom')
    assert r.status_code == 500 and 'Something went wrong on our end' in r.get_data(as_text=True)


def test_csrf_failure_explains_how_to_retry(app):
    app.config['WTF_CSRF_ENABLED'] = True
    r = app.test_client().post('/contact', data={'name': 'a', 'email': 'a@b.c', 'message': 'hi'})
    assert r.status_code == 400 and 'Your form needs a refresh' in r.get_data(as_text=True)


def test_error_page_falls_back_if_template_rendering_fails(app, monkeypatch):
    import app.errors as errors
    monkeypatch.setattr(errors, 'render_template', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db down')))
    r = app.test_client().get('/no-such-page')
    assert r.status_code == 404 and 'Go to the home page' in r.get_data(as_text=True)
