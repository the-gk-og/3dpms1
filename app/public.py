import json
import os
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from werkzeug.utils import secure_filename

from app import db
from app.models import Client, Filament, Job, Quote, Request
from app.helpers import (
    get_business_settings, generate_request_number, send_plain_email,
    notify_admin_new_submission,
    EmailNotConfiguredError, signed_uploads_dir, order_uploads_dir,
    ALLOWED_SIGNED_COPY_EXTENSIONS, ALLOWED_ORDER_FILE_EXTENSIONS, MAX_UPLOAD_SIZE_BYTES,
    verify_turnstile,
)
import secrets

public_bp = Blueprint('public', __name__)


def _turnstile_ok(business):
    """Returns True if Turnstile isn't configured (nothing to check), or if the
    submitted token verifies. Returns False (blocking the submission) otherwise.
    """
    if not business.turnstile_secret_key:
        return True
    token = request.form.get('cf-turnstile-response', '')
    return verify_turnstile(business.turnstile_secret_key, token, request.remote_addr)


def _save_uploaded_files(files, subdir_name):
    """Save a list of werkzeug FileStorage objects under order_uploads_dir()/subdir_name/
    and return the list of stored filenames (relative to that subdirectory).
    """
    if not files:
        return []
    target_dir = os.path.join(order_uploads_dir(), subdir_name)
    os.makedirs(target_dir, exist_ok=True)
    stored = []
    for file in files:
        if not file or not file.filename:
            continue
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext not in ALLOWED_ORDER_FILE_EXTENSIONS:
            continue
        safe_name = secure_filename(file.filename) or 'file'
        stored_name = f'{secrets.token_hex(6)}_{safe_name}'
        file.save(os.path.join(target_dir, stored_name))
        stored.append(stored_name)
    return stored


@public_bp.route('/')
def home():
    business = get_business_settings()
    return render_template('public/home.html', business=business)


@public_bp.route('/about')
def about():
    business = get_business_settings()
    return render_template('public/about.html', business=business)


@public_bp.route('/contact', methods=['GET', 'POST'])
def contact():
    business = get_business_settings()
    if request.method == 'POST':
        if not _turnstile_ok(business):
            flash('Please complete the verification challenge and try again.')
            return render_template('public/contact.html', business=business)

        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        message = request.form.get('message', '').strip()
        if not name or not email or not message:
            flash('Please fill in your name, email, and message.')
            return render_template('public/contact.html', business=business)

        notify_target = business.contact_email or business.smtp_from_email
        if notify_target:
            try:
                notify_admin_new_submission(
                    business,
                    subject=f'New contact form message from {name}',
                    body_text=f'From: {name} <{email}>\n\n{message}',
                )
            except EmailNotConfiguredError:
                pass  # Message was still received via the form; email just isn't set up.
            except Exception:
                pass

        return render_template('public/contact.html', business=business, submitted=True)

    return render_template('public/contact.html', business=business)


@public_bp.route('/track', methods=['GET', 'POST'])
def track_order():
    business = get_business_settings()
    order = None
    searched = False
    if request.method == 'POST':
        searched = True
        if not _turnstile_ok(business):
            flash('Please complete the verification challenge and try again.')
            return render_template('public/track_order.html', business=business, order=None, searched=False)

        order_number = (request.form.get('job_number') or '').strip()
        verify_method = request.form.get('verify_method', 'email')
        contact_value = (request.form.get('contact_value') or '').strip().lower()

        candidate = Request.query.filter(Request.request_number.ilike(order_number)).first() if order_number else None
        matched = False
        if candidate and candidate.client and contact_value:
            if verify_method == 'phone':
                stored = (candidate.client.phone or '').strip().lower()
                import re as _re
                matched = bool(stored) and _re.sub(r'\D', '', stored) == _re.sub(r'\D', '', contact_value)
            else:
                stored = (candidate.client.email or '').strip().lower()
                matched = stored == contact_value

        if matched:
            order = candidate
        else:
            flash('No matching order found. Double-check your order number and details.')

    return render_template('public/track_order.html', business=business, order=order, searched=searched)


@public_bp.route('/order', methods=['GET', 'POST'])
def order_form():
    business = get_business_settings()
    filaments = Filament.query.order_by(Filament.name).all()

    if request.method == 'POST':
        if not _turnstile_ok(business):
            flash('Please complete the verification challenge and try again.')
            return render_template('public/order_form.html', business=business, filaments=filaments)

        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        email = request.form.get('email', '').strip()
        shipping_address = request.form.get('shipping_address', '').strip()
        notify_me = request.form.get('notify_me') == 'on'
        model_source = request.form.get('model_source', 'has_model')
        if model_source not in ('has_model', 'need_design'):
            model_source = 'has_model'

        if not name or not phone or not email:
            flash('Please fill in your name, phone number, and email.')
            return render_template('public/order_form.html', business=business, filaments=filaments)

        client = Client.query.filter_by(email=email).first()
        if not client:
            client = Client(name=name, email=email, phone=phone)
            db.session.add(client)
        else:
            client.name = name
            client.phone = phone
        db.session.commit()

        request_number = generate_request_number()

        materials = request.form.getlist('materials')
        other_material = request.form.get('other_material', '').strip()

        model_files = _save_uploaded_files(request.files.getlist('model_files'), request_number)
        reference_images = _save_uploaded_files(request.files.getlist('reference_images'), request_number)

        order_details = {
            'model_source': model_source,
            'shipping_address': shipping_address,
            'model_links': request.form.get('model_links', '').strip(),
            'model_files': model_files,
            'description': request.form.get('description', '').strip(),
            'reference_links': request.form.get('reference_links', '').strip(),
            'reference_images': reference_images,
            'materials': materials,
            'other_material': other_material,
            'notes': request.form.get('notes', '').strip(),
        }

        new_request = Request(
            request_number=request_number,
            client_id=client.id,
            status='New',
            notify_me=notify_me,
            model_source=model_source,
            order_details=json.dumps(order_details),
        )
        db.session.add(new_request)
        db.session.commit()

        try:
            summary_lines = [
                f'New order request: {request_number}',
                f'Client: {name} ({email}, {phone})',
                f'Type: {"Needs design" if model_source == "need_design" else "Has model"}',
            ]
            if order_details.get('description'):
                summary_lines.append(f'Description: {order_details["description"]}')
            if materials:
                summary_lines.append(f'Materials: {", ".join(materials)}')
            if order_details.get('notes'):
                summary_lines.append(f'Notes: {order_details["notes"]}')
            summary_lines.append(f'Review it in the dashboard under Requests.')
            notify_admin_new_submission(
                business,
                subject=f'New order request — {request_number}',
                body_text='\n'.join(summary_lines),
            )
        except EmailNotConfiguredError:
            pass
        except Exception:
            pass

        return render_template('public/order_confirmation.html', business=business, order=new_request)

    return render_template('public/order_form.html', business=business, filaments=filaments)


@public_bp.route('/q/<token>/upload', methods=['GET', 'POST'])
def upload_signed_quote(token):
    """Public page (no login) linked from the quote email — lets a client upload a
    signed copy back to us. Found via an unguessable 256-bit token rather than the
    quote id, so the link can't be enumerated.
    """
    quote = Quote.query.filter_by(upload_token=token).first()
    if not quote:
        abort(404)
    business = get_business_settings()

    if request.method == 'POST':
        file = request.files.get('signed_copy')
        if not file or not file.filename:
            flash('Please choose a file to upload.')
            return redirect(url_for('public.upload_signed_quote', token=token))

        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext not in ALLOWED_SIGNED_COPY_EXTENSIONS:
            flash('Please upload a PDF, PNG, or JPG file.')
            return redirect(url_for('public.upload_signed_quote', token=token))

        if request.content_length and request.content_length > MAX_UPLOAD_SIZE_BYTES:
            flash('That file is too large (15MB max).')
            return redirect(url_for('public.upload_signed_quote', token=token))

        safe_name = secure_filename(file.filename) or 'signed-quote'
        stored_name = f'{quote.id}_{secrets.token_hex(8)}_{safe_name}'
        file.save(os.path.join(signed_uploads_dir(), stored_name))

        quote.signed_copy_filename = stored_name
        quote.signed_copy_uploaded_at = datetime.utcnow()
        quote.notify_me = request.form.get('notify_me') == 'on'
        if quote.status in ('Draft', 'Sent'):
            quote.status = 'Accepted'
        db.session.commit()

        return render_template('upload_signed_quote.html', quote=quote, business=business, uploaded=True)

    return render_template(
        'upload_signed_quote.html', quote=quote, business=business,
        uploaded=bool(quote.signed_copy_filename),
    )
