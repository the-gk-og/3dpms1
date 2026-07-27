import pyotp
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user

from app import limiter, oauth
from app.models import User, db
from app.helpers import get_business_settings, verify_turnstile, log_audit

auth_bp = Blueprint('auth', __name__, url_prefix='/dash/auth')


def _get_google_client(business):
    """Registers the Google OAuth client on first use each process. Client ID/secret
    live in the database (per-business setting), not env vars, so this can't happen
    at app-factory time — the settings might not exist yet, and an admin can change
    them from the dashboard without a restart. authlib caches by name internally, so
    re-registering after a credential change requires re-creating the client here.
    """
    if not business.google_oauth_client_id or not business.google_oauth_client_secret:
        return None
    existing = oauth._clients.get('google')
    if existing and getattr(existing, '_3dpms_client_id', None) == business.google_oauth_client_id:
        return existing
    client = oauth.register(
        name='google',
        client_id=business.google_oauth_client_id,
        client_secret=business.google_oauth_client_secret,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
        overwrite=True,
    )
    client._3dpms_client_id = business.google_oauth_client_id
    return client


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def login():
    business = get_business_settings()
    if request.method == 'POST':
        if business.turnstile_secret_key:
            token = request.form.get('cf-turnstile-response', '')
            if not verify_turnstile(business.turnstile_secret_key, token, request.remote_addr):
                flash('Please complete the verification challenge and try again.')
                return render_template('auth/login.html', business=business)

        username = request.form.get('username', '')
        user = User.query.filter_by(username=username).first()
        # Always hash-compare even when the username doesn't exist, so response
        # timing doesn't reveal whether a given username is registered.
        password_ok = user.check_password(request.form.get('password', '')) if user else False
        if user and password_ok:
            if user.two_factor_enabled:
                # Password is correct but a TOTP code is still required — stash the
                # user id in the (signed) session rather than logging in yet.
                session['pending_2fa_user_id'] = user.id
                return redirect(url_for('auth.verify_2fa'))
            login_user(user)
            log_audit('login_success', target_type='user', target_id=user.id)
            return redirect(url_for('main.index'))
        log_audit('login_failed', detail=f'username={username[:80]}')
        flash('Invalid credentials')
    return render_template('auth/login.html', business=business)


@auth_bp.route('/login/google')
@limiter.limit('10 per minute')
def login_google():
    business = get_business_settings()
    client = _get_google_client(business)
    if not client:
        flash('Google sign-in isn\u2019t set up for this site.')
        return redirect(url_for('auth.login'))
    redirect_uri = url_for('auth.login_google_callback', _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route('/login/google/callback')
@limiter.limit('10 per minute')
def login_google_callback():
    business = get_business_settings()
    client = _get_google_client(business)
    if not client:
        flash('Google sign-in isn\u2019t set up for this site.')
        return redirect(url_for('auth.login'))

    try:
        token = client.authorize_access_token()
        userinfo = token.get('userinfo') or client.parse_id_token(token)
    except Exception:
        log_audit('login_failed', detail='google_oauth_error')
        flash('Google sign-in failed. Please try again or use your password.')
        return redirect(url_for('auth.login'))

    google_sub = userinfo.get('sub')
    email = (userinfo.get('email') or '').strip().lower()
    email_verified = userinfo.get('email_verified', False)

    if not google_sub or not email or not email_verified:
        log_audit('login_failed', detail='google_oauth_unverified')
        flash('Your Google account email must be verified to sign in.')
        return redirect(url_for('auth.login'))

    # Never create an account here — only link/sign in an existing dashboard user.
    # This keeps the app's "admin-provisioned accounts only" model intact regardless
    # of who has a Google account.
    user = User.query.filter_by(google_sub=google_sub).first()
    if not user:
        user = User.query.filter(db.func.lower(User.email) == email).first()
        if not user:
            log_audit('login_failed', detail=f'google_no_match email={email[:80]}')
            flash('No dashboard account matches that Google email address.')
            return redirect(url_for('auth.login'))
        user.google_sub = google_sub
        db.session.commit()
        log_audit('google_account_linked', target_type='user', target_id=user.id)

    if user.two_factor_enabled:
        # Same as password login: a correct Google sign-in still isn't enough on
        # its own if 2FA is turned on — the TOTP step still has to happen.
        session['pending_2fa_user_id'] = user.id
        return redirect(url_for('auth.verify_2fa'))

    login_user(user)
    log_audit('login_success', target_type='user', target_id=user.id, detail='google_oauth')
    return redirect(url_for('main.index'))


@auth_bp.route('/google/connect')
@login_required
@limiter.limit('10 per minute')
def connect_google():
    """Self-service binding: lets an already-authenticated dashboard user link their
    own Google account, as an alternative to the automatic email-match linking that
    happens on first Google sign-in. This never creates a User — it only ever
    attaches a google_sub to the account you're already logged into.
    """
    business = get_business_settings()
    client = _get_google_client(business)
    if not client:
        flash('Google sign-in isn\u2019t set up for this site.')
        return redirect(url_for('settings.settings', tab='users'))
    redirect_uri = url_for('auth.connect_google_callback', _external=True)
    return client.authorize_redirect(redirect_uri)


@auth_bp.route('/google/connect/callback')
@login_required
@limiter.limit('10 per minute')
def connect_google_callback():
    business = get_business_settings()
    client = _get_google_client(business)
    if not client:
        flash('Google sign-in isn\u2019t set up for this site.')
        return redirect(url_for('settings.settings', tab='users'))

    try:
        token = client.authorize_access_token()
        userinfo = token.get('userinfo') or client.parse_id_token(token)
    except Exception:
        log_audit('google_account_link_failed', target_type='user', target_id=current_user.id, detail='oauth_error')
        flash('Could not connect your Google account. Please try again.')
        return redirect(url_for('settings.settings', tab='users'))

    google_sub = userinfo.get('sub')
    email = (userinfo.get('email') or '').strip().lower()
    email_verified = userinfo.get('email_verified', False)

    if not google_sub or not email or not email_verified:
        log_audit('google_account_link_failed', target_type='user', target_id=current_user.id, detail='unverified')
        flash('That Google account\u2019s email must be verified to connect it.')
        return redirect(url_for('settings.settings', tab='users'))

    existing = User.query.filter_by(google_sub=google_sub).first()
    if existing and existing.id != current_user.id:
        log_audit('google_account_link_failed', target_type='user', target_id=current_user.id, detail='already_linked_elsewhere')
        flash('That Google account is already connected to a different dashboard user.')
        return redirect(url_for('settings.settings', tab='users'))

    current_user.google_sub = google_sub
    db.session.commit()
    log_audit('google_account_linked', target_type='user', target_id=current_user.id, detail=f'manual email={email[:80]}')
    flash('Your Google account is now connected \u2014 you can sign in with it next time.')
    return redirect(url_for('settings.settings', tab='users'))


@auth_bp.route('/google/disconnect', methods=['POST'])
@login_required
def disconnect_google():
    if not current_user.google_sub:
        flash('No Google account is connected.')
        return redirect(url_for('settings.settings', tab='users'))
    current_user.google_sub = None
    db.session.commit()
    log_audit('google_account_unlinked', target_type='user', target_id=current_user.id)
    flash('Your Google account has been disconnected.')
    return redirect(url_for('settings.settings', tab='users'))


@auth_bp.route('/verify-2fa', methods=['GET', 'POST'])
@limiter.limit('10 per minute', methods=['POST'])
def verify_2fa():
    user_id = session.get('pending_2fa_user_id')
    if not user_id:
        return redirect(url_for('auth.login'))
    user = User.query.get(user_id)
    if not user or not user.two_factor_enabled:
        session.pop('pending_2fa_user_id', None)
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        code = (request.form.get('code') or '').strip().replace(' ', '')
        totp = pyotp.TOTP(user.totp_secret)
        if totp.verify(code, valid_window=1):
            session.pop('pending_2fa_user_id', None)
            login_user(user)
            log_audit('login_success', target_type='user', target_id=user.id, detail='2fa')
            return redirect(url_for('main.index'))
        log_audit('login_2fa_failed', target_type='user', target_id=user.id)
        flash('Invalid code. Try again.')
    return render_template('auth/verify_2fa.html')


@auth_bp.route('/logout')
@login_required
def logout():
    log_audit('logout', target_type='user', target_id=current_user.id)
    logout_user()
    return redirect(url_for('auth.login'))
