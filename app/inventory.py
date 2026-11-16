"""Filament inventory: taking a finished job's filament off the spools, undoing
that, and spotting filament that's running low.

A job's filament needs come from its print line items (weight_g is the total grams
for the line — print-line pricing doesn't multiply by quantity). The invoice is used
when the job has one, since it reflects what was actually billed; otherwise the quote.
"""
import json
from datetime import datetime

from app.models import Filament, FilamentSpool


def job_filament_needs(job):
    """{filament_id: grams} required by a job's print items."""
    if job.invoice is not None and job.invoice.items:
        items = job.invoice.items
    elif job.quote is not None:
        items = job.quote.items
    else:
        items = []
    needs = {}
    for item in items:
        if (item.item_type or 'print') != 'print' or not item.filament_id:
            continue
        grams = item.weight_g or 0
        if grams > 0:
            needs[item.filament_id] = needs.get(item.filament_id, 0) + grams
    return needs


def deduct_filament_for_job(job):
    """Take the job's filament off the spools, once. Partly used spools are drawn
    down first (smallest remaining first) so open spools get finished before new
    ones are started.

    Returns (usage, shortfalls) — usage is the list stored on the job, shortfalls is
    [(filament_name, grams_missing)] when inventory didn't cover the job. Returns
    None if already deducted or the job has no filament to deduct. Caller commits.
    """
    if job.filament_deducted_at:
        return None
    needs = job_filament_needs(job)
    if not needs:
        return None

    usage, shortfalls = [], []
    for filament_id, grams in needs.items():
        filament = Filament.query.get(filament_id)
        if filament is None:
            continue
        remaining = grams
        spools = sorted((s for s in filament.spools if s.weight_remaining_g > 0),
                        key=lambda s: (s.weight_remaining_g, s.id))
        for spool in spools:
            if remaining <= 0:
                break
            take = min(spool.weight_remaining_g, remaining)
            spool.weight_remaining_g = round(spool.weight_remaining_g - take, 2)
            remaining = round(remaining - take, 2)
            usage.append({'spool_id': spool.id, 'filament_id': filament.id,
                          'filament_name': filament.name, 'grams': round(take, 2)})
        if remaining > 0:
            shortfalls.append((filament.name, remaining))

    job.filament_deducted_at = datetime.utcnow()
    job.filament_usage = json.dumps(usage)
    return usage, shortfalls


def restore_filament_for_job(job):
    """Put a job's deducted filament back on the spools it came from (never above a
    spool's starting weight). Spools deleted since are skipped. Returns grams
    restored. Caller commits."""
    restored = 0.0
    for u in job.filament_usage_list:
        spool = FilamentSpool.query.get(u.get('spool_id'))
        if spool is None:
            continue
        before = spool.weight_remaining_g
        spool.weight_remaining_g = round(min(spool.initial_weight_g or before + u['grams'], before + u['grams']), 2)
        restored += spool.weight_remaining_g - before
    job.filament_deducted_at = None
    job.filament_usage = None
    return round(restored, 2)


def describe_deduction(result):
    """Short human sentence for a flash message, or '' when nothing happened."""
    if not result:
        return ''
    usage, shortfalls = result
    totals = {}
    for u in usage:
        totals[u['filament_name']] = totals.get(u['filament_name'], 0) + u['grams']
    parts = [f'{g:g}g {name}' for name, g in sorted(totals.items())]
    msg = f'Used {", ".join(parts)} from inventory.' if parts else ''
    if shortfalls:
        short = ', '.join(f'{g:g}g {name}' for name, g in shortfalls)
        msg = (msg + ' ' if msg else '') + f'Not enough on the spools — short {short}.'
    return msg


def low_stock_filaments(threshold_g):
    """Filaments with total remaining at or below the threshold, emptiest first."""
    threshold = threshold_g if threshold_g is not None else 200
    low = [f for f in Filament.query.all() if f.total_remaining_g <= threshold]
    return sorted(low, key=lambda f: (f.total_remaining_g, f.name.lower()))
