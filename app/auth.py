import pyotp
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user

from app import limiter
from app.models import User
from app.helpers import get_business_settings, verify_turnstile, log_audit

auth_bp = Blueprint('auth', __name__, url_prefix='/dash/auth')


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
