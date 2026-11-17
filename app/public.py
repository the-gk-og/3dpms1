import json
import os
from datetime import datetime

import stripe
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from werkzeug.utils import secure_filename

from app import db, limiter
from app.models import Client, Filament, Invoice, Job, Quote, Request
from app.testimonials import public_testimonials, rating_summary
from app.payments import (
    payment_choice_needed, checkout_amount, record_stripe_checkout, notify_part_payment,
)
from app.helpers import (
    get_business_settings, generate_request_number, generate_reference_number, send_plain_email,
    notify_admin_new_submission, render_email_template, html_to_text,
    EmailNotConfiguredError, signed_uploads_dir, order_uploads_dir,
    ALLOWED_SIGNED_COPY_EXTENSIONS, ALLOWED_ORDER_FILE_EXTENSIONS, MAX_UPLOAD_SIZE_BYTES,
    verify_turnstile, render_template as render_template_mobile_aware,
    default_email_html, find_trackable_order,
)
import secrets

from app.estimate import describe_combined

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
    return render_template('public/home.html', business=business,
                           testimonials=public_testimonials(), rating=rating_summary())


@public_bp.route('/about')
def about():
    business = get_business_settings()
    materials = [f for f in Filament.query.order_by(Filament.name).all() if f.in_stock]
    return render_template('public/about.html', business=business, materials=materials,
                           testimonials=public_testimonials(limit=3), rating=rating_summary())


@public_bp.route('/terms')
def terms():
    business = get_business_settings()
    return render_template('public/terms.html', business=business)


@public_bp.route('/privacy')
def privacy():
    business = get_business_settings()
    return render_template('public/privacy.html', business=business)


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
                context = {
                    'business_name': business.name or '', 'contact_name': safe_name,
                    'contact_email': email, 'message': message,
                }
                subject = render_email_template(business.contact_notification_email_subject, context) or default_subject
                custom_html = render_email_template(business.contact_notification_email_body_html, context, escape_html=True)
                html_body = custom_html or default_email_html('contact_notification', context, business)
                body_text = html_to_text(html_body)
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

        order = find_trackable_order(order_number, verify_method, contact_value)
        if not order:
            flash('No matching order found. Double-check your order number and details.')

    return render_template('public/track_order.html', business=business, order=order, searched=searched)


def _order_print_estimate(files, form, business):
    """Re-price the customer's model files server-side at submission time, using the
    materials they assigned per object (form fields ``part_material_<file>_<object>``)
    and the multi-material / clean-supports options. Never trusts client-side numbers.
    Returns the combined estimate dict, or None when estimates are off / nothing parsed.
    Each file's stream is rewound afterwards so it can still be saved.
    """
    if not business.instant_estimate_enabled:
        return None
    from app.estimate import combined_estimate
    from app.mesh import MeshError, analyse_mesh, SUPPORTED_EXTENSIONS

    parts = []
    for i, file in enumerate(files):
        if not file or not file.filename:
            continue
        if file.filename.rsplit('.', 1)[-1].lower() not in SUPPORTED_EXTENSIONS:
            continue
        data = file.stream.read(MAX_UPLOAD_SIZE_BYTES + 1)
        file.stream.seek(0)
        if len(data) > MAX_UPLOAD_SIZE_BYTES:
            continue
        try:
            mesh = analyse_mesh(data, file.filename)
        except (MeshError, Exception):
            continue
        finally:
            del data
        for j, obj in enumerate(mesh.objects):
            parts.append({
                'name': obj.name, 'file': file.filename, 'volume_cm3': obj.volume_cm3,
                'filament_id': form.get(f'part_material_{i}_{j}', type=int),
            })
    if not parts:
        return None
    filaments = [f for f in Filament.query.order_by(Filament.name).all() if f.in_stock]
    support = Filament.query.get(business.estimate_support_filament_id) if business.estimate_support_filament_id else None
    est = combined_estimate(parts, filaments, business,
                            multi_material=form.get('multi_material') == 'on',
                            clean_supports=form.get('clean_supports') == 'on',
                            support_filament=support)
    return est if est.get('ok') else None


@public_bp.route('/order', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def order_form():
    business = get_business_settings()
    filaments = Filament.query.order_by(Filament.name).all()
    support_filament = (Filament.query.get(business.estimate_support_filament_id)
                        if business.estimate_support_filament_id else None)

    if request.method == 'POST':
        if not _turnstile_ok(business):
            flash('Please complete the verification challenge and try again.')
            return render_template_mobile_aware('public/order_form.html', business=business, filaments=filaments, support_filament=support_filament)

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
            return render_template_mobile_aware('public/order_form.html', business=business, filaments=filaments, support_filament=support_filament)

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

        uploaded_models = request.files.getlist('model_files') if model_source == 'has_model' else []
        print_estimate = _order_print_estimate(uploaded_models, request.form, business)
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
            'multi_material': request.form.get('multi_material') == 'on',
            'clean_supports': request.form.get('clean_supports') == 'on',
        }
        if print_estimate:
            order_details['estimate'] = print_estimate

        new_request = Request(
            request_number=request_number,
            reference_number=generate_reference_number(),
            client_id=client.id,
            status='New',
            notify_me=notify_me,
            model_source=model_source,
            order_details=json.dumps(order_details),
        )
        db.session.add(new_request)
        db.session.commit()

        try:
            default_subject = f'New order request — {request_number}'
            summary_html_lines = [
                f'Type: {"Needs design" if model_source == "need_design" else "Has model"}',
            ]
            if order_details.get('description'):
                summary_html_lines.append(f'Description: {order_details["description"]}')
            if materials:
                summary_html_lines.append(f'Materials: {", ".join(materials)}')
            if order_details['multi_material']:
                summary_html_lines.append('Multi-material print')
            if order_details['clean_supports']:
                summary_html_lines.append('Clean supports')
            if print_estimate:
                summary_html_lines.append(f'Instant estimate: {describe_combined(print_estimate)}')
            context = {
                'business_name': business.name or '', 'contact_name': name,
                'contact_email': email, 'document_number': request_number,
                'summary': ' \u00b7 '.join(summary_html_lines),
                'dashboard_link': url_for('main.request_detail', request_id=new_request.id, _external=True),
            }
            subject = render_email_template(business.order_notification_email_subject, context) or default_subject
            custom_html = render_email_template(business.order_notification_email_body_html, context, escape_html=True)
            html_body = custom_html or default_email_html('order_notification', context, business)
            body_text = html_to_text(html_body)
            notify_admin_new_submission(business, subject=subject, body_text=body_text, html_body=html_body)
        except EmailNotConfiguredError:
            pass
        except Exception:
            pass

        _send_order_confirmation(business, new_request, client, order_details)
        return render_template_mobile_aware('public/order_confirmation.html', business=business, order=new_request)

    return render_template_mobile_aware('public/order_form.html', business=business, filaments=filaments, support_filament=support_filament)


@public_bp.route('/order/estimate', methods=['POST'])
@limiter.limit('10 per minute')
def order_estimate():
    """Instant rough estimate for one model file (JSON). Analysed in memory and
    discarded — nothing from here is ever stored. 404 unless enabled in Settings.
    """
    from app.estimate import estimate_for_mesh
    from app.mesh import MeshError, analyse_mesh

    business = get_business_settings()
    if not business.instant_estimate_enabled:
        abort(404)

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify(ok=False, error='Please choose a model file.'), 400
    data = file.stream.read(MAX_UPLOAD_SIZE_BYTES + 1)
    if len(data) > MAX_UPLOAD_SIZE_BYTES:
        return jsonify(ok=False, error=f'That file is too large to estimate '
                                       f'({MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB max).'), 413
    try:
        mesh = analyse_mesh(data, file.filename)
    except MeshError as exc:
        return jsonify(ok=False, error=str(exc)), 422
    except Exception:
        return jsonify(ok=False, error='We couldn\u2019t read this file automatically.'), 422
    finally:
        del data

    filaments = [f for f in Filament.query.order_by(Filament.name).all() if f.in_stock]
    filament_id = request.form.get('filament_id', type=int)
    if filament_id:
        filaments = [f for f in filaments if f.id == filament_id]
    result = estimate_for_mesh(mesh, filaments, business)
    result['objects'] = [{
        'name': o.name, 'volume_cm3': round(o.volume_cm3, 3),
        'dimensions_mm': {'x': round(o.size_x_mm, 1), 'y': round(o.size_y_mm, 1), 'z': round(o.size_z_mm, 1)},
    } for o in mesh.objects[:50]]
    return jsonify(result)


def _send_order_confirmation(business, order, client, order_details):
    """Email the customer that their order arrived (reference number, what they sent,
    and the rough estimate if there was one). Best-effort: a missing or broken SMTP
    setup never blocks the order."""
    if not client or not client.email:
        return
    est = order_details.get('estimate')
    files = [f.split('_', 1)[-1] for f in order_details.get('model_files') or []]
    summary = []
    if files:
        summary.append('Files: ' + ', '.join(files))
    elif order_details.get('model_source') == 'need_design':
        summary.append('Design request')
    if order_details.get('multi_material'):
        summary.append('Multi-material print')
    if order_details.get('clean_supports'):
        summary.append('Clean supports')
    context = {
        'client_name': client.name or '', 'business_name': business.name or '',
        'document_number': order.client_number, 'summary': ' · '.join(summary),
        'estimate': describe_combined(est),
        'track_link': url_for('public.track_order', _external=True),
    }
    try:
        subject = (render_email_template(business.order_confirmation_email_subject, context)
                   or f'We\u2019ve received your order {order.client_number}')
        custom_html = render_email_template(business.order_confirmation_email_body_html, context, escape_html=True)
        if custom_html:
            html_body = custom_html
        else:
            context['estimate_lines'] = [(l['name'], l['filament'], l['grams']) for l in (est or {}).get('lines', [])]
            if est and est.get('support'):
                context['estimate_lines'].append(('Clean supports', est['support']['filament'], est['support']['grams']))
            html_body = default_email_html('order_confirmation', context, business)
        send_plain_email(business, client.email, subject, html_to_text(html_body), html_body=html_body)
    except EmailNotConfiguredError:
        pass
    except Exception:
        pass


@public_bp.route('/order/estimate/total', methods=['POST'])
@limiter.limit('60 per minute')
def order_estimate_total():
    """Live combined price for the order form: per-object materials plus the
    multi-material / clean-supports options. Volumes come from the browser (they
    were returned by /order/estimate) and are only used for this display — the
    estimate stored with an order is recomputed from the uploaded files."""
    from app.estimate import combined_estimate

    business = get_business_settings()
    if not business.instant_estimate_enabled:
        abort(404)
    payload = request.get_json(silent=True) or {}
    raw_parts = payload.get('parts')
    if not isinstance(raw_parts, list):
        return jsonify(ok=False, error='No parts given.'), 400
    parts = []
    for p in raw_parts[:50]:
        if isinstance(p, dict):
            parts.append({'name': p.get('name'), 'volume_cm3': p.get('volume_cm3'),
                          'filament_id': p.get('filament_id') if isinstance(p.get('filament_id'), int) else None})
    filaments = [f for f in Filament.query.order_by(Filament.name).all() if f.in_stock]
    support = Filament.query.get(business.estimate_support_filament_id) if business.estimate_support_filament_id else None
    return jsonify(combined_estimate(parts, filaments, business,
                                     multi_material=bool(payload.get('multi_material')),
                                     clean_supports=bool(payload.get('clean_supports')),
                                     support_filament=support))


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

    # Deposits & part payments: let the client choose (or see what's already been
    # paid) before going to Stripe. Invoices with neither redirect straight away.
    option = request.args.get('option')
    if option not in ('deposit', 'full') and payment_choice_needed(invoice):
        return render_template('pay_invoice.html', invoice=invoice, business=business, choose=True)
    charge, charge_kind = checkout_amount(invoice, option)
    if charge_kind == 'deposit':
        charge_label = f'Deposit \u2014 invoice {invoice.client_number}'
    elif invoice.amount_paid > 0:
        charge_label = f'Balance of invoice {invoice.client_number}'
    else:
        charge_label = f'Invoice {invoice.client_number}'

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
            'unit_amount': round(charge * 100),
            'product_data': {'name': charge_label},
        },
        'quantity': 1,
    }]
    if stripe_surcharge_pct:
        surcharge_amount = round(charge * stripe_surcharge_pct / 100, 2)
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
            # The webhook records exactly this (surcharge excluded) as a Payment.
            metadata={'invoice_amount': f'{charge:.2f}', 'payment_kind': charge_kind},
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
        was_paid = invoice.status == 'Paid' if invoice else True
        payment, created = record_stripe_checkout(invoice, session) if invoice else (None, False)
        if created and not was_paid and invoice.status != 'Paid':
            notify_part_payment(business, invoice, payment)  # deposit / part payment
        if created and not was_paid and invoice.status == 'Paid':
            try:
                client_name = invoice.client.name if invoice.client else 'A client'
                default_subject = f'Invoice {invoice.display_number} paid \u2014 ${invoice.total:,.2f}'
                context = {
                    'business_name': business.name or '', 'client_name': client_name,
                    'document_number': invoice.display_number, 'total': f'{invoice.total:,.2f}',
                    'paid_at': invoice.paid_at.strftime('%d %B %Y, %I:%M %p UTC'),
                    'dashboard_link': url_for('main.invoice_detail', invoice_id=invoice.id, _external=True),
                }
                subject = render_email_template(business.invoice_paid_notification_email_subject, context) or default_subject
                custom_html = render_email_template(business.invoice_paid_notification_email_body_html, context, escape_html=True)
                html_body = custom_html or default_email_html('invoice_paid_notification', context, business)
                body_text = html_to_text(html_body)
                notify_admin_new_submission(business, subject=subject, body_text=body_text, html_body=html_body)
            except Exception:
                # Best-effort notification only \u2014 the invoice is already marked paid above,
                # so a broken SMTP config must never turn into a 500 back to Stripe (which
                # would make Stripe retry the webhook and could send a duplicate email later).
                pass

    return '', 200
