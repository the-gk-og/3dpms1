import os

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app import db
from app.models import BusinessSettings, User
from app.helpers import get_business_settings

settings_bp = Blueprint('settings', __name__, url_prefix='/dash/settings')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}


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
        business.contact_email = request.form.get('contact_email', '')
        business.phone = request.form.get('phone', '')
        business.website = request.form.get('website', '')
        business.hourly_rate = float(request.form.get('hourly_rate', 0) or 0)

        logo = request.files.get('logo')
        if logo and logo.filename and allowed_file(logo.filename):
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
            filename = secure_filename(logo.filename)
            logo.save(os.path.join(upload_dir, filename))
            business.logo_path = filename

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

    elif tab == 'security':
        business.turnstile_site_key = request.form.get('turnstile_site_key', '').strip()
        business.turnstile_secret_key = request.form.get('turnstile_secret_key', '').strip()

    db.session.commit()
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
    db.session.delete(user)
    db.session.commit()
    flash('User removed.')
    return redirect(url_for('settings.settings', tab='users'))
