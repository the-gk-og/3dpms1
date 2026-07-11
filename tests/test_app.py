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


def test_home_requires_login(client):
    response = client.get('/')
    assert response.status_code == 302


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
