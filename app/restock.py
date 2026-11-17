"""Restock shopping list: how many spools of each low/out-of-stock filament to buy
to get back above the low-stock threshold, and roughly what that will cost.

A filament's "typical spool" is its most recently added spool — its starting
weight is the spool size to buy (1000g when the filament has no spools), and its
purchase cost is the unit price when recorded; otherwise cost_per_kg × spool kg.
"""
import math

from app.inventory import low_stock_filaments

DEFAULT_SPOOL_G = 1000.0


def _latest_spool(filament):
    if not filament.spools:
        return None
    return max(filament.spools, key=lambda s: (s.created_at is not None, s.created_at, s.id or 0))


def suggested_spools(current_g, threshold_g, spool_g):
    """Smallest whole number of spools (at least 1) that takes stock strictly above
    the threshold — low stock means at or below it."""
    spool_g = spool_g if spool_g and spool_g > 0 else DEFAULT_SPOOL_G
    shortfall = (threshold_g or 0) - (current_g or 0)
    if shortfall < 0:
        return 1
    return max(1, math.floor(shortfall / spool_g) + 1)


def restock_line(filament, threshold_g):
    spool = _latest_spool(filament)
    spool_g = spool.initial_weight_g if spool and spool.initial_weight_g and spool.initial_weight_g > 0 else DEFAULT_SPOOL_G
    if spool and spool.purchase_cost and spool.purchase_cost > 0:
        unit_cost, cost_source = spool.purchase_cost, 'last spool'
    else:
        unit_cost, cost_source = (filament.cost_per_kg or 0) * spool_g / 1000.0, 'cost per kg'
    current = filament.total_remaining_g
    qty = suggested_spools(current, threshold_g, spool_g)
    return {
        'filament': filament,
        'current_g': round(current, 2),
        'spool_g': spool_g,
        'quantity': qty,
        'unit_cost': round(unit_cost, 2),
        'cost_source': cost_source,
        'line_total': round(unit_cost * qty, 2),
    }


def restock_list(threshold_g):
    """(lines, grand_total) for every filament at or below the threshold."""
    threshold = threshold_g if threshold_g is not None else 200
    lines = [restock_line(f, threshold) for f in low_stock_filaments(threshold)]
    return lines, round(sum(l['line_total'] for l in lines), 2)
