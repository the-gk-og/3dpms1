from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user

from app.models import User
from app.helpers import get_business_settings, verify_turnstile

auth_bp = Blueprint('auth', __name__, url_prefix='/dash/auth')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    business = get_business_settings()
    if request.method == 'POST':
        if business.turnstile_secret_key:
            token = request.form.get('cf-turnstile-response', '')
            if not verify_turnstile(business.turnstile_secret_key, token, request.remote_addr):
                flash('Please complete the verification challenge and try again.')
                return render_template('auth/login.html', business=business)

        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.check_password(request.form['password']):
            login_user(user)
            return redirect(url_for('main.index'))
        flash('Invalid credentials')
    return render_template('auth/login.html', business=business)


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))
