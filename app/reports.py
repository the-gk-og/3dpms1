"""Profit reports: revenue vs. material cost for paid invoices over a period.

Revenue follows the dashboard's rule — paid invoices, dated by ``paid_at`` (when the
money came in), not when they were created. Cost is material only; see
``invoice_material_cost`` for exactly what is and isn't counted.
"""
import csv
import io
import math
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, request
from flask_login import login_required

from app.helpers import render_template
from app.models import Filament, Invoice

reports_bp = Blueprint('reports', __name__, url_prefix='/dash/reports')

PERIODS = [
    ('this_month', 'This month'),
    ('last_month', 'Last month'),
    ('this_year', 'This year'),
    ('last_12_months', 'Last 12 months'),
    ('all', 'All time'),
]
PERIOD_LABELS = dict(PERIODS)
DEFAULT_PERIOD = 'this_month'


def _month_start(d, offset=0):
    """First day of d's month, shifted by ``offset`` months."""
    index = d.year * 12 + (d.month - 1) + offset
    return date(index // 12, index % 12 + 1, 1)


def period_bounds(period, today=None):
    """(start, end) datetimes for a period key — start inclusive, end exclusive.
    Either may be None (open-ended). Unknown keys fall back to this month."""
    today = today or date.today()
    if period not in PERIOD_LABELS:
        period = DEFAULT_PERIOD
    if period == 'all':
        return None, None
    if period == 'this_month':
        start, end = _month_start(today), _month_start(today, 1)
    elif period == 'last_month':
        start, end = _month_start(today, -1), _month_start(today)
    elif period == 'this_year':
        start, end = date(today.year, 1, 1), date(today.year + 1, 1, 1)
    else:  # last_12_months: the current month plus the 11 before it
        start, end = _month_start(today, -11), _month_start(today, 1)
    return datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())


def paid_invoices(start=None, end=None):
    """Paid invoices whose paid_at falls in [start, end), oldest payment first."""
    query = Invoice.query.filter(Invoice.status == 'Paid', Invoice.paid_at.isnot(None))
    if start is not None:
        query = query.filter(Invoice.paid_at >= start)
    if end is not None:
        query = query.filter(Invoice.paid_at < end)
    return query.order_by(Invoice.paid_at.asc(), Invoice.id.asc()).all()


def invoice_material_cost(invoice, filament_lookup=None):
    """Estimated material cost of one invoice.

    Filament: grams × the filament's ``cost_per_kg`` / 1000. The grams are the
    ACTUAL amounts taken off the spools when any job linked to this invoice has had
    its filament deducted (``Job.filament_usage``, written by app/inventory.py) —
    summed across every deducted job. Otherwise they're ESTIMATED from the invoice's
    print lines (``weight_g`` is already the line total; it isn't multiplied by
    quantity). Filament priced at cost, not at the sell price charged to the client.

    Hardware: the sum of every line's ``hardware_cost`` (charged once per line).

    Excluded: labour/design time, machine time and electricity, wear, shipping,
    payment fees and other overheads — so "profit" here is revenue minus materials,
    an upper bound on real profit.

    Returns a dict: filament_cost, hardware_cost, material_cost (the two together),
    grams_by_filament {filament_id: grams} (filament_id may be None for usage rows
    whose filament was deleted), names {filament_id: display name},
    grams (total) and source ('actual' | 'estimated' | 'none').
    """
    lookup = filament_lookup if filament_lookup is not None else {}

    def get_filament(fid):
        if fid is None:
            return None
        if fid not in lookup:
            lookup[fid] = Filament.query.get(fid)
        return lookup[fid]

    grams_by_filament, names = {}, {}
    deducted = [j for j in (invoice.jobs or []) if j.filament_deducted_at]
    if deducted:
        source = 'actual'
        for job in deducted:
            for u in job.filament_usage_list:
                fid = u.get('filament_id')
                grams_by_filament[fid] = grams_by_filament.get(fid, 0) + (u.get('grams') or 0)
                names.setdefault(fid, u.get('filament_name') or 'Filament')
    else:
        source = 'estimated'
        for item in invoice.items:
            if (item.item_type or 'print') != 'print' or not item.filament_id:
                continue
            grams = item.weight_g or 0
            if grams > 0:
                grams_by_filament[item.filament_id] = grams_by_filament.get(item.filament_id, 0) + grams

    filament_cost = 0.0
    for fid, grams in grams_by_filament.items():
        filament = get_filament(fid)
        if filament is not None:
            names[fid] = filament.name
            filament_cost += grams * (filament.cost_per_kg or 0) / 1000.0
        else:
            names.setdefault(fid, 'Deleted filament')

    hardware_cost = sum(item.hardware_cost or 0 for item in invoice.items)
    if not grams_by_filament:
        source = 'none'
    return {
        'filament_cost': round(filament_cost, 2),
        'hardware_cost': round(hardware_cost, 2),
        'material_cost': round(filament_cost + hardware_cost, 2),
        'grams_by_filament': grams_by_filament,
        'names': names,
        'grams': round(sum(grams_by_filament.values()), 2),
        'source': source,
    }


def _margin(profit, revenue):
    return round(profit / revenue * 100, 1) if revenue else None


def invoice_rows(invoices):
    """Per-invoice breakdown rows (dicts) for the report table and CSV."""
    lookup, rows = {}, []
    for inv in invoices:
        cost = invoice_material_cost(inv, lookup)
        revenue = round(inv.total or 0, 2)
        profit = round(revenue - cost['material_cost'], 2)
        rows.append({
            'invoice': inv,
            'number': inv.display_number,
            'client': inv.client.name if inv.client else 'No client',
            'paid_at': inv.paid_at,
            'revenue': revenue,
            'profit': profit,
            'margin': _margin(profit, revenue),
            **cost,
        })
    return rows, lookup


def _months_between(first, last):
    """Month-start dates from first to last inclusive."""
    months, m = [], _month_start(first)
    while m <= last:
        months.append(m)
        m = _month_start(m, 1)
    return months


def build_report(period, today=None):
    """Everything the Reports page shows for one period."""
    today = today or date.today()
    if period not in PERIOD_LABELS:
        period = DEFAULT_PERIOD
    start, end = period_bounds(period, today)
    invoices = paid_invoices(start, end)
    rows, lookup = invoice_rows(invoices)

    revenue = round(sum(r['revenue'] for r in rows), 2)
    cost = round(sum(r['material_cost'] for r in rows), 2)
    profit = round(revenue - cost, 2)
    totals = {
        'revenue': revenue,
        'cost': cost,
        'filament_cost': round(sum(r['filament_cost'] for r in rows), 2),
        'hardware_cost': round(sum(r['hardware_cost'] for r in rows), 2),
        'profit': profit,
        'margin': _margin(profit, revenue),
        'count': len(rows),
        'average': round(revenue / len(rows), 2) if rows else 0,
        'actual_count': sum(1 for r in rows if r['source'] == 'actual'),
    }

    # Monthly revenue vs. cost — every month in the period, including empty ones.
    if start is not None:
        first, last = start.date(), _month_start(end.date(), -1)
    elif rows:
        first, last = rows[0]['paid_at'].date(), max(today, rows[-1]['paid_at'].date())
    else:
        first = last = None
    monthly = []
    if first is not None:
        buckets = {m: {'month': m, 'revenue': 0.0, 'cost': 0.0} for m in _months_between(first, last)}
        for r in rows:
            b = buckets.get(_month_start(r['paid_at'].date()))
            if b is not None:
                b['revenue'] += r['revenue']
                b['cost'] += r['material_cost']
        monthly = [buckets[m] for m in sorted(buckets)]

    by_client = {}
    for r in rows:
        by_client[r['client']] = by_client.get(r['client'], 0) + r['revenue']
    by_client = sorted(by_client.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:8]

    by_material = {}
    for r in rows:
        for fid, grams in r['grams_by_filament'].items():
            filament = lookup.get(fid)
            label = (filament.material if filament is not None and filament.material else None) \
                or r['names'].get(fid) or 'Other'
            by_material[label] = by_material.get(label, 0) + grams
    by_material = sorted(((k, round(v, 1)) for k, v in by_material.items() if v > 0),
                         key=lambda kv: (-kv[1], kv[0].lower()))

    top = sorted(rows, key=lambda r: (-r['profit'], r['number']))[:10]

    return {
        'period': period,
        'period_label': PERIOD_LABELS[period],
        'start': start,
        'end': end,
        'range_label': (f"{start.day} {start:%b %Y} – {(end - timedelta(days=1)).day} {(end - timedelta(days=1)):%b %Y}"
                        if start else ''),
        'rows': rows,
        'totals': totals,
        'monthly': monthly,
        'monthly_chart': monthly_chart(monthly),
        'by_client': by_client,
        'by_material': by_material,
        'top': top,
    }


def _nice_ticks(peak, target=4):
    """(tick, count) so gridlines land on tidy values (1/2/2.5/5 × 10^n) and
    tick × count covers the peak with roughly ``target`` intervals."""
    if peak <= 0:
        return 1, target
    raw = peak / target
    magnitude = 10 ** math.floor(math.log10(raw))
    tick = next(step * magnitude for step in (1, 2, 2.5, 5, 10) if raw <= step * magnitude)
    return tick, max(1, math.ceil(peak / tick - 1e-9))


def monthly_chart(monthly, width=720, height=240):
    """Geometry for the grouped revenue/cost bar chart, rendered as inline SVG in the
    template. Colours come from CSS classes so it follows the light/dark theme."""
    pad_l, pad_r, pad_t, pad_b = 56, 8, 12, 28
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    peak = max([m['revenue'] for m in monthly] + [m['cost'] for m in monthly] + [0])
    tick, count = _nice_ticks(peak)
    top = tick * count
    gridlines = []
    for i in range(count + 1):
        v = tick * i
        gridlines.append({'y': round(pad_t + plot_h - plot_h * i / count, 1),
                          'label': f'${v:,.0f}' if tick >= 1 else f'${v:,.2f}'})
    n = max(len(monthly), 1)
    slot = plot_w / n
    bar_w = max(min(slot * 0.36, 28), 2)
    gap = min(slot * 0.06, 3)
    label_every = 1 if n <= 12 else (2 if n <= 24 else (n + 11) // 12)
    bars = []
    for i, m in enumerate(monthly):
        cx = pad_l + slot * i + slot / 2
        rev_h = plot_h * m['revenue'] / top
        cost_h = plot_h * m['cost'] / top
        bars.append({
            'month': m['month'],
            'revenue': round(m['revenue'], 2),
            'cost': round(m['cost'], 2),
            'rev_x': round(cx - gap / 2 - bar_w, 1), 'rev_y': round(pad_t + plot_h - rev_h, 1), 'rev_h': round(rev_h, 1),
            'cost_x': round(cx + gap / 2, 1), 'cost_y': round(pad_t + plot_h - cost_h, 1), 'cost_h': round(cost_h, 1),
            'label_x': round(cx, 1),
            'label': m['month'].strftime('%b') + (m['month'].strftime(" '%y") if m['month'].month == 1 or i == 0 else ''),
            'show_label': i % label_every == 0,
        })
    return {
        'width': width, 'height': height, 'bar_w': round(bar_w, 1),
        'pad_l': pad_l, 'plot_right': width - pad_r, 'baseline': pad_t + plot_h,
        'label_y': height - 8, 'gridlines': gridlines, 'bars': bars,
    }


def _current_period():
    period = request.args.get('period', DEFAULT_PERIOD)
    return period if period in PERIOD_LABELS else DEFAULT_PERIOD


@reports_bp.route('/')
@login_required
def index():
    report = build_report(_current_period())
    return render_template('reports.html', periods=PERIODS, **report)


def _csv_text(value):
    """Neutralise spreadsheet formula injection in free-text cells."""
    value = '' if value is None else str(value)
    return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


@reports_bp.route('/export.csv')
@login_required
def export_csv():
    report = build_report(_current_period())
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Invoice', 'Client', 'Paid', 'Revenue', 'Filament grams', 'Grams source',
                     'Filament cost', 'Hardware cost', 'Material cost', 'Profit', 'Margin %'])
    for r in report['rows']:
        writer.writerow([
            _csv_text(r['number']), _csv_text(r['client']),
            r['paid_at'].strftime('%Y-%m-%d'),
            f"{r['revenue']:.2f}", f"{r['grams']:g}", r['source'],
            f"{r['filament_cost']:.2f}", f"{r['hardware_cost']:.2f}", f"{r['material_cost']:.2f}",
            f"{r['profit']:.2f}", '' if r['margin'] is None else f"{r['margin']:.1f}",
        ])
    filename = f"profit-report-{report['period'].replace('_', '-')}-{date.today():%Y%m%d}.csv"
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})
