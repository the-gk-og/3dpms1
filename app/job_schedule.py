"""Job scheduling: due dates and the per-printer print queue.

A job's place on the queue board comes from its printer (None = Unassigned) and
its queue_position within that printer's column. Positions are renumbered 0..n-1
whenever a column is reordered, so gaps or NULLs left by older data sort out on
the first move.
"""
from datetime import date, datetime, timedelta

from app.models import Job, Printer

# Jobs in these states are finished with and drop off due-date badges and the queue.
CLOSED_STATUSES = ('Complete', 'Cancelled')

# "Due in N days" turns from muted to warning at this many days out.
DUE_WARNING_DAYS = 3


def parse_due_date(value):
    """'YYYY-MM-DD' from a date input -> date, or None when blank/invalid."""
    value = (value or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def due_badge(job, today=None):
    """(label, badge_variant) for an open job with a due date, else None.
    badge_variant is one of 'danger', 'warning', 'muted'."""
    if not job.due_date or job.status in CLOSED_STATUSES:
        return None
    today = today or date.today()
    days = (job.due_date - today).days
    if days < 0:
        return 'Overdue', 'danger'
    if days == 0:
        return 'Due today', 'warning'
    label = f'Due in {days} day{"s" if days != 1 else ""}'
    return label, 'warning' if days <= DUE_WARNING_DAYS else 'muted'


def open_jobs_query():
    """Non-archived jobs that aren't Complete/Cancelled."""
    return Job.query.filter(Job.archived.is_(False), Job.status.notin_(CLOSED_STATUSES))


def due_soon_jobs(today=None, days=7, limit=6):
    """Open jobs that are overdue or due within `days`, soonest first."""
    today = today or date.today()
    return (open_jobs_query()
            .filter(Job.due_date.isnot(None), Job.due_date <= today + timedelta(days=days))
            .order_by(Job.due_date, Job.id)
            .limit(limit)
            .all())


def job_print_hours(job):
    """Estimated print hours: print_time_hours summed over the print line items of
    the job's invoice if it has one (with items), else its quote."""
    if job.invoice is not None and job.invoice.items:
        items = job.invoice.items
    elif job.quote is not None:
        items = job.quote.items
    else:
        items = []
    return round(sum(item.print_time_hours or 0 for item in items
                     if (item.item_type or 'print') == 'print'), 2)


def _queue_sort_key(job):
    return (job.queue_position is None, job.queue_position or 0,
            job.due_date is None, job.due_date or date.min, job.id)


def queue_for(printer_id):
    """Open jobs queued on a printer (None = unassigned), in queue order."""
    jobs = open_jobs_query().filter(Job.printer_id == printer_id if printer_id else Job.printer_id.is_(None)).all()
    return sorted(jobs, key=_queue_sort_key)


def queued_hours(jobs):
    return round(sum(job_print_hours(j) for j in jobs), 2)


def queue_board(now=None):
    """Columns for the queue board: one per active printer, then Unassigned.
    Each column is a dict with printer, cards [(job, hours, finish_at)], total_hours,
    finish_at (None for Unassigned, which isn't printing anywhere)."""
    now = now or datetime.now()
    printers = Printer.query.filter(Printer.active.is_(True)).order_by(Printer.name).all()
    columns = []
    for printer in printers + [None]:
        jobs = queue_for(printer.id if printer else None)
        cards, running = [], 0.0
        for job in jobs:
            hours = job_print_hours(job)
            running += hours
            cards.append({'job': job, 'hours': hours,
                          'finish_at': now + timedelta(hours=running) if printer else None})
        columns.append({
            'printer': printer,
            'cards': cards,
            'total_hours': round(running, 2),
            'finish_at': now + timedelta(hours=running) if printer and cards else None,
        })
    return columns


def _renumber(jobs):
    for i, job in enumerate(jobs):
        job.queue_position = i


def assign_to_printer(job, printer_id):
    """Put a job at the end of a printer's queue (None = unassign). No-op if it's
    already on that printer. Caller commits."""
    printer_id = printer_id or None
    if job.printer_id == printer_id:
        return False
    old_printer_id = job.printer_id
    others = [j for j in queue_for(printer_id) if j.id != job.id]
    _renumber(others)
    job.printer_id = printer_id
    job.queue_position = len(others)
    _renumber([j for j in queue_for(old_printer_id) if j.id != job.id])
    return True


def move_in_queue(job, direction):
    """Swap a job with its neighbour in its column. direction is 'up' or 'down'.
    Returns True if it moved. Caller commits."""
    jobs = queue_for(job.printer_id)
    if job not in jobs:
        return False
    i = jobs.index(job)
    j = i - 1 if direction == 'up' else i + 1
    if j < 0 or j >= len(jobs):
        _renumber(jobs)
        return False
    jobs[i], jobs[j] = jobs[j], jobs[i]
    _renumber(jobs)
    return True
