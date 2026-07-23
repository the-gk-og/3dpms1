from datetime import datetime
import json
import os
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, abort
from flask_login import login_required

from app import db
from app.models import Client, Quote, QuoteItem, Filament, Invoice, InvoiceItem, Job, BusinessSettings, Request
from app.helpers import (
    calculate_line_price, recalculate_quote_total, recalculate_invoice_total,
    generate_quote_number, generate_invoice_number, generate_job_number,
    copy_quote_items_to_invoice, get_business_settings, default_due_date,
    send_document_email, EmailNotConfiguredError,
    parse_payment_methods_and_surcharges, compute_marked_items,
    generate_upload_token, render_email_template, html_to_text,
    signed_uploads_dir, job_should_notify, send_job_complete_notification,
    order_uploads_dir, log_audit,
)
from app.pdf_utils import build_pdf, build_payment_details

main_bp = Blueprint('main', __name__, url_prefix='/dash')


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _safe_filename_part(value):
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', value or '').strip('-')


def _safe_join(directory, filename):
    """Join `directory` and a user-supplied `filename`, returning None if the
    resolved path would escape `directory` (e.g. via `../` segments or an absolute
    path). Prevents path traversal on the authenticated file-download routes.
    """
    directory = os.path.abspath(directory)
    candidate = os.path.abspath(os.path.join(directory, filename))
    if os.path.commonpath([directory, candidate]) != directory:
        return None
    return candidate


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


def _items_and_totals_for_pdf(doc):
    """Build the (items, subtotal, markup_percent_shown, markup_amount_shown) used for
    PDF rendering. If show_markup_to_client is off, the markup is baked invisibly into
    each line's displayed amount so the numbers still add up without revealing it. If
    it's on, items show their raw price and an explicit 'Markup' line is added.
    """
    display_amounts, marked_subtotal = compute_marked_items(doc.items, doc.markup_percent)
    show_markup = bool(doc.show_markup_to_client and doc.markup_percent)
    if show_markup:
        items = [{'description': it.description, 'detail': it.detail_line, 'line_total': it.unit_price}
                 for it in doc.items]
        raw_subtotal = sum(it.unit_price for it in doc.items)
        markup_amount = round(marked_subtotal - raw_subtotal, 2)
        subtotal = raw_subtotal
    else:
        items = [{'description': it.description, 'detail': it.detail_line, 'line_total': amt}
                 for it, amt in zip(doc.items, display_amounts)]
        subtotal = marked_subtotal
        markup_amount = 0
    return items, subtotal, (doc.markup_percent if show_markup else 0), markup_amount


def _surcharge_notes(doc):
    """When more than one payment method is offered, the surcharge isn't baked into a
    single total (since it depends on how the client ends up paying) — instead list
    each method's surcharge as an advisory note on the PDF.
    """
    methods = [m.strip() for m in (doc.payment_method or '').split(',') if m.strip()]
    if len(methods) <= 1:
        return []
    smap = doc.surcharge_map
    notes = []
    for m in methods:
        pct = smap.get(m, 0) or 0
        notes.append(f'{m}: +{pct:.1f}% surcharge applies' if pct else f'{m}: no surcharge')
    return notes


def _quote_pdf_bytes(quote):
    business = get_business_settings()
    items, subtotal, markup_percent, markup_amount = _items_and_totals_for_pdf(quote)
    logo_path = None
    if business.logo_path:
        from flask import current_app
        logo_path = current_app.root_path + '/static/uploads/' + business.logo_path

    document_number = quote.display_number
    if quote.version:
        document_number = f'{document_number} · v{quote.version}'

    return build_pdf(
        'Quote', business, quote.client, items, quote.total,
        business.quote_footer or '',
        header_text=business.quote_header or '',
        payment_method=quote.payment_method or '',
        payment_details=build_payment_details(business),
        payment_terms=business.payment_terms or '',
        terms_of_service=business.terms_of_service or '',
        signature_enabled=quote.digital_signature_enabled,
        document_number=document_number,
        subtotal=subtotal,
        surcharge_percent=quote.surcharge_percent or 0,
        markup_percent=markup_percent,
        markup_amount=markup_amount,
        surcharge_notes=_surcharge_notes(quote),
        notes=quote.notes or '',
        logo_path=logo_path,
        valid_until=quote.valid_until,
        payment_terms_font_size=business.payment_terms_font_size or 9,
        tos_font_size=business.tos_font_size or 8,
    )


def _invoice_pdf_bytes(invoice):
    business = get_business_settings()
    items, subtotal, markup_percent, markup_amount = _items_and_totals_for_pdf(invoice)
    if not items:
        items = [{'description': 'Invoice total', 'detail': '—', 'line_total': invoice.total}]
        subtotal = invoice.total

    logo_path = None
    if business.logo_path:
        from flask import current_app
        logo_path = current_app.root_path + '/static/uploads/' + business.logo_path

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
        markup_percent=markup_percent,
        markup_amount=markup_amount,
        surcharge_notes=_surcharge_notes(invoice),
        notes=invoice.notes or '',
        logo_path=logo_path,
        due_date=invoice.due_date,
        payment_terms_font_size=business.payment_terms_font_size or 9,
        tos_font_size=business.tos_font_size or 8,
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
    business = get_business_settings()
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

        payment_method_str, overrides, effective_surcharge = parse_payment_methods_and_surcharges(
            request.form, business,
        )

        quote = Quote(
            quote_number=generate_quote_number(),
            client=client,
            notes=request.form.get('notes', ''),
            payment_method=payment_method_str,
            surcharge_overrides=json.dumps(overrides),
            surcharge_percent=effective_surcharge,
            markup_percent=float(request.form.get('markup_percent', 0) or 0),
            show_markup_to_client=request.form.get('show_markup_to_client') == 'on',
            digital_signature_enabled=request.form.get('digital_signature_enabled') == 'on',
            valid_until=_parse_date(request.form.get('valid_until')),
            upload_token=generate_upload_token(),
        )
        db.session.add(quote)
        db.session.commit()
        flash('Quote created')
        return redirect(url_for('main.quote_detail', quote_id=quote.id))
    clients = Client.query.order_by(Client.name).all()
    return render_template('new_quote.html', clients=clients, business=business)


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
    business = get_business_settings()
    quote.notes = request.form.get('notes', '')
    quote.status = request.form.get('status', quote.status)
    quote.digital_signature_enabled = request.form.get('digital_signature_enabled') == 'on'
    quote.markup_percent = float(request.form.get('markup_percent', 0) or 0)
    quote.show_markup_to_client = request.form.get('show_markup_to_client') == 'on'
    if 'valid_until' in request.form:
        quote.valid_until = _parse_date(request.form.get('valid_until'))

    payment_method_str, overrides, effective_surcharge = parse_payment_methods_and_surcharges(
        request.form, business,
    )
    quote.payment_method = payment_method_str
    quote.surcharge_overrides = json.dumps(overrides)
    quote.surcharge_percent = effective_surcharge

    recalculate_quote_total(quote)
    db.session.commit()
    flash('Quote updated')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/quotes/<int:quote_id>/set-version', methods=['POST'])
@login_required
def set_quote_version(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    new_version = (request.form.get('version') or '').strip()
    if not new_version:
        flash('Enter a version number')
        return redirect(url_for('main.quote_detail', quote_id=quote.id))

    old_version = quote.version or '1'
    quote.version = new_version
    log_line = f'{datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC: v{old_version} \u2192 v{new_version}'
    quote.version_history = f'{quote.version_history}\n{log_line}' if quote.version_history else log_line
    db.session.commit()
    flash(f'Quote updated to version {new_version}')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/quotes/<int:quote_id>/delete', methods=['POST'])
@login_required
def delete_quote(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    quote_number = quote.display_number
    db.session.delete(quote)
    db.session.commit()
    log_audit('quote_deleted', target_type='quote', target_id=quote_id, detail=quote_number)
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
    filename = _safe_filename_part(quote.display_number)
    if quote.version:
        filename += f'-v{_safe_filename_part(quote.version)}'
    return send_file(
        __import__('io').BytesIO(pdf), mimetype='application/pdf',
        as_attachment=True, download_name=f'{filename}.pdf',
    )


@main_bp.route('/quotes/<int:quote_id>/email', methods=['POST'])
@login_required
def generate_quote_email(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    business = get_business_settings()
    try:
        pdf = _quote_pdf_bytes(quote)
        if not quote.upload_token:
            quote.upload_token = generate_upload_token()
            db.session.commit()
        upload_link = url_for('public.upload_signed_quote', token=quote.upload_token, _external=True)

        default_body = (
            f"Hi {quote.client.name},\n\n"
            f"Please find attached your quote {quote.display_number} "
            f"for ${quote.total:,.2f}.\n\n"
            + (f"This quote is valid until {quote.valid_until.strftime('%d %B %Y')}.\n\n"
               if quote.valid_until else '')
            + f"If you'd like to proceed, you can upload a signed copy of this quote here:\n{upload_link}\n\n"
            + f"Kind regards,\n{business.name or ''}"
        )
        default_subject = f'Quote {quote.display_number} from {business.name or "us"}'

        context = {
            'client_name': quote.client.name,
            'business_name': business.name or '',
            'document_number': quote.display_number,
            'total': f'{quote.total:,.2f}',
            'valid_until': quote.valid_until.strftime('%d %B %Y') if quote.valid_until else '',
            'upload_link': upload_link,
        }
        subject = render_email_template(business.quote_email_subject, context) or default_subject
        html_body = render_email_template(business.quote_email_body_html, context, escape_html=True)
        body_text = html_to_text(html_body) if html_body else default_body

        send_document_email(
            business, quote.client.email,
            subject=subject,
            body_text=body_text, html_body=html_body,
            pdf_bytes=pdf, filename=f'{quote.display_number}.pdf',
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
        notify_me=quote.notify_me,
    )
    db.session.add(job)
    quote.status = 'In Production'
    db.session.commit()
    flash('Job created from quote.')
    return redirect(url_for('main.jobs'))


@main_bp.route('/invoices', methods=['GET'])
@login_required
def invoices():
    show_archived = request.args.get('archived') == '1'
    q = (request.args.get('q') or '').strip()
    query = Invoice.query.join(Client, isouter=True)
    if not show_archived:
        query = query.filter(Invoice.archived.is_(False))
    if q:
        like = f'%{q}%'
        query = query.filter(db.or_(Invoice.invoice_number.ilike(like), Client.name.ilike(like)))
    invoices = query.order_by(Invoice.created_at.desc()).all()
    return render_template('invoices.html', invoices=invoices, show_archived=show_archived, q=q)


@main_bp.route('/invoices/new', methods=['GET', 'POST'])
@login_required
def new_invoice():
    business = get_business_settings()
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
        payment_method_str, overrides, effective_surcharge = parse_payment_methods_and_surcharges(
            request.form, business,
        )

        invoice = Invoice(
            invoice_number=generate_invoice_number(),
            client=client,
            notes=request.form.get('notes', ''),
            payment_method=payment_method_str,
            surcharge_overrides=json.dumps(overrides),
            surcharge_percent=effective_surcharge,
            markup_percent=float(request.form.get('markup_percent', 0) or 0),
            show_markup_to_client=request.form.get('show_markup_to_client') == 'on',
            due_date=due_date,
            status='Draft',
        )
        db.session.add(invoice)
        db.session.commit()
        flash('Invoice created')
        return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))
    clients = Client.query.order_by(Client.name).all()
    return render_template(
        'new_invoice.html', clients=clients, business=business,
        default_due_date=default_due_date(),
    )


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
    business = get_business_settings()
    new_status = request.form.get('status', invoice.status)
    invoice.notes = request.form.get('notes', '')
    invoice.markup_percent = float(request.form.get('markup_percent', 0) or 0)
    invoice.show_markup_to_client = request.form.get('show_markup_to_client') == 'on'
    invoice.notify_me = request.form.get('notify_me') == 'on'
    if 'due_date' in request.form:
        invoice.due_date = _parse_date(request.form.get('due_date'))

    payment_method_str, overrides, effective_surcharge = parse_payment_methods_and_surcharges(
        request.form, business,
    )
    invoice.payment_method = payment_method_str
    invoice.surcharge_overrides = json.dumps(overrides)
    invoice.surcharge_percent = effective_surcharge

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
    invoice_number = invoice.display_number
    db.session.delete(invoice)
    db.session.commit()
    log_audit('invoice_deleted', target_type='invoice', target_id=invoice_id, detail=invoice_number)
    flash('Invoice deleted')
    return redirect(url_for('main.invoices'))


@main_bp.route('/invoices/<int:invoice_id>/archive', methods=['POST'])
@login_required
def archive_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    invoice.archived = True
    db.session.commit()
    flash('Invoice archived')
    return redirect(url_for('main.invoices'))


@main_bp.route('/invoices/<int:invoice_id>/unarchive', methods=['POST'])
@login_required
def unarchive_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    invoice.archived = False
    db.session.commit()
    flash('Invoice restored')
    return redirect(url_for('main.invoices', archived='1'))


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
        default_body = (
            f"Hi {invoice.client.name},\n\n"
            f"Please find attached invoice {invoice.display_number} "
            f"for ${invoice.total:,.2f}.\n\n"
            + (f"Payment is due by {invoice.due_date.strftime('%d %B %Y')}.\n\n"
               if invoice.due_date else '')
            + f"Kind regards,\n{business.name or ''}"
        )
        default_subject = f'Invoice {invoice.display_number} from {business.name or "us"}'

        context = {
            'client_name': invoice.client.name,
            'business_name': business.name or '',
            'document_number': invoice.display_number,
            'total': f'{invoice.total:,.2f}',
            'due_date': invoice.due_date.strftime('%d %B %Y') if invoice.due_date else '',
        }
        subject = render_email_template(business.invoice_email_subject, context) or default_subject
        html_body = render_email_template(business.invoice_email_body_html, context, escape_html=True)
        body_text = html_to_text(html_body) if html_body else default_body

        send_document_email(
            business, invoice.client.email,
            subject=subject,
            body_text=body_text, html_body=html_body,
            pdf_bytes=pdf, filename=f'{invoice.display_number}.pdf',
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
    client_name = client.name
    db.session.delete(client)
    db.session.commit()
    log_audit('client_deleted', target_type='client', target_id=client_id, detail=client_name)
    flash('Client deleted')
    return redirect(url_for('main.clients'))


@main_bp.route('/jobs')
@login_required
def jobs():
    show_archived = request.args.get('archived') == '1'
    q = (request.args.get('q') or '').strip()
    query = Job.query.join(Client, isouter=True)
    if not show_archived:
        query = query.filter(Job.archived.is_(False))
    if q:
        like = f'%{q}%'
        query = query.filter(db.or_(Job.title.ilike(like), Client.name.ilike(like)))
    jobs = query.order_by(Job.created_at.desc()).all()
    return render_template('jobs.html', jobs=jobs, show_archived=show_archived, q=q)


@main_bp.route('/jobs/<int:job_id>/edit', methods=['POST'])
@login_required
def edit_job(job_id):
    job = Job.query.get_or_404(job_id)
    job.title = request.form.get('title', job.title)
    new_status = request.form.get('status', job.status)
    job.status = new_status
    job.notes = request.form.get('notes', '')
    db.session.commit()

    if new_status == 'Complete' and job_should_notify(job):
        business = get_business_settings()
        try:
            if send_job_complete_notification(job, business):
                flash('Job updated — customer notified by email.')
            else:
                flash('Job updated')
        except EmailNotConfiguredError:
            flash('Job updated — but email is not configured, so the customer was not notified.')
        except Exception as e:
            flash(f'Job updated — but the notification email failed to send: {e}')
    else:
        flash('Job updated')
    return redirect(url_for('main.jobs'))


@main_bp.route('/jobs/<int:job_id>/archive', methods=['POST'])
@login_required
def archive_job(job_id):
    job = Job.query.get_or_404(job_id)
    job.archived = True
    db.session.commit()
    flash('Job archived')
    return redirect(url_for('main.jobs'))


@main_bp.route('/jobs/<int:job_id>/unarchive', methods=['POST'])
@login_required
def unarchive_job(job_id):
    job = Job.query.get_or_404(job_id)
    job.archived = False
    db.session.commit()
    flash('Job restored')
    return redirect(url_for('main.jobs', archived='1'))


@main_bp.route('/jobs/<int:job_id>/delete', methods=['POST'])
@login_required
def delete_job(job_id):
    job = Job.query.get_or_404(job_id)
    job_number = job.job_number
    db.session.delete(job)
    db.session.commit()
    log_audit('job_deleted', target_type='job', target_id=job_id, detail=job_number)
    flash('Job deleted')
    return redirect(url_for('main.jobs'))


@main_bp.route('/jobs/<int:job_id>/files/<path:filename>')
@login_required
def download_order_file(job_id, filename):
    job = Job.query.get_or_404(job_id)
    directory = os.path.join(order_uploads_dir(), job.job_number or f'JOB-{job.id:04d}')
    file_path = _safe_join(directory, filename)
    if file_path is None or not os.path.isfile(file_path):
        abort(404)
    return send_file(file_path, as_attachment=True)


# --- Requests (public order-form submissions awaiting triage) ----------------------

@main_bp.route('/requests')
@login_required
def requests_list():
    show_archived = request.args.get('archived') == '1'
    q = (request.args.get('q') or '').strip()
    query = Request.query.join(Client, isouter=True)
    if not show_archived:
        query = query.filter(Request.status != 'Archived')
    if q:
        like = f'%{q}%'
        query = query.filter(db.or_(Client.name.ilike(like), Request.request_number.ilike(like)))
    reqs = query.order_by(Request.created_at.desc()).all()
    return render_template('requests.html', requests=reqs, show_archived=show_archived, q=q)


@main_bp.route('/requests/<int:request_id>')
@login_required
def request_detail(request_id):
    req = Request.query.get_or_404(request_id)
    return render_template('request_detail.html', req=req)


@main_bp.route('/requests/<int:request_id>/convert-to-quote', methods=['POST'])
@login_required
def convert_request_to_quote(request_id):
    req = Request.query.get_or_404(request_id)
    if req.quote_id:
        flash('This request has already been converted to a quote.')
        return redirect(url_for('main.quote_detail', quote_id=req.quote_id))

    order = req.order_data
    notes_parts = []
    if order.get('description'):
        notes_parts.append(f"Description: {order['description']}")
    if order.get('materials'):
        notes_parts.append(f"Materials requested: {', '.join(order['materials'])}")
    if order.get('other_material'):
        notes_parts.append(f"Other material requested: {order['other_material']}")
    if order.get('model_links'):
        notes_parts.append(f"Model links: {order['model_links']}")
    if order.get('reference_links'):
        notes_parts.append(f"Reference links: {order['reference_links']}")
    if order.get('shipping_address'):
        notes_parts.append(f"Shipping to: {order['shipping_address']}")
    if order.get('notes'):
        notes_parts.append(f"Customer notes: {order['notes']}")

    quote = Quote(
        quote_number=generate_quote_number(),
        client_id=req.client_id,
        notes='\n'.join(notes_parts),
        notify_me=req.notify_me,
        upload_token=generate_upload_token(),
    )
    db.session.add(quote)
    db.session.commit()

    req.status = 'Converted'
    req.quote_id = quote.id
    db.session.commit()

    flash('Request converted to quote — add line items and pricing below.')
    return redirect(url_for('main.quote_detail', quote_id=quote.id))


@main_bp.route('/requests/<int:request_id>/mark-reviewed', methods=['POST'])
@login_required
def mark_request_reviewed(request_id):
    req = Request.query.get_or_404(request_id)
    req.status = 'Reviewed'
    db.session.commit()
    flash('Request marked as reviewed')
    return redirect(url_for('main.request_detail', request_id=req.id))


@main_bp.route('/requests/<int:request_id>/archive', methods=['POST'])
@login_required
def archive_request(request_id):
    req = Request.query.get_or_404(request_id)
    req.status = 'Archived'
    db.session.commit()
    flash('Request archived')
    return redirect(url_for('main.requests_list'))


@main_bp.route('/requests/<int:request_id>/files/<path:filename>')
@login_required
def download_request_file(request_id, filename):
    req = Request.query.get_or_404(request_id)
    directory = os.path.join(order_uploads_dir(), req.request_number or f'REQ-{req.id:04d}')
    file_path = _safe_join(directory, filename)
    if file_path is None or not os.path.isfile(file_path):
        abort(404)
    return send_file(file_path, as_attachment=True)


@main_bp.route('/quotes/<int:quote_id>/signed-copy')
@login_required
def download_signed_copy(quote_id):
    quote = Quote.query.get_or_404(quote_id)
    if not quote.signed_copy_filename:
        abort(404)
    ext = os.path.splitext(quote.signed_copy_filename)[1]
    return send_file(
        os.path.join(signed_uploads_dir(), quote.signed_copy_filename),
        as_attachment=True, download_name=f'{quote.display_number}-signed{ext}',
    )
