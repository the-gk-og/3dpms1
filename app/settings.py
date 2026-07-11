import os

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required
from werkzeug.utils import secure_filename

from app import db
from app.models import BusinessSettings
from app.helpers import get_business_settings

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@settings_bp.route('/')
@login_required
def settings():
    business = get_business_settings()
    tab = request.args.get('tab', 'business')
    return render_template('settings.html', business=business, active_tab=tab)


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

    elif tab == 'payment':
        business.pay_id = request.form.get('pay_id', '')
        business.bank_name = request.form.get('bank_name', '')
        business.bank_account_name = request.form.get('bank_account_name', '')
        business.bank_bsb = request.form.get('bank_bsb', '')
        business.bank_account_number = request.form.get('bank_account_number', '')
        business.paypal_email = request.form.get('paypal_email', '')
        business.stripe_link = request.form.get('stripe_link', '')

    elif tab == 'email':
        business.smtp_host = request.form.get('smtp_host', '')
        business.smtp_port = int(request.form.get('smtp_port', 587) or 587)
        business.smtp_username = request.form.get('smtp_username', '')
        if request.form.get('smtp_password'):
            business.smtp_password = request.form.get('smtp_password')
        business.smtp_from_email = request.form.get('smtp_from_email', '')

    db.session.commit()
    flash('Settings saved')
    return redirect(url_for('settings.settings', tab=tab))
