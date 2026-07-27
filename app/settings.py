import io
import os
import secrets as _secrets

import pyotp
import qrcode
import qrcode.image.svg
from flask import Blueprint, request, redirect, url_for, flash, current_app, session, Response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app import db
from app.models import BusinessSettings, User, AuditLog
from app.helpers import get_business_settings, log_audit, render_email_template, render_template, default_email_html

settings_bp = Blueprint('settings', __name__, url_prefix='/dash/settings')

# SVG deliberately excluded: SVG files can embed <script>, which is a stored-XSS
# vector if the file is ever opened directly rather than rendered as an <img>.
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@settings_bp.route('/')
@login_required
def settings():
    business = get_business_settings()
    tab = request.args.get('tab', 'business')
    users = User.query.order_by(User.username).all() if tab == 'users' else None
    return render_template('settings.html', business=business, active_tab=tab, users=users)


@settings_bp.route('/save', methods=['POST'])
@login_required
def save_settings():
    business = get_business_settings()
    tab = request.form.get('tab', 'business')

    if tab == 'business':
        business.name = request.form.get('name', business.name)
        business.address = request.form.get('address', '')
        business.abn = request.form.get('abn', '')
        business.contact_email = request.form.get('contact_email', '')
        business.phone = request.form.get('phone', '')
        business.website = request.form.get('website', '')
        business.hourly_rate = float(request.form.get('hourly_rate', 0) or 0)

        logo = request.files.get('logo')
        if logo and logo.filename and allowed_file(logo.filename):
            logo.stream.seek(0, os.SEEK_END)
            size = logo.stream.tell()
            logo.stream.seek(0)
            if size <= MAX_LOGO_SIZE_BYTES:
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
                os.makedirs(upload_dir, exist_ok=True)
                ext = logo.filename.rsplit('.', 1)[1].lower()
                filename = f'{_secrets.token_hex(8)}.{ext}'
                logo.save(os.path.join(upload_dir, filename))
                business.logo_path = filename
            else:
                flash('Logo file is too large (5MB max) — settings saved without updating it.')

    elif tab == 'templates':
        business.quote_header = request.form.get('quote_header', '')
        business.quote_footer = request.form.get('quote_footer', '')
        business.invoice_header = request.form.get('invoice_header', '')
        business.invoice_footer = request.form.get('invoice_footer', '')
        business.payment_terms = request.form.get('payment_terms', '')
        business.terms_of_service = request.form.get('terms_of_service', '')
        business.payment_terms_font_size = float(request.form.get('payment_terms_font_size', 9) or 9)
        business.tos_font_size = float(request.form.get('tos_font_size', 8) or 8)

    elif tab == 'payment':
        business.pay_id = request.form.get('pay_id', '')
        business.bank_name = request.form.get('bank_name', '')
        business.bank_account_name = request.form.get('bank_account_name', '')
        business.bank_bsb = request.form.get('bank_bsb', '')
        business.bank_account_number = request.form.get('bank_account_number', '')
        business.paypal_email = request.form.get('paypal_email', '')
        business.stripe_link = request.form.get('stripe_link', '')
        if request.form.get('stripe_secret_key'):
            business.stripe_secret_key = request.form.get('stripe_secret_key').strip()
        if request.form.get('stripe_webhook_secret'):
            business.stripe_webhook_secret = request.form.get('stripe_webhook_secret').strip()
        business.surcharge_bank_transfer = float(request.form.get('surcharge_bank_transfer', 0) or 0)
        business.surcharge_pay_id = float(request.form.get('surcharge_pay_id', 0) or 0)
        business.surcharge_cash = float(request.form.get('surcharge_cash', 0) or 0)
        business.surcharge_eft = float(request.form.get('surcharge_eft', 0) or 0)
        business.surcharge_credit_card = float(request.form.get('surcharge_credit_card', 0) or 0)
        business.surcharge_stripe = float(request.form.get('surcharge_stripe', 0) or 0)

    elif tab == 'email':
        business.smtp_host = request.form.get('smtp_host', '')
        business.smtp_port = int(request.form.get('smtp_port', 587) or 587)
        business.smtp_username = request.form.get('smtp_username', '')
        if request.form.get('smtp_password'):
            business.smtp_password = request.form.get('smtp_password')
        business.smtp_from_email = request.form.get('smtp_from_email', '')
        business.quote_email_subject = request.form.get('quote_email_subject', '')
        business.quote_email_body_html = request.form.get('quote_email_body_html', '')
        business.invoice_email_subject = request.form.get('invoice_email_subject', '')
        business.invoice_email_body_html = request.form.get('invoice_email_body_html', '')
        business.job_complete_email_subject = request.form.get('job_complete_email_subject', '')
        business.job_complete_email_body_html = request.form.get('job_complete_email_body_html', '')
        business.overdue_reminder_email_subject = request.form.get('overdue_reminder_email_subject', '')
        business.overdue_reminder_email_body_html = request.form.get('overdue_reminder_email_body_html', '')
        business.contact_notification_email_subject = request.form.get('contact_notification_email_subject', '')
        business.contact_notification_email_body_html = request.form.get('contact_notification_email_body_html', '')
        business.order_notification_email_subject = request.form.get('order_notification_email_subject', '')
        business.order_notification_email_body_html = request.form.get('order_notification_email_body_html', '')
        business.invoice_paid_notification_email_subject = request.form.get('invoice_paid_notification_email_subject', '')
        business.invoice_paid_notification_email_body_html = request.form.get('invoice_paid_notification_email_body_html', '')

    elif tab == 'security':
        business.turnstile_site_key = request.form.get('turnstile_site_key', '').strip()
        business.turnstile_secret_key = request.form.get('turnstile_secret_key', '').strip()
        business.google_oauth_client_id = request.form.get('google_oauth_client_id', '').strip()
        if request.form.get('google_oauth_client_secret'):
            business.google_oauth_client_secret = request.form.get('google_oauth_client_secret').strip()

    db.session.commit()
    log_audit('settings_updated', target_type='business_settings', detail=f'tab={tab}')
    flash('Settings saved')
    return redirect(url_for('settings.settings', tab=tab))


@settings_bp.route('/users/new', methods=['POST'])
@login_required
def add_user():
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')

    if not username or not email or not password:
        flash('Please fill in username, email, and password.')
        return redirect(url_for('settings.settings', tab='users'))
    if len(password) < 8:
        flash('Password must be at least 8 characters.')
        return redirect(url_for('settings.settings', tab='users'))
    if User.query.filter((User.username == username) | (User.email == email)).first():
        flash('A user with that username or email already exists.')
        return redirect(url_for('settings.settings', tab='users'))

    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    log_audit('user_created', target_type='user', target_id=user.id, detail=username)
    flash(f'User "{username}" created.')
    return redirect(url_for('settings.settings', tab='users'))


@settings_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user(user_id):
    if user_id == current_user.id:
        flash("You can't delete the account you're currently logged in as.")
        return redirect(url_for('settings.settings', tab='users'))
    if User.query.count() <= 1:
        flash('At least one user account must remain.')
        return redirect(url_for('settings.settings', tab='users'))

    user = User.query.get_or_404(user_id)
    deleted_username = user.username
    db.session.delete(user)
    db.session.commit()
    log_audit('user_deleted', target_type='user', target_id=user_id, detail=deleted_username)
    flash('User removed.')
    return redirect(url_for('settings.settings', tab='users'))


# --- Two-factor authentication (per-user TOTP) -----------------------------------

@settings_bp.route('/2fa/setup', methods=['GET', 'POST'])
@login_required
def setup_2fa():
    if current_user.two_factor_enabled:
        flash('Two-factor authentication is already enabled on your account.')
        return redirect(url_for('settings.settings', tab='users'))

    if request.method == 'POST':
        pending_secret = session.get('pending_totp_secret')
        code = (request.form.get('code') or '').strip().replace(' ', '')
        if not pending_secret:
            flash('Setup session expired — start again.')
            return redirect(url_for('settings.setup_2fa'))
        if pyotp.TOTP(pending_secret).verify(code, valid_window=1):
            current_user.totp_secret = pending_secret
            current_user.two_factor_enabled = True
            db.session.commit()
            session.pop('pending_totp_secret', None)
            log_audit('2fa_enabled', target_type='user', target_id=current_user.id)
            flash('Two-factor authentication is now enabled on your account.')
            return redirect(url_for('settings.settings', tab='users'))
        flash('That code didn\u2019t match — please try again.')

    secret = session.get('pending_totp_secret')
    if not secret:
        secret = pyotp.random_base32()
        session['pending_totp_secret'] = secret

    business = get_business_settings()
    issuer = business.name or '3DPMS'
    uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.email, issuer_name=issuer)
    return render_template('settings_2fa_setup.html', secret=secret, otpauth_uri=uri)


@settings_bp.route('/2fa/qr.svg')
@login_required
def totp_qr_svg():
    secret = session.get('pending_totp_secret')
    if not secret:
        return Response(status=404)
    business = get_business_settings()
    uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.email, issuer_name=business.name or '3DPMS')
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), mimetype='image/svg+xml')


@settings_bp.route('/2fa/disable', methods=['POST'])
@login_required
def disable_2fa():
    password = request.form.get('password', '')
    if not current_user.check_password(password):
        flash('Incorrect password — two-factor authentication was not disabled.')
        return redirect(url_for('settings.settings', tab='users'))
    current_user.two_factor_enabled = False
    current_user.totp_secret = None
    db.session.commit()
    log_audit('2fa_disabled', target_type='user', target_id=current_user.id)
    flash('Two-factor authentication has been disabled on your account.')
    return redirect(url_for('settings.settings', tab='users'))


# --- Audit log ---------------------------------------------------------------------

@settings_bp.route('/audit-log')
@login_required
def audit_log():
    page = request.args.get('page', 1, type=int)
    pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=50, error_out=False
    )
    return render_template('settings_audit_log.html', pagination=pagination)


# --- Email template preview ---------------------------------------------------------

# One sample context per template type, matching exactly what each real send site
# builds (see generate_quote_email, generate_invoice_email, send_job_complete_notification,
# send-overdue-reminders CLI command, and the three notify_admin_new_submission call
# sites in app/public.py). Keeping these in sync with the real contexts means a
# preview always shows genuinely available placeholders — nothing that would render
# blank in production.
EMAIL_PREVIEW_SAMPLES = {
    'quote': {
        'label': 'Quote Email',
        'default_subject': 'Quote {{document_number}} from {{business_name}}',
        'context': {
            'client_name': 'Jamie Smith', 'business_name': '{business_name}',
            'document_number': 'Q-2026-0042', 'total': '184.50',
            'valid_until': '15 August 2026',
            'upload_link': 'https://example.com/q/sample-token/upload',
        },
    },
    'invoice': {
        'label': 'Invoice Email',
        'default_subject': 'Invoice {{document_number}} from {{business_name}}',
        'context': {
            'client_name': 'Jamie Smith', 'business_name': '{business_name}',
            'document_number': 'INV-2026-0091', 'total': '184.50',
            'due_date': '07 August 2026',
            'pay_link': 'https://example.com/pay/sample-token',
        },
    },
    'job_complete': {
        'label': 'Job Complete Notification',
        'default_subject': 'Your order {{document_number}} is ready — {{business_name}}',
        'context': {
            'client_name': 'Jamie Smith', 'business_name': '{business_name}',
            'document_number': 'JOB-2026-0033', 'job_title': 'Articulated dragon miniature',
        },
    },
    'overdue_reminder': {
        'label': 'Overdue Reminder',
        'default_subject': 'Overdue: Invoice {{document_number}} from {{business_name}}',
        'context': {
            'client_name': 'Jamie Smith', 'business_name': '{business_name}',
            'document_number': 'INV-2026-0091', 'total': '184.50',
            'due_date': '07 August 2026', 'days_overdue': '5',
            'pay_link': 'https://example.com/pay/sample-token',
        },
    },
    'contact_notification': {
        'label': 'Contact Form Notification (to you)',
        'default_subject': 'New contact form message from {{contact_name}}',
        'context': {
            'business_name': '{business_name}', 'contact_name': 'Jamie Smith',
            'contact_email': 'jamie@example.com',
            'message': 'Hi, I\u2019m interested in getting a custom miniature printed \u2014 could you give me a quote?',
        },
    },
    'order_notification': {
        'label': 'New Order Request Notification (to you)',
        'default_subject': 'New order request \u2014 {{document_number}}',
        'context': {
            'business_name': '{business_name}', 'contact_name': 'Jamie Smith',
            'contact_email': 'jamie@example.com', 'document_number': 'REQ-2026-0017',
            'summary': 'Type: Has model \u00b7 Materials: PLA, PETG',
            'dashboard_link': 'https://example.com/dash/requests/17',
        },
    },
    'invoice_paid_notification': {
        'label': 'Invoice Paid Notification (to you)',
        'default_subject': 'Invoice {{document_number}} paid \u2014 ${{total}}',
        'context': {
            'business_name': '{business_name}', 'client_name': 'Jamie Smith',
            'document_number': 'INV-2026-0091', 'total': '184.50',
            'paid_at': '24 July 2026, 09:14 AM UTC',
            'dashboard_link': 'https://example.com/dash/invoices/91',
        },
    },
}


@settings_bp.route('/email-preview/<template_key>', methods=['GET', 'POST'])
@login_required
def email_preview(template_key):
    """Renders a template with realistic sample data so you can see what it looks
    like before saving. POST is used from the settings form so the *unsaved* textarea
    content previews live; GET falls back to whatever's already saved, for opening the
    preview link directly.
    """
    if template_key not in EMAIL_PREVIEW_SAMPLES:
        return Response('Unknown template.', status=404)

    business = get_business_settings()
    sample = EMAIL_PREVIEW_SAMPLES[template_key]
    context = dict(sample['context'])
    context['business_name'] = business.name or 'Your Business'

    if request.method == 'POST':
        subject_raw = request.form.get('subject', '')
        body_raw = request.form.get('body_html', '')
    else:
        subject_raw = getattr(business, f'{template_key}_email_subject', '') or ''
        body_raw = getattr(business, f'{template_key}_email_body_html', '') or ''

    subject = render_email_template(subject_raw, context) or \
        render_email_template(sample['default_subject'], context)
    custom_html = render_email_template(body_raw, context, escape_html=True)
    html_body = custom_html or default_email_html(template_key, context, business)

    return render_template(
        'settings_email_preview.html',
        label=sample['label'], subject=subject, html_body=html_body,
        is_custom=bool(custom_html),
    )
