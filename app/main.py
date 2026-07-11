from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required

from app import db
from app.models import Client, Quote, QuoteItem, Filament, Invoice, InvoiceItem, Job, BusinessSettings
from app.helpers import (
    calculate_line_price, recalculate_quote_total, recalculate_invoice_total,
    generate_quote_number, generate_invoice_number, generate_job_number,
    copy_quote_items_to_invoice, get_business_settings, default_due_date,
    send_document_email, EmailNotConfiguredError,
)
from app.pdf_utils import build_pdf, build_payment_details

main_bp = Blueprint('main', __name__)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _item_fields_from_form(form, business):
    """Parse a line-item form into (field_dict, unit_price), handling all three item types:
    'print' (filament weight + hardware + print time), 'flat' (qty x price), and
    'hourly' (hours x rate) — e.g. design services or per-item hardware like bolts.
    """
    item_type = form.get('item_type', 'print')
    if item_type not in ('print', 'flat', 'hourly'):
        item_type = 'print'

    filament = Filament.query.get(form.get('filament_id')) if item_type == 'print' else None

    if item_type == 'print':
        weight_g = float(form.get('weight_g', 0) or 0)
        hardware_cost = float(form.get('hardware_cost', 0) or 0)
        print_time_hours = float(form.get('print_time_hours', 0) or 0)
        quantity = 1.0
        rate = 0.0
    else:
        weight_g = 0.0
        hardware_cost = 0.0
        print_time_hours = 0.0
        quantity = float(form.get('quantity', 1) or 0)
        rate = float(form.get('rate', 0) or 0)

    unit_price = calculate_line_price(
        item_type, filament, weight_g, hardware_cost, print_time_hours,
        business.hourly_rate or 0, quantity, rate,
    )

    fields = {
        'description': form.get('description') or ('Print job' if item_type == 'print' else 'Item'),
        'item_type': item_type,
        'weight_g': weight_g,
        'filament_id': filament.id if filament else None,
        'hardware_cost': hardware_cost,
        'print_time_hours': print_time_hours,
        'quantity': quantity,
        'rate': rate,
        'unit_price': unit_price,
    }
    return fields


def _quote_pdf_bytes(quote):
    business = get_business_settings()
    items = [{
        'description': item.description,
        'detail': item.detail_line,
        'line_total': item.unit_price,
    } for item in quote.items]
    logo_path = None
    if business.logo_path:
        from flask import current_app
        logo_path = current_app.root_path + '/static/uploads/' + business.logo_path

    return build_pdf(
        'Quote', business, quote.client, items, quote.total,
        business.quote_footer or '',
        header_text=business.quote_header or '',
        payment_method=quote.payment_method or '',
        payment_details=build_payment_details(business),
        payment_terms=business.payment_terms or '',
        terms_of_service=business.terms_of_service or '',
        signature_enabled=quote.digital_signature_enabled,
        document_number=quote.display_number,
        subtotal=quote.subtotal,
        surcharge_percent=quote.surcharge_percent or 0,
        notes=quote.notes or '',
        logo_path=logo_path,
        valid_until=quote.valid_until,
    )


def _invoice_pdf_bytes(invoice):
    business = get_business_settings()
    items = [{
        'description': item.description,
        'detail': item.detail_line,
        'line_total': item.unit_price,
    } for item in invoice.items]
    if not items:
        items = [{'description': 'Invoice total', 'detail': '—', 'line_total': invoice.total}]

    logo_path = None
    if business.logo_path:
        from flask import current_app
        logo_path = current_app.root_path + '/static/uploads/' + business.logo_path

    subtotal = sum(i['line_total'] for i in items)
    return build_pdf(
        'Invoice', business, invoice.client, items, invoice.total,
        business.invoice_footer or '',
        header_text=business.invoice_header or '',
        payment_method=invoice.payment_method or '',
        payment_details=build_payment_details(business),
        payment_terms=business.payment_terms or '',
        document_number=invoice.display_number,
        subtotal=subtotal,
        surcharge_percent=invoice.surcharge_percent or 0,
        notes=invoice.notes or '',
        logo_path=logo_path,
        due_date=invoice.due_date,
    )


@main_bp.route('/')
@login_required
def index():
    settings = get_business_settings()
    quotes = Quote.query.order_by(Quote.created_at.desc()).limit(10).all()
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).limit(10).all()
    clients = Client.query.order_by(Client.created_at.desc()).limit(10).all()
    filaments = Filament.query.all()
    return render_template(
        'index.html', settings=settings, quotes=quotes,
        invoices=invoices, clients=clients, filament_count=len(filaments),
    )


@main_bp.route('/quotes', methods=['GET'])
@login_required
def quotes():
    quotes = Quote.query.order_by(Quote.created_at.desc()).all()
    return render_template('quotes.html', quotes=quotes)


@main_bp.route('/quotes/new', methods=['GET', 'POST'])
@login_required
def new_quote():
    if request.method == 'POST':
        client = Client.query.get(request.form.get('client_id'))
        if not client:
            client = Client(
                name=request.form.get('client_name', 'New Client'),
                email=request.form.get('client_email', ''),
                phone=request.form.get('client_phone', ''),
            )
            db.session.add(client)
            db.session.commit()

        payment_methods = []
        for key, label in [
            ('payment_bank_transfer', 'Bank Transfer'),
            ('payment_pay_id', 'Pay ID'),
            ('payment_cash', 'Cash'),
            ('payment_eft', 'EFT'),
            ('payment_credit_card', 'Credit Card'),
            ('payment_stripe', 'Stripe'),
        ]:
            if request.form.get(key):
                payment_methods.append(label)

        quote = Quote(
            quote_number=generate_quote_number(),
            client=client,
            notes=request.form.get('notes', ''),
            payment_method=', '.join(payment_methods) if payment_methods else 'Bank Transfer',
            surcharge_percent=float(request.form.get('surcharge_percent', 0) or 0),
            digital_signature_enabled=request.form.get('digital_signature_enabled') == 'on',
            valid_until=_parse_date(request.form.get('valid_until')),
        )
        db.session.add(quote)
        db.session.commit()
        flash('Quote created')
        return redirect(url_for('main.quote_detail', quote_id=quote.id))
    clients = Client.query.order_by(Client.name).all()
    return render_template('new_quote.html', clients=clients)


@main_bp.route('/quotes/<int:quote_id>')
@login_required
def quote_detail(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    filaments = Filament.query.order_by(Filament.name).all()
    business = get_business_settings()
    existing_invoice = Invoice.query.filter_by(quote_id=quote.id).first()
    existing_job = Job.query.filter_by(quote_id=quote.id).first()
    return render_template(
        'quote_detail.html', quote=quote, filaments=filaments,
        business=business, existing_invoice=existing_invoice, existing_job=existing_job,
    )


@main_bp.route('/quotes/<int:quote_id>/edit', methods=['POST'])
@login_required
def edit_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    quote.notes = request.form.get('notes', '')
    quote.status = request.form.get('status', quote.status)
    quote.surcharge_percent = float(request.form.get('surcharge_percent', 0) or 0)
    quote.digital_signature_enabled = request.form.get('digital_signature_enabled') == 'on'
    if 'valid_until' in request.form:
        quote.valid_until = _parse_date(request.form.get('valid_until'))

    payment_methods = []
    for key, label in [
        ('payment_bank_transfer', 'Bank Transfer'),
        ('payment_pay_id', 'Pay ID'),
        ('payment_cash', 'Cash'),
        ('payment_eft', 'EFT'),
        ('payment_credit_card', 'Credit Card'),
        ('payment_stripe', 'Stripe'),
    ]:
        if request.form.get(key):
            payment_methods.append(label)
    if payment_methods:
        quote.payment_method = ', '.join(payment_methods)

    recalculate_quote_total(quote)
    db.session.commit()
    flash('Quote updated')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def delete_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    db.session.delete(quote)
    db.session.commit()
    flash('Quote deleted')
    return redirect(url_for('main.quotes'))


@main_bp.route('/quotes/<int:quote_id>/add-item', methods=['POST'])
@login_required
def add_quote_item(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    business = get_business_settings()
    fields = _item_fields_from_form(request.form, business)
    item = QuoteItem(quote_id=quote.id, **fields)
    db.session.add(item)
    recalculate_quote_total(quote)
    db.session.commit()
    flash('Item added')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/quotes/<int:quote_id>/items/<int:item_id>/edit', methods=['POST'])
@login_required
def edit_quote_item(quote_id, item_id):
    quote = Quote.query.get_or_404(quote_id)
    item = QuoteItem.query.filter_by(id=item_id, quote_id=quote.id).first_or_404()
    business = get_business_settings()
    fields = _item_fields_from_form(request.form, business)
    for key, value in fields.items():
        setattr(item, key, value)
    recalculate_quote_total(quote)
    db.session.commit()
    flash('Item updated')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/quotes/<int:quote_id>/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_quote_item(quote_id, item_id):
    quote = Quote.query.get_or_404(quote_id)
    item = QuoteItem.query.filter_by(id=item_id, quote_id=quote.id).first_or_404()
    db.session.delete(item)
    recalculate_quote_total(quote)
    db.session.commit()
    flash('Item removed')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/quotes/<int:quote_id>/pdf', methods=['POST'])
@login_required
def generate_quote_pdf(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    pdf = _quote_pdf_bytes(quote)
    return send_file(
        __import__('io').BytesIO(pdf), mimetype='application/pdf',
        as_attachment=True, download_name=f'{quote.display_number}.pdf',
    )


@main_bp.route('/quotes/<int:quote_id>/email', methods=['POST'])
@login_required
def generate_quote_email(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    business = get_business_settings()
    try:
        pdf = _quote_pdf_bytes(quote)
        body = (
            f"Hi {quote.client.name},\n\n"
            f"Please find attached your quote {quote.display_number} "
            f"for ${quote.total:,.2f}.\n\n"
            + (f"This quote is valid until {quote.valid_until.strftime('%d %B %Y')}.\n\n"
               if quote.valid_until else '')
            + f"Kind regards,\n{business.name or ''}"
        )
        send_document_email(
            business, quote.client.email,
            subject=f'Quote {quote.display_number} from {business.name or "us"}',
            body_text=body, pdf_bytes=pdf, filename=f'{quote.display_number}.pdf',
        )
        if quote.status == 'Draft':
            quote.status = 'Sent'
            db.session.commit()
        flash(f'Quote emailed to {quote.client.email}')
    except EmailNotConfiguredError as e:
        flash(str(e))
    except Exception as e:
        flash(f'Could not send email: {e}')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/quotes/<int:quote_id>/convert-to-invoice', methods=['POST'])
@login_required
def convert_quote_to_invoice(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    existing = Invoice.query.filter_by(quote_id=quote.id).first()
    if existing:
        flash('Invoice already exists for this quote.')
        return redirect(url_for('main.invoice_detail', invoice_id=existing.id))

    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        client=quote.client,
        quote_id=quote.id,
        status='Draft',
    )
    copy_quote_items_to_invoice(quote, invoice)
    db.session.add(invoice)
    quote.status = 'Invoiced'
    db.session.commit()
    flash('Invoice created from quote.')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))


@main_bp.route('/quotes/<int:quote_id>/create-job', methods=['POST'])
@login_required
def create_job_from_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    existing = Job.query.filter_by(quote_id=quote.id).first()
    if existing:
        flash('Job already exists for this quote.')
        return redirect(url_for('main.jobs'))

    title = f'Print job for {quote.client.name}'
    if quote.items:
        title = quote.items[0].description or title

    job = Job(
        job_number=generate_job_number(),
        quote_id=quote.id,
        client_id=quote.client_id,
        title=title,
        status='Queued',
        notes=quote.notes or '',
    )
    db.session.add(job)
    quote.status = 'In Production'
    db.session.commit()
    flash('Job created from quote.')
    return redirect(url_for('main.jobs'))


@main_bp.route('/invoices', methods=['GET'])
@login_required
def invoices():
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    return render_template('invoices.html', invoices=invoices)


@main_bp.route('/invoices/new', methods=['GET', 'POST'])
@login_required
def new_invoice():
    if request.method == 'POST':
        client = Client.query.get(request.form.get('client_id'))
        if not client:
            client = Client(
                name=request.form.get('client_name', 'New Client'),
                email=request.form.get('client_email', ''),
                phone=request.form.get('client_phone', ''),
            )
            db.session.add(client)
            db.session.commit()

        due_date = _parse_date(request.form.get('due_date')) or default_due_date()

        invoice = Invoice(
            invoice_number=generate_invoice_number(),
            client=client,
            notes=request.form.get('notes', ''),
            payment_method=request.form.get('payment_method') or 'Bank Transfer',
            surcharge_percent=float(request.form.get('surcharge_percent', 0) or 0),
            due_date=due_date,
            status='Draft',
        )
        db.session.add(invoice)
        db.session.commit()
        flash('Invoice created')
        return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))
    clients = Client.query.order_by(Client.name).all()
    return render_template('new_invoice.html', clients=clients, default_due_date=default_due_date())


@main_bp.route('/invoices/<int:invoice_id>')
@login_required
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    filaments = Filament.query.order_by(Filament.name).all()
    business = get_business_settings()
    return render_template('invoice_detail.html', invoice=invoice, filaments=filaments, business=business)


@main_bp.route('/invoices/<int:invoice_id>/edit', methods=['POST'])
@login_required
def edit_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    new_status = request.form.get('status', invoice.status)
    invoice.notes = request.form.get('notes', '')
    invoice.payment_method = request.form.get('payment_method', invoice.payment_method)
    invoice.surcharge_percent = float(request.form.get('surcharge_percent', 0) or 0)
    if 'due_date' in request.form:
        invoice.due_date = _parse_date(request.form.get('due_date'))

    if new_status == 'Paid' and invoice.status != 'Paid':
        invoice.paid_at = datetime.utcnow()
    elif new_status != 'Paid':
        invoice.paid_at = None
    invoice.status = new_status

    recalculate_invoice_total(invoice)
    db.session.commit()
    flash('Invoice updated')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))


@main_bp.route('/invoices/<int:invoice_id>/delete', methods=['POST'])
@login_required
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    db.session.delete(invoice)
    db.session.commit()
    flash('Invoice deleted')
    return redirect(url_for('main.invoices'))


@main_bp.route('/invoices/<int:invoice_id>/add-item', methods=['POST'])
@login_required
def add_invoice_item(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    business = get_business_settings()
    fields = _item_fields_from_form(request.form, business)
    item = InvoiceItem(invoice_id=invoice.id, **fields)
    db.session.add(item)
    recalculate_invoice_total(invoice)
    db.session.commit()
    flash('Item added')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))


@main_bp.route('/invoices/<int:invoice_id>/items/<int:item_id>/edit', methods=['POST'])
@login_required
def edit_invoice_item(invoice_id, item_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    item = InvoiceItem.query.filter_by(id=item_id, invoice_id=invoice.id).first_or_404()
    business = get_business_settings()
    fields = _item_fields_from_form(request.form, business)
    for key, value in fields.items():
        setattr(item, key, value)
    recalculate_invoice_total(invoice)
    db.session.commit()
    flash('Item updated')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))


@main_bp.route('/invoices/<int:invoice_id>/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_invoice_item(invoice_id, item_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    item = InvoiceItem.query.filter_by(id=item_id, invoice_id=invoice.id).first_or_404()
    db.session.delete(item)
    recalculate_invoice_total(invoice)
    db.session.commit()
    flash('Item removed')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))


@main_bp.route('/invoices/<int:invoice_id>/pdf', methods=['POST'])
@login_required
def generate_invoice_pdf(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    pdf = _invoice_pdf_bytes(invoice)
    return send_file(
        __import__('io').BytesIO(pdf), mimetype='application/pdf',
        as_attachment=True, download_name=f'{invoice.display_number}.pdf',
    )


@main_bp.route('/invoices/<int:invoice_id>/email', methods=['POST'])
@login_required
def generate_invoice_email(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    business = get_business_settings()
    try:
        pdf = _invoice_pdf_bytes(invoice)
        body = (
            f"Hi {invoice.client.name},\n\n"
            f"Please find attached invoice {invoice.display_number} "
            f"for ${invoice.total:,.2f}.\n\n"
            + (f"Payment is due by {invoice.due_date.strftime('%d %B %Y')}.\n\n"
               if invoice.due_date else '')
            + f"Kind regards,\n{business.name or ''}"
        )
        send_document_email(
            business, invoice.client.email,
            subject=f'Invoice {invoice.display_number} from {business.name or "us"}',
            body_text=body, pdf_bytes=pdf, filename=f'{invoice.display_number}.pdf',
        )
        if invoice.status == 'Draft':
            invoice.status = 'Sent'
            db.session.commit()
        flash(f'Invoice emailed to {invoice.client.email}')
    except EmailNotConfiguredError as e:
        flash(str(e))
    except Exception as e:
        flash(f'Could not send email: {e}')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))


@main_bp.route('/clients', methods=['GET', 'POST'])
@login_required
def clients():
    if request.method == 'POST':
        client = Client(
            name=request.form['name'],
            phone=request.form.get('phone', ''),
            email=request.form.get('email', ''),
            notes=request.form.get('notes', ''),
        )
        db.session.add(client)
        db.session.commit()
        flash('Client saved')
        return redirect(url_for('main.clients'))
    clients = Client.query.order_by(Client.name).all()
    return render_template('clients.html', clients=clients)


@main_bp.route('/clients/<int:client_id>/edit', methods=['POST'])
@login_required
def edit_client(client_id):
    client = Client.query.get_or_404(client_id)
    client.name = request.form['name']
    client.phone = request.form.get('phone', '')
    client.email = request.form.get('email', '')
    client.notes = request.form.get('notes', '')
    db.session.commit()
    flash('Client updated')
    return redirect(url_for('main.clients'))


@main_bp.route('/clients/<int:client_id>/delete', methods=['POST'])
@login_required
def delete_client(client_id):
    client = Client.query.get_or_404(client_id)
    db.session.delete(client)
    db.session.commit()
    flash('Client deleted')
    return redirect(url_for('main.clients'))


@main_bp.route('/jobs')
@login_required
def jobs():
    jobs = Job.query.order_by(Job.created_at.desc()).all()
    return render_template('jobs.html', jobs=jobs)


@main_bp.route('/jobs/<int:job_id>/edit', methods=['POST'])
@login_required
def edit_job(job_id):
    job = Job.query.get_or_404(job_id)
    job.title = request.form.get('title', job.title)
    job.status = request.form.get('status', job.status)
    job.notes = request.form.get('notes', '')
    db.session.commit()
    flash('Job updated')
    return redirect(url_for('main.jobs'))


@main_bp.route('/jobs/<int:job_id>/delete', methods=['POST'])
@login_required
def delete_job(job_id):
    job = Job.query.get_or_404(job_id)
    db.session.delete(job)
    db.session.commit()
    flash('Job deleted')
    return redirect(url_for('main.jobs'))
