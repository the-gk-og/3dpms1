"""Photo gallery of past work.

Staff upload photos under /dash/gallery (or straight from a job page); nothing is
public until a photo is published. Published photos show on /gallery, and
published + featured ones also appear in the "Recent work" strip on the home page.

Files are stored under app/uploads/gallery/ (the ./data/uploads volume in Docker)
with random names, and are served through gallery_photo() rather than as static
files so unpublished photos stay private to signed-in staff.
"""
import os
import secrets

from flask import Blueprint, request, redirect, url_for, flash, abort, send_file, current_app
from flask_login import login_required, current_user
from sqlalchemy import func

from app import db
from app.models import GalleryPhoto, Job
from app.helpers import log_audit, render_template, get_business_settings, _uploads_subdir, MAX_UPLOAD_SIZE_BYTES

gallery_bp = Blueprint('gallery', __name__)

ALLOWED_GALLERY_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
_MIMETYPES = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
              'webp': 'image/webp', 'gif': 'image/gif'}
MAX_FEATURED_ON_HOME = 6


def gallery_uploads_dir():
    override = current_app.config.get('GALLERY_UPLOAD_DIR')
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    return _uploads_subdir('gallery')


def _photo_path(photo):
    # stored_filename is always generated server-side (hex + fixed extension), but
    # keep the basename guard so a tampered row can never escape the directory.
    name = os.path.basename(photo.stored_filename or '')
    if not name:
        return None
    return os.path.join(gallery_uploads_dir(), name)


def _looks_like_image(head, ext):
    """Cheap magic-byte check so a renamed non-image can't be stored as a photo."""
    if ext in ('jpg', 'jpeg'):
        return head.startswith(b'\xff\xd8\xff')
    if ext == 'png':
        return head.startswith(b'\x89PNG\r\n\x1a\n')
    if ext == 'gif':
        return head[:6] in (b'GIF87a', b'GIF89a')
    if ext == 'webp':
        return head[:4] == b'RIFF' and head[8:12] == b'WEBP'
    return False


def save_gallery_photo(file_storage, job=None, uploaded_by=None):
    """Validate and store one uploaded image; returns the new (unpublished)
    GalleryPhoto, or None if the file was empty, the wrong type, or too large."""
    if not file_storage or not file_storage.filename:
        return None
    filename = file_storage.filename
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_GALLERY_EXTENSIONS:
        return None

    stream = file_storage.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size == 0 or size > MAX_UPLOAD_SIZE_BYTES:
        return None
    head = stream.read(16)
    stream.seek(0)
    if not _looks_like_image(head, ext):
        return None

    if ext == 'jpeg':
        ext = 'jpg'
    stored_name = f'{secrets.token_hex(16)}.{ext}'
    file_storage.save(os.path.join(gallery_uploads_dir(), stored_name))

    # New photos go to the top of the list.
    lowest = db.session.query(func.min(GalleryPhoto.sort_order)).scalar()
    title = os.path.splitext(os.path.basename(filename.replace('\\', '/')))[0][:200]
    photo = GalleryPhoto(
        stored_filename=stored_name,
        original_filename=filename[:300],
        title=title.replace('_', ' ').replace('-', ' ').strip() or None,
        job_id=job.id if job else None,
        sort_order=(lowest - 1) if lowest is not None else 0,
        uploaded_by=uploaded_by,
    )
    db.session.add(photo)
    db.session.commit()
    return photo


def _ordered(query=None):
    return (query if query is not None else GalleryPhoto.query).order_by(GalleryPhoto.sort_order.asc(), GalleryPhoto.id.desc())


def published_photos():
    return _ordered(GalleryPhoto.query.filter(GalleryPhoto.published.is_(True))).all()


def featured_photos(limit=MAX_FEATURED_ON_HOME):
    return _ordered(GalleryPhoto.query.filter(GalleryPhoto.published.is_(True),
                                              GalleryPhoto.featured.is_(True))).limit(limit).all()


def has_published_photos():
    return db.session.query(GalleryPhoto.id).filter(GalleryPhoto.published.is_(True)).first() is not None


@gallery_bp.app_context_processor
def _gallery_template_helpers():
    # Exposed as callables so the query only runs on templates that use them.
    return {'gallery_has_published': has_published_photos,
            'gallery_featured_photos': featured_photos}


# --- Public ------------------------------------------------------------------------

@gallery_bp.route('/gallery')
def public_gallery():
    return render_template('public/gallery.html', business=get_business_settings(),
                           photos=published_photos())


@gallery_bp.route('/gallery/photo/<int:photo_id>')
def photo_file(photo_id):
    photo = db.session.get(GalleryPhoto, photo_id)
    if photo is None or (not photo.published and not current_user.is_authenticated):
        abort(404)
    path = _photo_path(photo)
    if path is None or not os.path.isfile(path):
        abort(404)
    ext = photo.stored_filename.rsplit('.', 1)[-1].lower()
    response = send_file(path, mimetype=_MIMETYPES.get(ext, 'application/octet-stream'),
                         conditional=True)
    response.headers['Cache-Control'] = 'public, max-age=3600' if photo.published else 'private, no-store'
    return response


# --- Dashboard ---------------------------------------------------------------------

@gallery_bp.route('/dash/gallery/')
@login_required
def index():
    photos = _ordered().all()
    return render_template('gallery.html', photos=photos,
                           published_count=sum(1 for p in photos if p.published),
                           featured_count=sum(1 for p in photos if p.published and p.featured),
                           max_featured=MAX_FEATURED_ON_HOME)


def _back(photo=None):
    target = request.form.get('next', '')
    # Only allow returning to our own dashboard paths.
    if target.startswith('/dash/') and not target.startswith('//'):
        return redirect(target)
    return redirect(url_for('gallery.index') + (f'#photo-{photo.id}' if photo else ''))


@gallery_bp.route('/dash/gallery/upload', methods=['POST'])
@login_required
def upload():
    job = None
    job_id = request.form.get('job_id', type=int)
    if job_id:
        job = Job.query.get_or_404(job_id)
    files = [f for f in request.files.getlist('photos') if f and f.filename]
    saved = [p for p in (save_gallery_photo(f, job=job, uploaded_by=current_user.username) for f in files) if p]
    rejected = len(files) - len(saved)
    if saved:
        log_audit('gallery_upload', 'job' if job else 'gallery', job.id if job else None,
                  f'{len(saved)} photo(s)')
        flash(f'{len(saved)} photo{"s" if len(saved) != 1 else ""} added to the gallery (unpublished)'
              + (f' — {rejected} skipped' if rejected else ''))
    else:
        flash('No photos were saved — use JPG, PNG, WebP or GIF images up to 15 MB.')
    return _back()


@gallery_bp.route('/dash/gallery/<int:photo_id>/edit', methods=['POST'])
@login_required
def edit(photo_id):
    photo = GalleryPhoto.query.get_or_404(photo_id)
    photo.title = (request.form.get('title') or '').strip()[:200] or None
    photo.caption = (request.form.get('caption') or '').strip()[:2000] or None
    photo.material = (request.form.get('material') or '').strip()[:100] or None
    db.session.commit()
    flash('Photo details saved')
    return _back(photo)


@gallery_bp.route('/dash/gallery/<int:photo_id>/publish', methods=['POST'])
@login_required
def toggle_publish(photo_id):
    photo = GalleryPhoto.query.get_or_404(photo_id)
    photo.published = not photo.published
    db.session.commit()
    log_audit('gallery_publish' if photo.published else 'gallery_unpublish', 'gallery_photo', photo.id)
    flash('Photo published to the website' if photo.published else 'Photo hidden from the website')
    return _back(photo)


@gallery_bp.route('/dash/gallery/<int:photo_id>/feature', methods=['POST'])
@login_required
def toggle_feature(photo_id):
    photo = GalleryPhoto.query.get_or_404(photo_id)
    photo.featured = not photo.featured
    db.session.commit()
    if photo.featured and not photo.published:
        flash('Featured — it will show on the home page once published')
    else:
        flash('Featured on the home page' if photo.featured else 'Removed from the home page')
    return _back(photo)


@gallery_bp.route('/dash/gallery/<int:photo_id>/move/<direction>', methods=['POST'])
@login_required
def move(photo_id, direction):
    if direction not in ('up', 'down'):
        abort(404)
    photos = _ordered().all()
    idx = next((i for i, p in enumerate(photos) if p.id == photo_id), None)
    if idx is None:
        abort(404)
    swap = idx - 1 if direction == 'up' else idx + 1
    if 0 <= swap < len(photos):
        photos[idx], photos[swap] = photos[swap], photos[idx]
        for i, p in enumerate(photos):
            p.sort_order = i
        db.session.commit()
    return _back(photos[swap] if 0 <= swap < len(photos) else None)


@gallery_bp.route('/dash/gallery/<int:photo_id>/delete', methods=['POST'])
@login_required
def delete(photo_id):
    photo = GalleryPhoto.query.get_or_404(photo_id)
    path = _photo_path(photo)
    detail = photo.title or photo.original_filename
    db.session.delete(photo)
    db.session.commit()
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            current_app.logger.warning('Could not remove gallery file %s', path)
    log_audit('gallery_delete', 'gallery_photo', photo_id, detail)
    flash('Photo deleted')
    return _back()
