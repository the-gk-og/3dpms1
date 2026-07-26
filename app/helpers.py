from datetime import datetime, timedelta
import os
import re
import secrets
import smtplib
from email.message import EmailMessage

from flask import request, render_template as _flask_render_template

from app.models import BusinessSettings, Quote, Invoice, Job, Request, AuditLog, FeedbackSurvey


# Matches common mobile/tablet user agents. Deliberately conservative — false
# negatives (a mobile device gets the desktop layout) are far less harmful than
# false positives (a desktop user gets a phone-width layout), so this only matches
# well-established device signatures rather than trying to catch everything.
_MOBILE_UA_RE = re.compile(
    r'Mobi|Android|iPhone|iPod|iPad|BlackBerry|IEMobile|Opera Mini|Windows Phone',
    re.IGNORECASE,
)


def is_mobile_request():
    """True if the request's User-Agent header identifies a phone or tablet.

    This is server-side user-agent sniffing, not a viewport check — it reflects
    what device made the request, not how wide the browser window currently is.
    A ?mobile=0 / ?mobile=1 query param can force either layout, mainly useful for
    testing and for anyone on a device this misses.
    """
    override = request.args.get('mobile')
    if override == '1':
        return True
    if override == '0':
        return False
    ua = request.user_agent.string or ''
    return bool(_MOBILE_UA_RE.search(ua))


def render_template(template_name, **context):
    """Drop-in replacement for Flask's render_template that transparently swaps in
    a `<name>.mobile.html` version when one exists and the request looks like it's
    coming from a phone or tablet. Falls back to the desktop template automatically
    if no mobile variant has been created for that page yet, so pages can be
    migrated to mobile versions incrementally without breaking anything.
    """
    if is_mobile_request() and not template_name.endswith('.mobile.html'):
        mobile_name = template_name.rsplit('.html', 1)[0] + '.mobile.html'
        try:
            return _flask_render_template(mobile_name, **context)
        except Exception as e:
            # jinja2.TemplateNotFound is the expected case (no mobile version yet for
            # this page) — anything else is a real bug in the mobile template and
            # should surface normally rather than silently masking it as "not found".
            from jinja2 import TemplateNotFound
            if not isinstance(e, TemplateNotFound):
                raise
    return _flask_render_template(template_name, **context)


def get_business_settings():
    business = BusinessSettings.query.first()
    if not business:
        business = BusinessSettings(name='Your Business')
        from app import db
        db.session.add(business)
        db.session.commit()
    return business


def log_audit(action, target_type=None, target_id=None, detail=None):
    """Record an audit trail entry for a sensitive action (login, delete, settings
    change, user management, 2FA change). Best-effort — a logging failure should
    never break the request that triggered it, so errors are swallowed.
    """
    try:
        from flask import request as flask_request
        from flask_login import current_user
        from app import db

        user_id = None
        username = None
        if current_user and getattr(current_user, 'is_authenticated', False):
            user_id = current_user.id
            username = current_user.username

        entry = AuditLog(
            user_id=user_id,
            username=username,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=(detail or '')[:500],
            ip_address=flask_request.remote_addr if flask_request else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        from app import db
        db.session.rollback()


PAYMENT_METHOD_DEFS = [
    ('bank_transfer', 'Bank Transfer'),
    ('pay_id', 'Pay ID'),
    ('cash', 'Cash'),
    ('eft', 'EFT'),
    ('credit_card', 'Credit Card'),
    ('stripe', 'Stripe'),
]


def parse_payment_methods_and_surcharges(form, business):
    """Read payment-method checkboxes + per-method surcharge overrides from a submitted
    form. Returns (payment_method_str, surcharge_overrides_dict, effective_surcharge_percent).

    Different payment methods often carry different processing costs (e.g. EFT vs bank
    transfer), so each selected method can have its own surcharge %, defaulting to the
    business-wide default for that method. The effective surcharge is baked into the
    document total automatically only when a single payment method is selected — if
    several are offered, each one's surcharge is shown as an advisory note on the PDF
    instead, since which one actually applies depends on how the client pays.
    """
    selected = []
    overrides = {}
    for key, label in PAYMENT_METHOD_DEFS:
        if form.get(f'payment_{key}') == 'on':
            selected.append(label)
            raw = form.get(f'surcharge_{key}')
            try:
                overrides[label] = float(raw) if raw not in (None, '') else (getattr(business, f'surcharge_{key}', 0) or 0)
            except ValueError:
                overrides[label] = 0.0
    payment_method_str = ', '.join(selected) if selected else 'Bank Transfer'
    effective_surcharge = overrides.get(selected[0], 0.0) if len(selected) == 1 else 0.0
    return payment_method_str, overrides, effective_surcharge


def compute_marked_items(items, markup_percent):
    """Apply a markup % to each item's price (rounded per line) and return the
    (list_of_display_amounts, marked_up_subtotal). Used consistently by both the total
    calculation and the PDF renderer so the numbers always reconcile exactly.
    """
    mult = 1 + (markup_percent or 0) / 100
    amounts = [round(item.unit_price * mult, 2) for item in items]
    return amounts, round(sum(amounts), 2)


def calculate_line_price(item_type, filament, weight_g, hardware_cost, print_time_hours,
                          hourly_rate=0, quantity=1, rate=0):
    if item_type == 'flat':
        return round((quantity or 0) * (rate or 0), 2)
    if item_type == 'hourly':
        return round((quantity or 0) * (rate or 0), 2)
    material = 0.0
    if filament:
        material = (weight_g / 1000.0) * filament.sell_price_per_kg
    time_cost = print_time_hours * hourly_rate
    return round(material + hardware_cost + time_cost, 2)


def recalculate_quote_total(quote):
    _, marked_subtotal = compute_marked_items(quote.items, quote.markup_percent)
    quote.total = round(marked_subtotal * (1 + (quote.surcharge_percent or 0) / 100), 2)
    return quote.total


def recalculate_invoice_total(invoice):
    _, marked_subtotal = compute_marked_items(invoice.items, invoice.markup_percent)
    invoice.total = round(marked_subtotal * (1 + (invoice.surcharge_percent or 0) / 100), 2)
    return invoice.total


def generate_quote_number():
    year = datetime.utcnow().year
    count = Quote.query.filter(Quote.quote_number.like(f'Q-{year}-%')).count() + 1
    return f'Q-{year}-{count:04d}'


def generate_invoice_number():
    year = datetime.utcnow().year
    count = Invoice.query.filter(Invoice.invoice_number.like(f'INV-{year}-%')).count() + 1
    return f'INV-{year}-{count:04d}'


def generate_job_number():
    year = datetime.utcnow().year
    count = Job.query.filter(Job.job_number.like(f'JOB-{year}-%')).count() + 1
    return f'JOB-{year}-{count:04d}'


def generate_request_number():
    year = datetime.utcnow().year
    count = Request.query.filter(Request.request_number.like(f'REQ-{year}-%')).count() + 1
    return f'REQ-{year}-{count:04d}'


def default_due_date(days=14):
    return (datetime.utcnow() + timedelta(days=days)).date()


def copy_quote_items_to_invoice(quote, invoice):
    from app.models import InvoiceItem
    for item in quote.items:
        invoice.items.append(InvoiceItem(
            description=item.description,
            item_type=item.item_type,
            weight_g=item.weight_g,
            filament_id=item.filament_id,
            hardware_cost=item.hardware_cost,
            print_time_hours=item.print_time_hours,
            quantity=item.quantity,
            rate=item.rate,
            unit_price=item.unit_price,
        ))
    invoice.total = quote.total
    invoice.surcharge_percent = quote.surcharge_percent
    invoice.surcharge_overrides = quote.surcharge_overrides
    invoice.markup_percent = quote.markup_percent
    invoice.show_markup_to_client = quote.show_markup_to_client
    invoice.payment_method = quote.payment_method
    invoice.notes = quote.notes
    invoice.due_date = default_due_date()


def generate_upload_token():
    """A 256-bit (32-byte / 64 hex char) random token used for the signed-quote upload link."""
    return secrets.token_hex(32)


def render_email_template(template_str, context, escape_html=False):
    """Simple {{placeholder}} substitution — deliberately not a full template engine,
    since this content is rendered from business-authored settings rather than trusted
    application code, and plain substitution avoids any template-injection risk.

    Pass escape_html=True when the result becomes an HTML email body (as opposed to a
    plain-text subject line) — context values like client_name originate from the
    public, unauthenticated order form, and without escaping, a name containing HTML
    would be interpreted as real markup by the recipient's email client rather than
    shown as literal text. Left off by default since escaping would be wrong for a
    plain-text subject line (a literal '&' would incorrectly show as '&amp;').
    """
    if not template_str:
        return None
    result = template_str
    for key, value in context.items():
        value_str = str(value)
        if escape_html:
            from html import escape as _html_escape
            value_str = _html_escape(value_str)
        result = result.replace('{{' + key + '}}', value_str)
    return result


def html_to_text(html):
    """Rough plain-text fallback for an HTML email body, for clients that don't render HTML."""
    text = re.sub(r'<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def verify_turnstile(secret_key, token, remote_ip=None):
    """Verify a Cloudflare Turnstile token server-side. Returns True/False — fails
    closed (False) on any network or parsing error rather than raising, so a broken
    connection to Cloudflare blocks the submission instead of silently letting it through.
    """
    if not secret_key or not token:
        return False
    from urllib import request as urllib_request, parse as urllib_parse, error as urllib_error
    import json as _json

    data = {'secret': secret_key, 'response': token}
    if remote_ip:
        data['remoteip'] = remote_ip
    try:
        req = urllib_request.Request(
            'https://challenges.cloudflare.com/turnstile/v0/siteverify',
            data=urllib_parse.urlencode(data).encode('utf-8'),
            method='POST',
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            result = _json.loads(resp.read().decode('utf-8'))
        return bool(result.get('success'))
    except (urllib_error.URLError, ValueError, OSError):
        return False


class EmailNotConfiguredError(Exception):
    """Raised when a business has not set up SMTP details."""


def _smtp_send(business, msg):
    if not business.smtp_host or not business.smtp_username or not business.smtp_password:
        raise EmailNotConfiguredError(
            'Email is not configured yet. Add your SMTP details under Settings → Email.'
        )
    port = business.smtp_port or 587
    with smtplib.SMTP(business.smtp_host, port, timeout=20) as server:
        server.ehlo()
        if port != 465:
            server.starttls()
            server.ehlo()
        server.login(business.smtp_username, business.smtp_password)
        server.send_message(msg)


def _build_email_message(business, to_email, subject, body_text, html_body=None):
    if not to_email:
        raise EmailNotConfiguredError('No recipient email address on file.')
    from_email = business.smtp_from_email or business.smtp_username
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f'{business.name} <{from_email}>' if business.name else from_email
    msg['To'] = to_email
    msg.set_content(body_text)
    if html_body:
        msg.add_alternative(html_body, subtype='html')
    return msg


def send_document_email(business, to_email, subject, body_text, pdf_bytes, filename, html_body=None):
    """Send an email with a PDF attachment using the business's configured SMTP server.

    If html_body is given, the email is sent as multipart/alternative (plain text +
    HTML) so it renders nicely in HTML-capable clients while still degrading to the
    plain-text version elsewhere. Raises EmailNotConfiguredError if SMTP hasn't been
    set up, or smtplib exceptions if the send itself fails (bad credentials,
    unreachable host, etc.) so callers can show the user an accurate error instead of
    a false "sent" message.
    """
    msg = _build_email_message(business, to_email, subject, body_text, html_body)
    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=filename)
    _smtp_send(business, msg)


def send_plain_email(business, to_email, subject, body_text, html_body=None):
    """Send a plain notification email (no attachment) — used for order-status
    notifications and the public contact form.
    """
    msg = _build_email_message(business, to_email, subject, body_text, html_body)
    _smtp_send(business, msg)


def notify_admin_new_submission(business, subject, body_text, html_body=None):
    """Email the business owner about a new public submission (order or enquiry).
    Best-effort — the caller should swallow failures so a broken SMTP config never
    blocks the customer's submission from completing.
    """
    target = business.contact_email or business.smtp_from_email
    if not target:
        return False
    send_plain_email(business, target, subject, body_text, html_body=html_body)
    return True


# --- File upload storage -----------------------------------------------------------

ALLOWED_SIGNED_COPY_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}
ALLOWED_ORDER_FILE_EXTENSIONS = {
    'stl', '3mf', 'obj', 'step', 'stp', 'gcode', 'zip',
    'png', 'jpg', 'jpeg', 'pdf',
}
MAX_UPLOAD_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB


def _uploads_subdir(name):
    from flask import current_app
    path = os.path.join(current_app.root_path, 'uploads', name)
    os.makedirs(path, exist_ok=True)
    return path


def signed_uploads_dir():
    return _uploads_subdir('signed_quotes')


def order_uploads_dir():
    return _uploads_subdir('order_files')


# --- Job status notifications -------------------------------------------------------

def job_should_notify(job):
    """A job's customer gets notified on completion if they opted in on the order form
    (or the quote's signed-copy upload page), OR if the business manually enabled it
    on the related invoice for jobs that didn't come through the public form.
    """
    if job.notify_me:
        return True
    if job.quote_id:
        invoice = Invoice.query.filter_by(quote_id=job.quote_id).first()
        if invoice and invoice.notify_me:
            return True
    return False


def send_job_complete_notification(job, business):
    """Emails the customer that their job is finished. Idempotent — only sends once per
    job (tracked via notify_sent_at) even if the status flips back and forth later.
    """
    if job.notify_sent_at:
        return False
    if not job.client or not job.client.email:
        return False

    default_subject = f'Your order {job.display_number} is ready — {business.name or ""}'.strip()
    default_body = (
        f"Hi {job.client.name},\n\n"
        f"Good news — your order {job.display_number} ({job.title}) is complete.\n\n"
        f"Kind regards,\n{business.name or ''}"
    )
    context = {
        'client_name': job.client.name, 'business_name': business.name or '',
        'document_number': job.display_number, 'job_title': job.title or '',
    }
    subject = render_email_template(business.job_complete_email_subject, context) or default_subject
    html_body = render_email_template(business.job_complete_email_body_html, context, escape_html=True)
    body = html_to_text(html_body) if html_body else default_body

    send_plain_email(business, job.client.email, subject, body, html_body=html_body)
    from app import db
    job.notify_sent_at = datetime.utcnow()
    db.session.commit()
    return True


# --- Feedback surveys ----------------------------------------------------------------

def get_or_create_job_survey(job):
    """Returns the job's in-progress (unresponded) FeedbackSurvey if one exists, so
    re-sending the survey re-uses the same link rather than minting a new one every
    time. Once a client has responded, the next send starts a fresh survey instead.
    """
    from app import db
    existing = FeedbackSurvey.query.filter_by(job_id=job.id, responded_at=None).first()
    if existing:
        return existing
    survey = FeedbackSurvey(
        job_id=job.id, client_id=job.client_id, quote_id=job.quote_id,
        respondent_name=job.client.name if job.client else None,
        respondent_email=job.client.email if job.client else None,
    )
    db.session.add(survey)
    db.session.commit()
    return survey


def send_feedback_survey_email(job, business=None):
    """Emails the job's client a link to the public feedback survey. Raises
    ValueError if there's no client email on file, EmailNotConfiguredError if SMTP
    isn't set up. Returns the FeedbackSurvey row on success.
    """
    from flask import url_for
    from app import db

    business = business or get_business_settings()
    if not job.client or not job.client.email:
        raise ValueError('This client has no email address on file.')

    survey = get_or_create_job_survey(job)
    survey_url = url_for('public.feedback_survey', token=survey.token, _external=True)

    subject = f'How did we do? — {job.display_number} from {business.name or "us"}'
    body = (
        f"Hi {job.client.name},\n\n"
        f"We'd love to hear how your order {job.display_number} ({job.title}) went. "
        f"It only takes a minute:\n\n{survey_url}\n\n"
        f"Thanks for your time,\n{business.name or ''}"
    )
    html_body = (
        f"<p>Hi {job.client.name},</p>"
        f"<p>We'd love to hear how your order <strong>{job.display_number}</strong> "
        f"({job.title}) went. It only takes a minute.</p>"
        f'<p><a href="{survey_url}" style="display:inline-block;padding:10px 20px;'
        f'background:#2563eb;color:#fff;text-decoration:none;border-radius:6px;">'
        f"Leave feedback</a></p>"
        f"<p>Thanks for your time,<br>{business.name or ''}</p>"
    )

    send_plain_email(business, job.client.email, subject, body, html_body=html_body)
    survey.sent_at = datetime.utcnow()
    db.session.commit()
    return survey
