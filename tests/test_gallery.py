import io
import os
import shutil
import struct
import tempfile
import zlib

import pytest

from app import create_app, db
from app.models import BusinessSettings, User, Client, Job, GalleryPhoto


def _png_bytes():
    """A valid 1x1 PNG built in memory."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x80\x00'))
            + chunk(b'IEND', b''))


@pytest.fixture()
def app_and_client():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    upload_dir = tempfile.mkdtemp()
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
                      'GALLERY_UPLOAD_DIR': upload_dir})
    with app.app_context():
        db.create_all()
        db.session.add(BusinessSettings(name='Acme 3D'))
        user = User(username='admin', email='admin@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
    with app.test_client() as test_client:
        test_client.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
        yield app, test_client, upload_dir
    with app.app_context():
        db.drop_all()
    os.remove(db_path)
    shutil.rmtree(upload_dir, ignore_errors=True)


def _upload(client, name='bracket.png', data=None, **extra):
    form = {'photos': (io.BytesIO(data if data is not None else _png_bytes()), name)}
    form.update(extra)
    return client.post('/dash/gallery/upload', data=form, content_type='multipart/form-data')


def test_bad_extension_and_fake_image_rejected(app_and_client):
    app, client, upload_dir = app_and_client
    _upload(client, name='evil.svg', data=b'<svg onload="alert(1)"></svg>')
    _upload(client, name='notreally.png', data=b'<html>hi</html>')
    with app.app_context():
        assert GalleryPhoto.query.count() == 0
    assert os.listdir(upload_dir) == []


def test_unpublished_photo_is_private_until_published(app_and_client):
    app, client, upload_dir = app_and_client
    resp = _upload(client, name='Dragon_Bust.png')
    assert resp.status_code == 302
    with app.app_context():
        photo = GalleryPhoto.query.one()
        photo_id = photo.id
        assert photo.published is False
        assert photo.title == 'Dragon Bust'
        # Stored under a random name, not the client's filename.
        assert 'Dragon' not in photo.stored_filename
        assert os.listdir(upload_dir) == [photo.stored_filename]

    # Staff can see the file and the dashboard.
    assert client.get(f'/gallery/photo/{photo_id}').status_code == 200
    assert client.get('/dash/gallery/').status_code == 200

    client.get('/dash/auth/logout')
    anon = client
    page = anon.get('/gallery')
    assert page.status_code == 200
    assert f'/gallery/photo/{photo_id}'.encode() not in page.data
    assert anon.get(f'/gallery/photo/{photo_id}').status_code == 404
    home = anon.get('/')
    assert b'Recent work' not in home.data
    assert b'href="/gallery"' not in home.data  # nav link hidden with nothing published

    # Publish + feature as staff.
    anon.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
    client.post(f'/dash/gallery/{photo_id}/edit', data={'title': 'Dragon bust', 'caption': 'Silk finish', 'material': 'Silk PLA'})
    client.post(f'/dash/gallery/{photo_id}/publish')
    client.post(f'/dash/gallery/{photo_id}/feature')
    client.get('/dash/auth/logout')

    page = anon.get('/gallery')
    assert f'/gallery/photo/{photo_id}'.encode() in page.data
    assert b'Silk finish' in page.data and b'Silk PLA' in page.data
    resp = anon.get(f'/gallery/photo/{photo_id}')
    assert resp.status_code == 200
    assert resp.mimetype == 'image/png'
    home = anon.get('/')
    assert b'Recent work' in home.data
    assert b'href="/gallery"' in home.data


def test_job_upload_links_photo_and_never_shows_client_name(app_and_client):
    app, client, _ = app_and_client
    with app.app_context():
        c = Client(name='Ava Thompson')
        db.session.add(c)
        db.session.flush()
        job = Job(job_number='JOB-7', title='Bracket', client_id=c.id, status='Complete')
        db.session.add(job)
        db.session.commit()
        job_id = job.id
    resp = _upload(client, job_id=str(job_id), next=f'/dash/jobs/{job_id}')
    assert resp.headers['Location'].endswith(f'/dash/jobs/{job_id}')
    assert b'Add photos to gallery' in client.get(f'/dash/jobs/{job_id}').data
    with app.app_context():
        photo = GalleryPhoto.query.one()
        assert photo.job_id == job_id and photo.published is False
        photo.published = True
        db.session.commit()
    client.get('/dash/auth/logout')
    assert b'Ava Thompson' not in client.get('/gallery').data


def test_reorder_and_delete_removes_file(app_and_client):
    app, client, upload_dir = app_and_client
    _upload(client, name='first.png')
    _upload(client, name='second.png')
    with app.app_context():
        order = [p.title for p in GalleryPhoto.query.order_by(GalleryPhoto.sort_order, GalleryPhoto.id.desc())]
        assert order == ['second', 'first']  # newest on top
        first_id = GalleryPhoto.query.filter_by(title='first').one().id
    client.post(f'/dash/gallery/{first_id}/move/up')
    with app.app_context():
        order = [p.title for p in GalleryPhoto.query.order_by(GalleryPhoto.sort_order, GalleryPhoto.id.desc())]
        assert order == ['first', 'second']
        stored = db.session.get(GalleryPhoto, first_id).stored_filename
    assert os.path.isfile(os.path.join(upload_dir, stored))

    client.post(f'/dash/gallery/{first_id}/delete')
    with app.app_context():
        assert db.session.get(GalleryPhoto, first_id) is None
        assert GalleryPhoto.query.count() == 1
    assert not os.path.exists(os.path.join(upload_dir, stored))
    assert client.get(f'/gallery/photo/{first_id}').status_code == 404


def test_gallery_routes_require_login(app_and_client):
    _, client, _ = app_and_client
    client.get('/dash/auth/logout')
    assert client.get('/dash/gallery/').status_code == 302
    assert _upload(client).status_code == 302
