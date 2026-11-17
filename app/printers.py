"""Printers and the print queue board (/dash/printers)."""
from datetime import date

from flask import Blueprint, request, redirect, url_for, flash
from flask_login import login_required

from app import db
from app.models import Job, Printer
from app.helpers import log_audit, render_template
from app.job_schedule import (
    assign_to_printer, due_badge, job_print_hours, move_in_queue, queue_board,
)

printers_bp = Blueprint('printers', __name__, url_prefix='/dash/printers')


@printers_bp.app_template_global('due_badge')
def _due_badge_global(job):
    return due_badge(job, date.today())


@printers_bp.app_template_global('job_print_hours')
def _job_print_hours_global(job):
    return job_print_hours(job)


@printers_bp.app_template_global('active_printers')
def _active_printers_global():
    return Printer.query.filter(Printer.active.is_(True)).order_by(Printer.name).all()


@printers_bp.route('/')
@login_required
def index():
    columns = queue_board()
    inactive = Printer.query.filter(Printer.active.is_(False)).order_by(Printer.name).all()
    return render_template('printers.html', columns=columns, inactive_printers=inactive)


@printers_bp.route('/add', methods=['POST'])
@login_required
def add_printer():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Enter a printer name.')
        return redirect(url_for('printers.index'))
    printer = Printer(
        name=name,
        model=(request.form.get('model') or '').strip(),
        notes=(request.form.get('notes') or '').strip(),
        active=True,
    )
    db.session.add(printer)
    db.session.commit()
    log_audit('printer_added', target_type='printer', target_id=printer.id, detail=printer.name)
    flash(f'Printer "{printer.name}" added.')
    return redirect(url_for('printers.index'))


@printers_bp.route('/<int:printer_id>/edit', methods=['POST'])
@login_required
def edit_printer(printer_id):
    printer = Printer.query.get_or_404(printer_id)
    name = (request.form.get('name') or '').strip()
    if name:
        printer.name = name
    printer.model = (request.form.get('model') or '').strip()
    printer.notes = (request.form.get('notes') or '').strip()
    db.session.commit()
    log_audit('printer_updated', target_type='printer', target_id=printer.id, detail=printer.name)
    flash('Printer updated.')
    return redirect(url_for('printers.index'))


@printers_bp.route('/<int:printer_id>/deactivate', methods=['POST'])
@login_required
def deactivate_printer(printer_id):
    """Take a printer off the board. Its open jobs move to Unassigned so they
    don't disappear from the queue."""
    printer = Printer.query.get_or_404(printer_id)
    moved = 0
    for job in Job.query.filter(Job.printer_id == printer.id).all():
        if job.status not in ('Complete', 'Cancelled') and not job.archived:
            assign_to_printer(job, None)
            moved += 1
    printer.active = False
    db.session.commit()
    log_audit('printer_deactivated', target_type='printer', target_id=printer.id,
              detail=f'{printer.name} ({moved} job(s) moved to Unassigned)')
    msg = f'"{printer.name}" deactivated.'
    if moved:
        msg += f' {moved} queued job{"s" if moved != 1 else ""} moved to Unassigned.'
    flash(msg)
    return redirect(url_for('printers.index'))


@printers_bp.route('/<int:printer_id>/activate', methods=['POST'])
@login_required
def activate_printer(printer_id):
    printer = Printer.query.get_or_404(printer_id)
    printer.active = True
    db.session.commit()
    log_audit('printer_activated', target_type='printer', target_id=printer.id, detail=printer.name)
    flash(f'"{printer.name}" is back on the board.')
    return redirect(url_for('printers.index'))


@printers_bp.route('/jobs/<int:job_id>/assign', methods=['POST'])
@login_required
def assign_job(job_id):
    job = Job.query.get_or_404(job_id)
    printer_id = request.form.get('printer_id', type=int)
    printer = Printer.query.get(printer_id) if printer_id else None
    if printer_id and (printer is None or not printer.active):
        flash('That printer is not available.')
        return redirect(url_for('printers.index'))
    if assign_to_printer(job, printer.id if printer else None):
        db.session.commit()
        log_audit('job_assigned_printer', target_type='job', target_id=job.id,
                  detail=printer.name if printer else 'Unassigned')
        flash(f'{job.display_number} → {printer.name if printer else "Unassigned"}.')
    return redirect(url_for('printers.index') + f'#job-{job.id}')


@printers_bp.route('/jobs/<int:job_id>/move', methods=['POST'])
@login_required
def move_job(job_id):
    job = Job.query.get_or_404(job_id)
    direction = request.form.get('direction')
    if direction in ('up', 'down') and move_in_queue(job, direction):
        db.session.commit()
    return redirect(url_for('printers.index') + f'#job-{job.id}')
