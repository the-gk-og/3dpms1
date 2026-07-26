import json
import os
from datetime import datetime

import stripe
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from werkzeug.utils import secure_filename

from app import db, limiter
from app.models import Client, Filament, Invoice, Job, Quote, Request
from app.helpers import (
    get_business_settings, generate_request_number, send_plain_email,
    notify_admin_new_submission, render_email_template, html_to_text,
    EmailNotConfiguredError, signed_uploads_dir, order_uploads_dir,
    ALLOWED_SIGNED_COPY_EXTENSIONS, ALLOWED_ORDER_FILE_EXTENSIONS, MAX_UPLOAD_SIZE_BYTES,
    verify_turnstile, render_template as render_template_mobile_aware,
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


def _file_size(file):
    """Size in bytes of a werkzeug FileStorage without consuming its stream."""
    pos = file.stream.tell()
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(pos)
    return size


def _save_uploaded_files(files, subdir_name):
    """Save a list of werkzeug FileStorage objects under order_uploads_dir()/subdir_name/
    and return the list of stored filenames (relative to that subdirectory). Files with
    a disallowed extension or that exceed MAX_UPLOAD_SIZE_BYTES are silently skipped —
    the request-level MAX_CONTENT_LENGTH is the hard backstop, this is defense in depth
    per-file so one oversized file doesn't sink an otherwise-valid submission.
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
        if _file_size(file) > MAX_UPLOAD_SIZE_BYTES:
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
@limiter.limit('5 per minute', methods=['POST'])
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
                safe_name = ' '.join(name.split())  # collapse embedded newlines/whitespace for the Subject header
                default_subject = f'New contact form message from {safe_name}'
                default_body = f'From: {name} <{email}>\n\n{message}'
                context = {
                    'business_name': business.name or '', 'contact_name': safe_name,
                    'contact_email': email, 'message': message,
                }
                subject = render_email_template(business.contact_notification_email_subject, context) or default_subject
                html_body = render_email_template(business.contact_notification_email_body_html, context, escape_html=True)
                body_text = html_to_text(html_body) if html_body else default_body
                notify_admin_new_submission(business, subject=subject, body_text=body_text, html_body=html_body)
            except EmailNotConfiguredError:
                pass  # Message was still received via the form; email just isn't set up.
            except Exception:
                pass

        return render_template('public/contact.html', business=business, submitted=True)

    return render_template('public/contact.html', business=business)


@public_bp.route('/track', methods=['GET', 'POST'])
@limiter.limit('15 per minute', methods=['POST'])
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
@limiter.limit('5 per minute', methods=['POST'])
def order_form():
    business = get_business_settings()
    filaments = Filament.query.order_by(Filament.name).all()

    if request.method == 'POST':
        if not _turnstile_ok(business):
            flash('Please complete the verification challenge and try again.')
            return render_template_mobile_aware('public/order_form.html', business=business, filaments=filaments)

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
            return render_template_mobile_aware('public/order_form.html', business=business, filaments=filaments)

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
            default_subject = f'New order request — {request_number}'
            default_body = '\n'.join(summary_lines)
            summary_html_lines = [
                f'Type: {"Needs design" if model_source == "need_design" else "Has model"}',
            ]
            if order_details.get('description'):
                summary_html_lines.append(f'Description: {order_details["description"]}')
            if materials:
                summary_html_lines.append(f'Materials: {", ".join(materials)}')
            context = {
                'business_name': business.name or '', 'contact_name': name,
                'contact_email': email, 'document_number': request_number,
                'summary': ' \u00b7 '.join(summary_html_lines),
            }
            subject = render_email_template(business.order_notification_email_subject, context) or default_subject
            html_body = render_email_template(business.order_notification_email_body_html, context, escape_html=True)
            body_text = html_to_text(html_body) if html_body else default_body
            notify_admin_new_submission(business, subject=subject, body_text=body_text, html_body=html_body)
        except EmailNotConfiguredError:
            pass
        except Exception:
            pass

        return render_template_mobile_aware('public/order_confirmation.html', business=business, order=new_request)

    return render_template_mobile_aware('public/order_form.html', business=business, filaments=filaments)


@public_bp.route('/q/<token>/upload', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
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


@public_bp.route('/feedback/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def feedback_survey(token):
    """Public page (no login) linked from the feedback survey email. Found via an
    unguessable token, same pattern as the quote upload and Stripe pay links.
    """
    from app.models import FeedbackSurvey

    survey = FeedbackSurvey.query.filter_by(token=token).first()
    if not survey:
        abort(404)
    business = get_business_settings()

    if request.method == 'POST':
        if survey.responded_at:
            return render_template('feedback_survey.html', survey=survey, business=business, submitted=True)

        if not _turnstile_ok(business):
            flash('Verification failed. Please try again.')
            return redirect(url_for('public.feedback_survey', token=token))

        def _star(field):
            try:
                val = int(request.form.get(field, 0))
            except (TypeError, ValueError):
                return None
            return val if 1 <= val <= 5 else None

        def _yes_no(field):
            val = request.form.get(field)
            return (val == 'yes') if val in ('yes', 'no') else None

        rating = _star('rating')
        if rating is None:
            flash('Please choose an overall star rating.')
            return redirect(url_for('public.feedback_survey', token=token))

        survey.respondent_name = (request.form.get('respondent_name') or '').strip()[:200] or survey.respondent_name
        survey.respondent_email = (request.form.get('respondent_email') or '').strip()[:200] or survey.respondent_email
        survey.rating = rating
        survey.would_recommend = _yes_no('would_recommend')
        survey.would_order_again = _yes_no('would_order_again')
        survey.print_quality_rating = _star('print_quality_rating')
        survey.customer_service_rating = _star('customer_service_rating')
        survey.communication_rating = _star('communication_rating')
        survey.turnaround_rating = _star('turnaround_rating')
        survey.value_rating = _star('value_rating')
        survey.referral_source = (request.form.get('referral_source') or '').strip()[:100]
        survey.comments = (request.form.get('comments') or '').strip()[:5000]
        survey.improvements = (request.form.get('improvements') or '').strip()[:5000]
        survey.testimonial_ok = request.form.get('testimonial_ok') == 'on'
        survey.responded_at = datetime.utcnow()
        db.session.commit()

        try:
            notify_admin_new_submission(
                business,
                subject=f'New feedback received — {survey.job.display_number if survey.job else survey.client.name}',
                body_text=(
                    f'Rating: {survey.rating}/5\n'
                    f'Would recommend: {"Yes" if survey.would_recommend else ("No" if survey.would_recommend is False else "—")}\n'
                    f'Comments: {survey.comments or "(none)"}\n'
                    f'Could improve: {survey.improvements or "(none)"}'
                ),
            )
        except Exception:
            pass  # best-effort — the response itself is already saved

        return render_template('feedback_survey.html', survey=survey, business=business, submitted=True)

    return render_template(
        'feedback_survey.html', survey=survey, business=business,
        submitted=bool(survey.responded_at),
    )


@public_bp.route('/pay/<token>')
def pay_invoice(token):
    """Public page (no login) linked from the invoice PDF/email — creates a fresh
    Stripe Checkout Session for the invoice's current total and redirects the client
    to Stripe's hosted payment page. Found via an unguessable token, same pattern as
    the quote upload link, so invoices can't be enumerated or guessed.
    """
    invoice = Invoice.query.filter_by(pay_token=token).first()
    if not invoice:
        abort(404)
    business = get_business_settings()

    if invoice.status == 'Paid':
        return render_template('pay_invoice.html', invoice=invoice, business=business, already_paid=True)

    if not business.stripe_secret_key or not invoice.stripe_enabled:
        return render_template(
            'pay_invoice.html', invoice=invoice, business=business,
            error='Online payment isn\u2019t set up for this invoice yet. Please use one of the other payment methods listed on your invoice.',
        )

    stripe.api_key = business.stripe_secret_key

    # invoice.total already bakes in the surcharge when Stripe was the ONE payment
    # method selected on the invoice (see recalculate_invoice_total). But when several
    # methods are offered, the surcharge isn't baked in — it's shown as an advisory
    # note instead, since which one applies depends on how the client ends up paying.
    # Now that they've picked Stripe specifically, add that surcharge on top here.
    methods = [m.strip() for m in (invoice.payment_method or '').split(',') if m.strip()]
    stripe_surcharge_pct = 0.0
    if len(methods) > 1:
        stripe_surcharge_pct = invoice.surcharge_map.get('Stripe', 0) or 0

    line_items = [{
        'price_data': {
            'currency': 'aud',
            'unit_amount': round(invoice.total * 100),
            'product_data': {'name': f'Invoice {invoice.display_number}'},
        },
        'quantity': 1,
    }]
    if stripe_surcharge_pct:
        surcharge_amount = round(invoice.total * stripe_surcharge_pct / 100, 2)
        line_items.append({
            'price_data': {
                'currency': 'aud',
                'unit_amount': round(surcharge_amount * 100),
                'product_data': {'name': f'Card surcharge ({stripe_surcharge_pct:.1f}%)'},
            },
            'quantity': 1,
        })

    try:
        session = stripe.checkout.Session.create(
            mode='payment',
            payment_method_types=['card'],
            line_items=line_items,
            client_reference_id=invoice.pay_token,
            success_url=url_for('public.pay_invoice_success', token=token, _external=True),
            cancel_url=url_for('public.pay_invoice', token=token, _external=True),
        )
    except stripe.error.StripeError:
        return render_template(
            'pay_invoice.html', invoice=invoice, business=business,
            error='We couldn\u2019t start the payment right now. Please try again shortly or use another payment method.',
        )

    return redirect(session.url, code=303)


@public_bp.route('/pay/<token>/success')
def pay_invoice_success(token):
    """Cosmetic thank-you page after Stripe Checkout. This never marks the invoice as
    paid itself \u2014 the webhook is the only trustworthy source of that, since a client
    could land here without the payment actually completing (or skip it entirely).
    """
    invoice = Invoice.query.filter_by(pay_token=token).first()
    if not invoice:
        abort(404)
    business = get_business_settings()
    return render_template('pay_invoice.html', invoice=invoice, business=business, just_paid=True)


@public_bp.route('/webhooks/stripe', methods=['POST'])
def stripe_webhook():
    """Receives payment confirmations from Stripe. This is the only place an invoice
    is actually marked Paid \u2014 never the success-page redirect, which the client's
    browser could skip or which could be hit without a real payment. Signature
    verification stops anyone else from POSTing a fake 'paid' event here.
    """
    business = get_business_settings()
    if not business.stripe_webhook_secret:
        abort(404)

    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, business.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400)

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        token = session.get('client_reference_id')
        invoice = Invoice.query.filter_by(pay_token=token).first() if token else None
        if invoice and invoice.status != 'Paid':
            invoice.status = 'Paid'
            invoice.paid_at = datetime.utcnow()
            db.session.commit()

            try:
                client_name = invoice.client.name if invoice.client else 'A client'
                default_subject = f'Invoice {invoice.display_number} paid \u2014 ${invoice.total:,.2f}'
                default_body = (
                    f'{client_name} just paid invoice {invoice.display_number} for '
                    f'${invoice.total:,.2f} via Stripe.\n\n'
                    f'Paid at: {invoice.paid_at.strftime("%Y-%m-%d %H:%M UTC")}'
                )
                context = {
                    'business_name': business.name or '', 'client_name': client_name,
                    'document_number': invoice.display_number, 'total': f'{invoice.total:,.2f}',
                    'paid_at': invoice.paid_at.strftime('%d %B %Y, %I:%M %p UTC'),
                }
                subject = render_email_template(business.invoice_paid_notification_email_subject, context) or default_subject
                html_body = render_email_template(business.invoice_paid_notification_email_body_html, context, escape_html=True)
                body_text = html_to_text(html_body) if html_body else default_body
                notify_admin_new_submission(business, subject=subject, body_text=body_text, html_body=html_body)
            except Exception:
                # Best-effort notification only \u2014 the invoice is already marked paid above,
                # so a broken SMTP config must never turn into a 500 back to Stripe (which
                # would make Stripe retry the webhook and could send a duplicate email later).
                pass

    return '', 200
