"""Customer portal — /account.

Customers sign in with a passwordless, single-use magic link emailed to the address
on their Client record, then see their own orders, quotes, invoices and jobs.

Security model (kept deliberately separate from staff auth):
- Customers never go through Flask-Login. Staff use Flask-Login's session keys; the
  portal uses its own `portal_client_id` + `portal_nonce` keys, so a customer session
  can never satisfy `@login_required` and a staff session never satisfies
  `portal_login_required`.
- Every signed-in portal session has a server-side PortalSession row keyed by the
  SHA-256 of a random nonce. Signing out revokes the row, so a copied cookie stops
  working too. Sessions last 12 hours, or 30 days with "keep me signed in" (carried
  by a separate signed, HttpOnly cookie).
- Sign-in links: 256-bit random token, only its SHA-256 stored, 30-minute expiry,
  consumed atomically on first use. The emailed link opens a confirmation page and
  the token is only spent on its POST, so mail scanners that prefetch links can't
  burn it.
- The sign-in form always answers with the same message whether or not the email
  matches a client (no account enumeration), sends mail off the request thread so
  response timing doesn't reveal it either, and is rate limited per IP and per email.
- Every query is scoped to the signed-in customer's client ids; anything else 404s.
  Draft and archived records are never shown, and only client-facing fields are
  rendered (never internal_notes, markup, costs, or audit data).
"""
import hashlib
import logging
import secrets
import threading
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace

from flask import (
    Blueprint, abort, after_this_request, current_app, flash, g, redirect,
    render_template, request, send_file, session, url_for,
)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, or_

from app import db, limiter
from app.helpers import (
    get_business_settings, render_email_template, default_email_html, html_to_text,
    _build_email_message, _smtp_send,
)
from app.models import Client, Invoice, Job, PortalLoginToken, PortalSession, Quote, Request

log = logging.getLogger(__name__)

portal_bp = Blueprint('portal', __name__, url_prefix='/account')

TOKEN_TTL = timedelta(minutes=30)
SESSION_TTL = timedelta(hours=12)
REMEMBER_TTL = timedelta(days=30)
# Silent per-client cap on sign-in emails (on top of the IP/email rate limits), so
# someone can't flood a customer's inbox. Over the cap we still show the same
# "check your email" message.
MAX_LINKS_PER_WINDOW = 3
LINK_WINDOW = timedelta(minutes=15)
REMEMBER_COOKIE = 'portal_remember'
SESSION_KEYS = ('portal_client_id', 'portal_nonce')

# Customer-facing progress ladder — same steps as the public /track page.
PROGRESS_STEPS = [
    ('Received', ['Received']),
    ('Reviewing', ['Being Reviewed']),
    ('Quoted', ['Quote Sent']),
    ('Accepted', ['Accepted', 'Invoiced']),
    ('Printing', ['Queued', 'Printing', 'Post-Processing']),
    ('Complete', ['Complete', 'Paid']),
]


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _normalize_email(value):
    return (value or '').strip().lower()


def _now():
    return datetime.utcnow()


# --- Session handling ----------------------------------------------------------------

def _remember_serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='portal-remember')


def _clear_browser_state(clear_remember_cookie=True):
    for key in SESSION_KEYS:
        session.pop(key, None)
    g.portal_client = None
    if clear_remember_cookie and request.cookies.get(REMEMBER_COOKIE):
        @after_this_request
        def _drop_cookie(response):
            response.delete_cookie(REMEMBER_COOKIE, path='/account')
            return response


def _credentials_from_remember_cookie():
    raw = request.cookies.get(REMEMBER_COOKIE)
    if not raw:
        return None
    try:
        data = _remember_serializer().loads(raw, max_age=int(REMEMBER_TTL.total_seconds()))
        return int(data['c']), str(data['n'])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


def _load_portal_client():
    business = get_business_settings()
    if not business.portal_enabled:
        return None

    client_id, nonce = session.get('portal_client_id'), session.get('portal_nonce')
    if not (client_id and nonce):
        creds = _credentials_from_remember_cookie()
        if not creds:
            if request.cookies.get(REMEMBER_COOKIE):
                _clear_browser_state()
            return None
        client_id, nonce = creds

    record = PortalSession.query.filter_by(nonce_hash=_hash(nonce)).first()
    if (not record or record.client_id != client_id or record.revoked_at
            or record.expires_at <= _now()):
        _clear_browser_state()
        return None

    client = db.session.get(Client, client_id)
    if not client or not _normalize_email(client.email):
        # Client deleted, or staff removed their email: access goes with it.
        _clear_browser_state()
        return None

    session['portal_client_id'] = client_id
    session['portal_nonce'] = nonce
    return client


def current_portal_client():
    """The signed-in portal customer for this request, or None. Cached per request."""
    if 'portal_client' not in g:
        g.portal_client = _load_portal_client()
    return g.portal_client


def portal_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_portal_client() is None:
            return redirect(url_for('portal.sign_in'))
        return view(*args, **kwargs)
    return wrapped


def _start_portal_session(client, remember):
    """Sign the browser in as `client`. The whole Flask session is cleared first
    (regenerated), so nothing from before sign-in — including any staff login in the
    same browser — carries over into the customer's session."""
    old_nonce = session.get('portal_nonce')
    if old_nonce:
        PortalSession.query.filter_by(nonce_hash=_hash(old_nonce)).update(
            {'revoked_at': _now()}, synchronize_session=False)
    session.clear()

    nonce = secrets.token_urlsafe(32)
    db.session.add(PortalSession(
        client_id=client.id, nonce_hash=_hash(nonce), remember=bool(remember),
        expires_at=_now() + (REMEMBER_TTL if remember else SESSION_TTL),
    ))
    db.session.commit()
    session['portal_client_id'] = client.id
    session['portal_nonce'] = nonce
    return nonce


def _scope_client_ids(client):
    """Client ids the signed-in customer may see: their own record plus any other
    client records with the same (case-insensitive) email — the order form and staff
    can end up creating duplicates for one person. Anyone with that inbox could
    already sign in as either record, so this widens nothing."""
    email = _normalize_email(client.email)
    ids = {client.id}
    if email:
        ids.update(cid for (cid,) in db.session.query(Client.id).filter(
            func.lower(func.trim(Client.email)) == email))
    return sorted(ids)


def _not_archived(model):
    return or_(model.archived.is_(None), model.archived.is_(False))


def _visible_requests(ids):
    return Request.query.filter(Request.client_id.in_(ids), Request.status != 'Archived')


def _visible_quotes(ids):
    # Draft quotes haven't been sent to the customer yet — never show them.
    return Quote.query.filter(Quote.client_id.in_(ids), Quote.status != 'Draft', _not_archived(Quote))


def _visible_invoices(ids):
    return Invoice.query.filter(Invoice.client_id.in_(ids), Invoice.status != 'Draft', _not_archived(Invoice))


def _visible_jobs(ids):
    return Job.query.filter(Job.client_id.in_(ids), _not_archived(Job))


# --- Blueprint-wide guards --------------------------------------------------------------

@portal_bp.before_request
def _portal_gate():
    if not get_business_settings().portal_enabled:
        abort(404)


@portal_bp.after_request
def _no_store(response):
    # Account pages hold personal data — keep them out of shared/back-button caches.
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    return response


@portal_bp.app_context_processor
def _inject_portal_helpers():
    return {'portal_current_client': current_portal_client}


# --- Sign-in ----------------------------------------------------------------------------

def _email_rate_key():
    return 'portal-email:' + _normalize_email(request.form.get('email'))


def _deliver_email(business, to_email, subject, body_text, html_body):
    """Send the sign-in email off the request thread, so the response takes the same
    time whether or not the address matched a client. Tests monkeypatch this."""
    if not (business.smtp_host and business.smtp_username and business.smtp_password):
        log.warning('Customer portal sign-in requested but SMTP is not configured.')
        return
    msg = _build_email_message(business, to_email, subject, body_text, html_body)
    smtp = SimpleNamespace(
        smtp_host=business.smtp_host, smtp_port=business.smtp_port,
        smtp_username=business.smtp_username, smtp_password=business.smtp_password,
    )

    def _send():
        try:
            _smtp_send(smtp, msg)
        except Exception:
            log.exception('Failed to send customer portal sign-in email')

    threading.Thread(target=_send, daemon=True).start()


def _issue_sign_in_link(email, business):
    """Email a sign-in link if `email` belongs to a client. Returns nothing either
    way, so the caller can't accidentally reveal whether it matched."""
    client = (Client.query.filter(func.lower(func.trim(Client.email)) == email)
              .order_by(Client.id).first())
    if not client:
        return

    now = _now()
    recent = PortalLoginToken.query.filter(
        PortalLoginToken.client_id == client.id,
        PortalLoginToken.created_at >= now - LINK_WINDOW,
    ).count()
    if recent >= MAX_LINKS_PER_WINDOW:
        return

    # Opportunistic housekeeping so these tables don't grow forever.
    PortalLoginToken.query.filter(PortalLoginToken.expires_at < now - timedelta(days=1)).delete(synchronize_session=False)
    PortalSession.query.filter(PortalSession.expires_at < now - timedelta(days=1)).delete(synchronize_session=False)

    raw = secrets.token_urlsafe(32)
    db.session.add(PortalLoginToken(
        client_id=client.id, token_hash=_hash(raw), expires_at=now + TOKEN_TTL,
        requested_ip=(request.remote_addr or '')[:64],
    ))
    db.session.commit()

    context = {
        'client_name': client.name or '', 'business_name': business.name or '',
        'sign_in_link': url_for('portal.verify', token=raw, _external=True),
        'expires_minutes': str(int(TOKEN_TTL.total_seconds() // 60)),
    }
    default_subject = f'Your sign-in link for {business.name or "your account"}'
    subject = render_email_template(business.portal_sign_in_email_subject, context) or default_subject
    custom_html = render_email_template(business.portal_sign_in_email_body_html, context, escape_html=True)
    html_body = custom_html or default_email_html('portal_sign_in', context, business)
    # html_to_text drops the button's href, so put the link in the plain-text part explicitly.
    body_text = f'{html_to_text(html_body)}\n\nSign-in link: {context["sign_in_link"]}'
    try:
        _deliver_email(business, client.email.strip(), ' '.join(subject.split()), body_text, html_body)
    except Exception:
        log.exception('Failed to queue customer portal sign-in email')


@portal_bp.route('/sign-in', methods=['GET', 'POST'])
@limiter.limit('5 per minute;30 per hour', methods=['POST'])
@limiter.limit('5 per hour', key_func=_email_rate_key, methods=['POST'])
def sign_in():
    from app.public import _turnstile_ok

    business = get_business_settings()
    if current_portal_client():
        return redirect(url_for('portal.overview'))

    if request.method == 'POST':
        if not _turnstile_ok(business):
            flash('Please complete the verification challenge and try again.')
            return render_template('portal/sign_in.html', business=business)

        email = _normalize_email(request.form.get('email'))
        if not email or '@' not in email or len(email) > 254:
            flash('Please enter a valid email address.')
            return render_template('portal/sign_in.html', business=business)

        _issue_sign_in_link(email, business)
        return render_template('portal/sign_in.html', business=business, sent=True)

    return render_template('portal/sign_in.html', business=business)


def _lookup_token(token):
    if not token or len(token) > 128:
        return None
    record = PortalLoginToken.query.filter_by(token_hash=_hash(token)).first()
    if not record or record.used_at or record.expires_at <= _now():
        return None
    return record


@portal_bp.route('/verify/<token>', methods=['GET', 'POST'])
@limiter.limit('20 per minute')
def verify(token):
    business = get_business_settings()
    record = _lookup_token(token)

    if request.method == 'GET':
        # Don't consume on GET: email security scanners prefetch links.
        return render_template('portal/verify.html', business=business, valid=bool(record))

    if record:
        # Atomic single use: only one request can flip used_at from NULL.
        consumed = PortalLoginToken.query.filter(
            PortalLoginToken.id == record.id,
            PortalLoginToken.used_at.is_(None),
            PortalLoginToken.expires_at > _now(),
        ).update({'used_at': _now()}, synchronize_session=False)
        db.session.commit()
        client = db.session.get(Client, record.client_id) if consumed == 1 else None
    else:
        client = None

    if not client or not _normalize_email(client.email):
        flash('That sign-in link has expired or has already been used. Request a new one below.')
        return redirect(url_for('portal.sign_in'))

    remember = request.form.get('remember') == 'on'
    nonce = _start_portal_session(client, remember)
    response = redirect(url_for('portal.overview'))
    if remember:
        response.set_cookie(
            REMEMBER_COOKIE, _remember_serializer().dumps({'c': client.id, 'n': nonce}),
            max_age=int(REMEMBER_TTL.total_seconds()), path='/account',
            secure=current_app.config.get('SESSION_COOKIE_SECURE', True),
            httponly=True, samesite='Lax',
        )
    else:
        response.delete_cookie(REMEMBER_COOKIE, path='/account')
    return response


@portal_bp.route('/sign-out', methods=['POST'])
def sign_out():
    nonce = session.get('portal_nonce')
    creds = _credentials_from_remember_cookie()
    hashes = {_hash(n) for n in (nonce, creds[1] if creds else None) if n}
    if hashes:
        PortalSession.query.filter(PortalSession.nonce_hash.in_(hashes)).update(
            {'revoked_at': _now()}, synchronize_session=False)
        db.session.commit()
    _clear_browser_state(clear_remember_cookie=False)
    response = redirect(url_for('portal.sign_in'))
    response.delete_cookie(REMEMBER_COOKIE, path='/account')
    flash("You've been signed out.")
    return response


# --- Account pages ----------------------------------------------------------------------

def _progress_index(status):
    for i, (_label, matches) in enumerate(PROGRESS_STEPS):
        if status in matches:
            return i
    return -1


def _quote_upload_url(quote):
    if quote.upload_token and quote.status == 'Sent' and not quote.signed_copy_filename:
        return url_for('public.upload_signed_quote', token=quote.upload_token)
    return None


def _invoice_pay_url(invoice, business):
    if (invoice.status not in ('Paid', 'Cancelled') and invoice.pay_token
            and business.stripe_secret_key and invoice.stripe_enabled):
        return url_for('public.pay_invoice', token=invoice.pay_token)
    return None


def _render(template, **context):
    business = get_business_settings()
    return render_template(
        template, business=business, portal_client=current_portal_client(),
        steps=PROGRESS_STEPS, progress_index=_progress_index,
        quote_upload_url=_quote_upload_url,
        invoice_pay_url=lambda inv: _invoice_pay_url(inv, business),
        **context,
    )


@portal_bp.route('/')
@portal_login_required
def overview():
    client = current_portal_client()
    ids = _scope_client_ids(client)
    orders = _visible_requests(ids).order_by(Request.created_at.desc()).all()
    quotes = _visible_quotes(ids).order_by(Quote.created_at.desc()).all()
    invoices = _visible_invoices(ids).order_by(Invoice.created_at.desc()).all()
    jobs = _visible_jobs(ids).order_by(Job.created_at.desc()).all()
    outstanding = sum((inv.total or 0) for inv in invoices if inv.status not in ('Paid', 'Cancelled'))
    return _render('portal/overview.html', orders=orders, quotes=quotes, invoices=invoices,
                   jobs=jobs, outstanding=outstanding)


@portal_bp.route('/orders/<int:order_id>')
@portal_login_required
def order_detail(order_id):
    ids = _scope_client_ids(current_portal_client())
    order = _visible_requests(ids).filter(Request.id == order_id).first_or_404()
    quote = invoices = jobs = None
    if order.quote_id:
        quote = _visible_quotes(ids).filter(Quote.id == order.quote_id).first()
        invoices = _visible_invoices(ids).filter(Invoice.quote_id == order.quote_id).order_by(Invoice.created_at).all()
        jobs = _visible_jobs(ids).filter(Job.quote_id == order.quote_id).order_by(Job.created_at).all()
    return _render('portal/order_detail.html', order=order, data=order.order_data,
                   quote=quote, invoices=invoices or [], jobs=jobs or [])


def _pdf_response(pdf, name):
    import io
    return send_file(io.BytesIO(pdf), mimetype='application/pdf', as_attachment=False,
                     download_name=f'{name}.pdf')


@portal_bp.route('/quotes/<int:quote_id>/pdf')
@portal_login_required
def quote_pdf(quote_id):
    from app.main import _quote_pdf_bytes, _safe_filename_part

    ids = _scope_client_ids(current_portal_client())
    quote = _visible_quotes(ids).filter(Quote.id == quote_id).first_or_404()
    name = _safe_filename_part(quote.client_number)
    if quote.version:
        name += f'-v{_safe_filename_part(quote.version)}'
    return _pdf_response(_quote_pdf_bytes(quote), name)


@portal_bp.route('/invoices/<int:invoice_id>/pdf')
@portal_login_required
def invoice_pdf(invoice_id):
    from app.main import _invoice_pdf_bytes, _safe_filename_part

    ids = _scope_client_ids(current_portal_client())
    invoice = _visible_invoices(ids).filter(Invoice.id == invoice_id).first_or_404()
    return _pdf_response(_invoice_pdf_bytes(invoice), _safe_filename_part(invoice.client_number))
