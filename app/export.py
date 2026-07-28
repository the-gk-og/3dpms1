import csv
import io
import zipfile
from datetime import datetime

from flask import Blueprint, Response, request
from flask_login import login_required

from app.models import Client, Filament, FilamentSpool, Quote, Invoice, Job, Request as OrderRequest, FeedbackSurvey
from app.helpers import log_audit

export_bp = Blueprint('export', __name__, url_prefix='/dash/export')


def _csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


def _customers_rows():
    header = ['ID', 'Name', 'Email', 'Phone', 'Notes', 'Created At']
    rows = []
    for c in Client.query.order_by(Client.name).all():
        rows.append([
            c.id, c.name, c.email or '', c.phone or '', (c.notes or '').replace('\n', ' '),
            c.created_at.isoformat() if c.created_at else '',
        ])
    return header, rows


def _filament_rows():
    header = [
        'ID', 'Name', 'Brand', 'Material', 'Color', 'Cost per kg', 'Price per kg',
        'Charge per gram', 'Total Spools', 'Total Remaining (g)', 'Notes', 'Created At',
    ]
    rows = []
    for f in Filament.query.order_by(Filament.name).all():
        rows.append([
            f.id, f.name, f.brand or '', f.material or '', f.color or '',
            f.cost_per_kg, f.price_per_kg, f.charge_per_gram,
            f.total_spools, f.total_remaining_g, (f.notes or '').replace('\n', ' '),
            f.created_at.isoformat() if f.created_at else '',
        ])
    return header, rows


def _spools_rows():
    header = [
        'ID', 'Filament', 'Initial Weight (g)', 'Remaining (g)', 'Purchase Cost',
        'Location', 'Notes', 'Created At',
    ]
    rows = []
    for s in FilamentSpool.query.order_by(FilamentSpool.id).all():
        rows.append([
            s.id, s.filament.name if s.filament else '', s.initial_weight_g, s.weight_remaining_g,
            s.purchase_cost, s.location or '', (s.notes or '').replace('\n', ' '),
            s.created_at.isoformat() if s.created_at else '',
        ])
    return header, rows


def _quotes_rows():
    header = [
        'Reference Number', 'Quote Number', 'Status', 'Client', 'Total', 'Version', 'Created At',
        'Valid Until', 'Originating Request', 'Linked Jobs', 'Linked Invoices',
    ]
    rows = []
    for q in Quote.query.order_by(Quote.created_at.desc()).all():
        req = q.originating_request
        rows.append([
            q.reference_number or '', q.display_number, q.status, q.client.name if q.client else '', q.total,
            q.version or '1', q.created_at.isoformat() if q.created_at else '',
            q.valid_until.isoformat() if q.valid_until else '',
            req.display_number if req else '',
            ', '.join(j.display_number for j in q.jobs),
            ', '.join(i.display_number for i in q.invoices),
        ])
    return header, rows


def _invoices_rows():
    header = [
        'Reference Number', 'Invoice Number', 'Status', 'Client', 'Total', 'Created At', 'Due Date', 'Paid At',
        'Quote', 'Originating Request', 'Stripe Pay Link Enabled',
    ]
    rows = []
    for inv in Invoice.query.order_by(Invoice.created_at.desc()).all():
        req = inv.originating_request
        rows.append([
            inv.reference_number or '', inv.display_number, inv.status, inv.client.name if inv.client else '', inv.total,
            inv.created_at.isoformat() if inv.created_at else '',
            inv.due_date.isoformat() if inv.due_date else '',
            inv.paid_at.isoformat() if inv.paid_at else '',
            inv.quote.display_number if inv.quote else '',
            req.display_number if req else '',
            'Yes' if inv.stripe_enabled else 'No',
        ])
    return header, rows


def _jobs_rows():
    header = [
        'Reference Number', 'Job Number', 'Title', 'Status', 'Client', 'Created At', 'Quote', 'Originating Request',
    ]
    rows = []
    for j in Job.query.order_by(Job.created_at.desc()).all():
        req = j.originating_request
        rows.append([
            j.reference_number or '', j.display_number, j.title, j.status, j.client.name if j.client else '',
            j.created_at.isoformat() if j.created_at else '',
            j.quote.display_number if j.quote else '',
            req.display_number if req else '',
        ])
    return header, rows


def _requests_rows():
    header = ['Reference Number', 'Request Number', 'Status', 'Client', 'Model Source', 'Created At', 'Converted Quote']
    rows = []
    for r in OrderRequest.query.order_by(OrderRequest.created_at.desc()).all():
        rows.append([
            r.reference_number or '', r.display_number, r.status, r.client.name if r.client else '',
            r.model_source or '', r.created_at.isoformat() if r.created_at else '',
            r.quote.display_number if r.quote else '',
        ])
    return header, rows


def _feedback_rows():
    header = [
        'Respondent Name', 'Respondent Email', 'Client', 'Job', 'Sent At', 'Responded At',
        'Overall Rating', 'Print Quality', 'Customer Service', 'Communication', 'Turnaround',
        'Value for Money', 'Would Recommend', 'Would Order Again', 'Referral Source',
        'Comments', 'Improvements', 'OK for Testimonial',
    ]
    rows = []
    for s in FeedbackSurvey.query.order_by(FeedbackSurvey.sent_at.desc()).all():
        def yn(v):
            return 'Yes' if v is True else ('No' if v is False else '')
        rows.append([
            s.respondent_name or '', s.respondent_email or '',
            s.client.name if s.client else '', s.job.display_number if s.job else '',
            s.sent_at.isoformat() if s.sent_at else '',
            s.responded_at.isoformat() if s.responded_at else '',
            s.rating or '', s.print_quality_rating or '', s.customer_service_rating or '',
            s.communication_rating or '', s.turnaround_rating or '', s.value_rating or '',
            yn(s.would_recommend), yn(s.would_order_again), s.referral_source or '',
            (s.comments or '').replace('\n', ' '), (s.improvements or '').replace('\n', ' '),
            yn(s.testimonial_ok),
        ])
    return header, rows


EXPORTS = {
    'customers': ('customers.csv', _customers_rows),
    'filament': ('filament.csv', _filament_rows),
    'spools': ('filament-spools.csv', _spools_rows),
    'quotes': ('quotes.csv', _quotes_rows),
    'invoices': ('invoices.csv', _invoices_rows),
    'jobs': ('jobs.csv', _jobs_rows),
    'requests': ('requests.csv', _requests_rows),
    'feedback': ('feedback.csv', _feedback_rows),
}


@export_bp.route('/<key>.csv')
@login_required
def export_csv(key):
    if key not in EXPORTS:
        return Response('Unknown export.', status=404)
    filename, builder = EXPORTS[key]
    header, rows = builder()
    log_audit('data_exported', target_type=key, detail=f'{len(rows)} rows')
    return _csv_response(filename, header, rows)


@export_bp.route('/all.zip')
@login_required
def export_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for key, (filename, builder) in EXPORTS.items():
            header, rows = builder()
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
            zf.writestr(filename, csv_buf.getvalue())
    buf.seek(0)
    log_audit('data_exported', target_type='all', detail='full zip export')
    stamp = datetime.utcnow().strftime('%Y%m%d')
    return Response(
        buf.getvalue(),
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="3dpms-export-{stamp}.zip"'},
    )
