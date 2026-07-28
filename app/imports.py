"""CSV import for the same tables export.py can produce a CSV for. Each importer
matches export.py's column headers exactly, so a file downloaded from Data Export,
edited (e.g. in a spreadsheet while cleaning up legacy data), and re-uploaded here
round-trips correctly. Rows are matched to existing records by their natural key
(client name/email, quote/invoice/job/request number, filament name) where possible
so re-running an import updates rather than duplicates.

Every run happens inside the current db.session; the caller decides whether to
commit or roll back once every row has been processed, which is what powers the
dry-run preview (process everything, then roll back instead of committing).
"""
import csv
import io

from flask import Blueprint, request, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Client, Filament, FilamentSpool, Quote, Invoice, Job, Request as OrderRequest
from app.helpers import (
    log_audit, render_template, generate_quote_number, generate_invoice_number,
    generate_job_number, generate_request_number, generate_upload_token,
)

import_bp = Blueprint('import_data', __name__, url_prefix='/dash/import')


def _clean(value):
    return (value or '').strip()


def _parse_float(value):
    value = _clean(value)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_datetime(value):
    from datetime import datetime
    value = _clean(value)
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _get_or_create_client(name):
    name = _clean(name)
    if not name:
        return None
    client = Client.query.filter(db.func.lower(Client.name) == name.lower()).first()
    if client:
        return client
    client = Client(name=name)
    db.session.add(client)
    db.session.flush()
    return client


def _row_customers(row):
    name = _clean(row.get('Name'))
    if not name:
        return 'error', '', 'Missing "Name"'
    email = _clean(row.get('Email'))
    row_id = _clean(row.get('ID'))
    existing = Client.query.get(int(row_id)) if row_id.isdigit() else None
    if not existing:
        q = Client.query.filter(db.func.lower(Client.name) == name.lower())
        existing = q.filter(db.func.lower(Client.email) == email.lower()).first() if email else q.first()
    target = existing or Client(name=name)
    target.name = name
    if email:
        target.email = email
    if _clean(row.get('Phone')):
        target.phone = _clean(row.get('Phone'))
    if _clean(row.get('Notes')):
        target.notes = _clean(row.get('Notes'))
    if not existing:
        db.session.add(target)
        return 'create', name, None
    return 'update', name, None


def _row_filament(row):
    name = _clean(row.get('Name'))
    if not name:
        return 'error', '', 'Missing "Name"'
    row_id = _clean(row.get('ID'))
    existing = Filament.query.get(int(row_id)) if row_id.isdigit() else None
    if not existing:
        existing = Filament.query.filter(db.func.lower(Filament.name) == name.lower()).first()
    target = existing or Filament(name=name)
    target.name = name
    for header, attr in (('Brand', 'brand'), ('Material', 'material'), ('Color', 'color'), ('Notes', 'notes')):
        if _clean(row.get(header)):
            setattr(target, attr, _clean(row.get(header)))
    for header, attr in (
        ('Cost per kg', 'cost_per_kg'), ('Price per kg', 'price_per_kg'), ('Charge per gram', 'charge_per_gram'),
    ):
        parsed = _parse_float(row.get(header))
        if parsed is not None:
            setattr(target, attr, parsed)
    if not existing:
        db.session.add(target)
        return 'create', name, None
    return 'update', name, None


def _row_spools(row):
    fname = _clean(row.get('Filament'))
    filament = Filament.query.filter(db.func.lower(Filament.name) == fname.lower()).first() if fname else None
    if not filament:
        return 'error', fname or '(no filament)', 'Filament not found by name — import filament first'
    row_id = _clean(row.get('ID'))
    existing = FilamentSpool.query.get(int(row_id)) if row_id.isdigit() else None
    target = existing or FilamentSpool(filament_id=filament.id, initial_weight_g=1000.0, weight_remaining_g=1000.0)
    target.filament_id = filament.id
    for header, attr in (
        ('Initial Weight (g)', 'initial_weight_g'), ('Remaining (g)', 'weight_remaining_g'),
        ('Purchase Cost', 'purchase_cost'),
    ):
        parsed = _parse_float(row.get(header))
        if parsed is not None:
            setattr(target, attr, parsed)
    if _clean(row.get('Location')):
        target.location = _clean(row.get('Location'))
    if _clean(row.get('Notes')):
        target.notes = _clean(row.get('Notes'))
    if not existing:
        db.session.add(target)
        return 'create', f'{fname} spool', None
    return 'update', f'{fname} spool', None


def _row_requests(row):
    reqnum = _clean(row.get('Request Number'))
    existing = OrderRequest.query.filter_by(request_number=reqnum).first() if reqnum else None
    target = existing or OrderRequest(request_number=reqnum or generate_request_number())
    client = _get_or_create_client(row.get('Client'))
    if client:
        target.client_id = client.id
    if _clean(row.get('Status')):
        target.status = _clean(row.get('Status'))
    if _clean(row.get('Model Source')):
        target.model_source = _clean(row.get('Model Source'))
    if _clean(row.get('Reference Number')):
        target.reference_number = _clean(row.get('Reference Number'))
    created = _parse_datetime(row.get('Created At'))
    if created:
        target.created_at = created
    qnum = _clean(row.get('Converted Quote'))
    if qnum:
        quote = Quote.query.filter_by(quote_number=qnum).first()
        if quote:
            target.quote_id = quote.id
    if not existing:
        db.session.add(target)
        db.session.flush()
        return 'create', target.display_number, None
    return 'update', target.display_number, None


def _row_quotes(row):
    qnum = _clean(row.get('Quote Number'))
    existing = Quote.query.filter_by(quote_number=qnum).first() if qnum else None
    target = existing or Quote(quote_number=qnum or generate_quote_number(), upload_token=generate_upload_token())
    client = _get_or_create_client(row.get('Client'))
    if client:
        target.client_id = client.id
    if _clean(row.get('Status')):
        target.status = _clean(row.get('Status'))
    total = _parse_float(row.get('Total'))
    if total is not None:
        target.total = total
    if _clean(row.get('Version')):
        target.version = _clean(row.get('Version'))
    if _clean(row.get('Reference Number')):
        target.reference_number = _clean(row.get('Reference Number'))
    created = _parse_datetime(row.get('Created At'))
    if created:
        target.created_at = created
    valid_until = _parse_datetime(row.get('Valid Until'))
    if valid_until:
        target.valid_until = valid_until.date()
    if not existing:
        db.session.add(target)
        db.session.flush()
        return 'create', target.display_number, None
    return 'update', target.display_number, None


def _row_invoices(row):
    invnum = _clean(row.get('Invoice Number'))
    existing = Invoice.query.filter_by(invoice_number=invnum).first() if invnum else None
    target = existing or Invoice(invoice_number=invnum or generate_invoice_number())
    client = _get_or_create_client(row.get('Client'))
    if client:
        target.client_id = client.id
    if _clean(row.get('Status')):
        target.status = _clean(row.get('Status'))
    total = _parse_float(row.get('Total'))
    if total is not None:
        target.total = total
    if _clean(row.get('Reference Number')):
        target.reference_number = _clean(row.get('Reference Number'))
    created = _parse_datetime(row.get('Created At'))
    if created:
        target.created_at = created
    due = _parse_datetime(row.get('Due Date'))
    if due:
        target.due_date = due.date()
    paid = _parse_datetime(row.get('Paid At'))
    if paid:
        target.paid_at = paid
    qnum = _clean(row.get('Quote'))
    if qnum:
        quote = Quote.query.filter_by(quote_number=qnum).first()
        if quote:
            target.quote_id = quote.id
    stripe = _clean(row.get('Stripe Pay Link Enabled'))
    if stripe:
        target.stripe_enabled = stripe.lower() in ('yes', 'true', '1')
    if not existing:
        db.session.add(target)
        db.session.flush()
        return 'create', target.display_number, None
    return 'update', target.display_number, None


def _row_jobs(row):
    jobnum = _clean(row.get('Job Number'))
    title = _clean(row.get('Title'))
    existing = Job.query.filter_by(job_number=jobnum).first() if jobnum else None
    if not existing and not title:
        return 'error', jobnum or '(new job)', 'Missing "Title" (required for new jobs)'
    target = existing or Job(job_number=jobnum or generate_job_number(), title=title or 'Imported job')
    if title:
        target.title = title
    client = _get_or_create_client(row.get('Client'))
    if client:
        target.client_id = client.id
    if _clean(row.get('Status')):
        target.status = _clean(row.get('Status'))
    if _clean(row.get('Reference Number')):
        target.reference_number = _clean(row.get('Reference Number'))
    created = _parse_datetime(row.get('Created At'))
    if created:
        target.created_at = created
    qnum = _clean(row.get('Quote'))
    if qnum:
        quote = Quote.query.filter_by(quote_number=qnum).first()
        if quote:
            target.quote_id = quote.id
    if not existing:
        db.session.add(target)
        db.session.flush()
        return 'create', target.display_number, None
    return 'update', target.display_number, None


# key -> (label, expected header for the template hint, row handler, list endpoint to link back to)
IMPORTERS = {
    'customers': ('Customers', ['Name', 'Email', 'Phone', 'Notes'], _row_customers, 'main.clients'),
    'filament': ('Filament', ['Name', 'Brand', 'Material', 'Color', 'Cost per kg', 'Price per kg', 'Charge per gram', 'Notes'], _row_filament, 'filament.index'),
    'spools': ('Filament Spools', ['Filament', 'Initial Weight (g)', 'Remaining (g)', 'Purchase Cost', 'Location', 'Notes'], _row_spools, 'filament.index'),
    'requests': ('Requests', ['Reference Number', 'Request Number', 'Status', 'Client', 'Model Source', 'Created At', 'Converted Quote'], _row_requests, 'main.requests_list'),
    'quotes': ('Quotes', ['Reference Number', 'Quote Number', 'Status', 'Client', 'Total', 'Version', 'Created At', 'Valid Until'], _row_quotes, 'main.quotes'),
    'invoices': ('Invoices', ['Reference Number', 'Invoice Number', 'Status', 'Client', 'Total', 'Created At', 'Due Date', 'Paid At', 'Quote', 'Stripe Pay Link Enabled'], _row_invoices, 'main.invoices'),
    'jobs': ('Jobs', ['Reference Number', 'Job Number', 'Title', 'Status', 'Client', 'Created At', 'Quote'], _row_jobs, 'main.jobs'),
}


@import_bp.route('/')
@login_required
def import_home():
    return render_template('import.html', importers=IMPORTERS)


@import_bp.route('/run', methods=['POST'])
@login_required
def run_import():
    key = request.form.get('type')
    if key not in IMPORTERS:
        flash('Unknown import type.')
        return redirect(url_for('import_data.import_home'))
    label, expected_header, handler, list_endpoint = IMPORTERS[key]
    dry_run = request.form.get('dry_run') == '1'

    csv_text = request.form.get('csv_content')
    if not csv_text:
        file = request.files.get('file')
        if not file or not file.filename:
            flash('Choose a CSV file to import.')
            return redirect(url_for('import_data.import_home'))
        try:
            csv_text = file.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            flash('Could not read that file as UTF-8 text.')
            return redirect(url_for('import_data.import_home'))

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        flash("That file looks empty, or is missing its header row.")
        return redirect(url_for('import_data.import_home'))

    results = []
    counts = {'create': 0, 'update': 0, 'error': 0}
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        try:
            action, row_label, error = handler(row)
        except Exception as e:
            action, row_label, error = 'error', '', str(e)
        counts[action] = counts.get(action, 0) + 1
        results.append({'row': i, 'action': action, 'label': row_label, 'error': error})

    committed = (not dry_run) and counts['error'] == 0
    if committed:
        db.session.commit()
        log_audit(
            'data_imported', target_type=key,
            detail=f"{counts['create']} created, {counts['update']} updated",
        )
    else:
        db.session.rollback()

    return render_template(
        'import_preview.html', type_key=key, type_label=label, results=results, counts=counts,
        committed=committed, csv_content=csv_text, list_endpoint=list_endpoint,
    )
