"""Rough, instant price estimates for the public order form.

This is deliberately a ballpark, not a quote. The model:

* grams  = volume_cm³ × material density × fill factor
  The fill factor (setting `estimate_fill_factor`, default 0.35) approximates how
  much of a solid part a slicer actually prints — perimeters/top/bottom shells plus
  sparse infill. Small parts are mostly shell (real factor higher), large chunky
  parts mostly infill (lower), and supports/brims aren't counted at all.
* hours  = grams / `estimate_grams_per_hour` (default 12 g/h, a typical FDM rate).
* price  = the same formula staff quotes use (`calculate_line_price`): filament
  sell price per kg × kg + hours × hourly rate. No markup, hardware, setup,
  post-processing or surcharges — then shown as a ±25% range in whole dollars.
"""
import math

from app.helpers import calculate_line_price

DEFAULT_FILL_FACTOR = 0.35
DEFAULT_GRAMS_PER_HOUR = 12.0
DEFAULT_MAX_MM = 256.0
RANGE_FRACTION = 0.25

DEFAULT_DENSITY = 1.24  # g/cm³ — PLA, the most common filament
# Checked in order so e.g. "PLA" doesn't shadow a longer, more specific name.
MATERIAL_DENSITIES = (
    ('PETG', 1.27),
    ('PLA', 1.24),
    ('ABS', 1.04),
    ('ASA', 1.07),
    ('TPU', 1.21),
    ('NYLON', 1.14),
    ('PA', 1.14),
)

DISCLAIMER = ('This is a rough automatic estimate only. Your final quote may be higher or lower '
              'once we’ve checked the model, orientation, supports and finish.')


def density_for(material):
    """Density in g/cm³ for a filament material string (e.g. 'PLA+', 'PETG-CF')."""
    name = (material or '').upper()
    for key, density in MATERIAL_DENSITIES:
        if key == 'PA':
            # Avoid matching 'PA' inside other words; only standalone PA / PA6 / PA12 etc.
            if name == 'PA' or name.startswith(('PA6', 'PA12', 'PA-', 'PA ')):
                return density
            continue
        if key in name:
            return density
    return DEFAULT_DENSITY


def price_range(price, fraction=RANGE_FRACTION):
    """(low, high) whole dollars around `price`: low rounded down, high rounded up."""
    if price <= 0:
        return 0, 0
    low = max(0, math.floor(price * (1 - fraction)))
    high = max(1, math.ceil(price * (1 + fraction)))
    return low, high


def _setting(value, default, lo, hi):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return min(hi, max(lo, value))


def estimate_settings(business):
    return {
        'fill_factor': _setting(business.estimate_fill_factor, DEFAULT_FILL_FACTOR, 0.05, 1.0),
        'grams_per_hour': _setting(business.estimate_grams_per_hour, DEFAULT_GRAMS_PER_HOUR, 0.5, 500.0),
        'max_mm': _setting(business.estimate_max_mm, DEFAULT_MAX_MM, 1.0, 10000.0),
    }


def estimate_for_mesh(mesh, filaments, business):
    """Build the JSON-able estimate for one analysed mesh across `filaments`."""
    cfg = estimate_settings(business)
    hourly_rate = business.hourly_rate or 0
    dims = mesh.dimensions
    warnings = []

    if any(d > cfg['max_mm'] for d in dims):
        warnings.append(
            f'This model is larger than our {cfg["max_mm"]:g} mm build volume in at least one direction '
            f'— it may need to be split into parts or scaled down.')
    if max(dims) < 5:
        warnings.append('This model is very small — if it was exported in inches or metres '
                        'rather than millimetres, the size and price shown will be wrong.')
    if mesh.volume_mm3 <= 0:
        warnings.append('We couldn’t work out a volume for this model (it may not be a closed solid).')

    estimates = []
    for f in filaments:
        density = density_for(f.material or f.name)
        grams = mesh.volume_cm3 * density * cfg['fill_factor']
        hours = grams / cfg['grams_per_hour']
        price = calculate_line_price('print', f, grams, 0, hours, hourly_rate)
        price += per_part_fee(business) * max(1, len(getattr(mesh, 'objects', None) or [1]))
        low, high = price_range(price)
        estimates.append({
            'filament_id': f.id, 'name': f.name, 'material': f.material or '',
            'grams': round(grams, 1), 'hours': round(hours, 1),
            'low': low, 'high': high,
        })
    if not filaments:
        warnings.append('We can’t price this automatically right now — we’ll include a price in your quote.')

    # Headline weight/time are for PLA; per-filament figures are in `estimates`.
    grams = mesh.volume_cm3 * DEFAULT_DENSITY * cfg['fill_factor']
    return {
        'ok': True,
        'dimensions_mm': {'x': round(dims[0], 1), 'y': round(dims[1], 1), 'z': round(dims[2], 1)},
        'volume_cm3': round(mesh.volume_cm3, 2),
        'triangles': mesh.triangle_count,
        'grams': round(grams, 1),
        'hours': round(grams / cfg['grams_per_hour'], 1),
        'estimates': estimates,
        'warnings': warnings,
        'disclaimer': DISCLAIMER,
    }


# --- Multi-object / multi-material estimates ------------------------------------
#
# A customer can upload several files (and a 3MF can hold several objects) and give
# each object its own material. Two optional print modes add cost on top:
#
# * Multi-material — objects in different materials printed together on one plate
#   (AMS / multi-nozzle). Adds a waste allowance for purging/prime towers
#   (`estimate_mm_waste_pct`, % of material cost) and extra time for filament or
#   nozzle changes (`estimate_mm_time_pct`, % of print time).
# * Clean supports — support interfaces printed in a separate break-away material
#   (e.g. PETG under PLA) so the supported surfaces come off clean. Adds
#   `estimate_support_pct` % of the model's weight in `estimate_support_filament`,
#   and always counts as multi-material.

DEFAULT_MM_WASTE_PCT = 10.0
DEFAULT_MM_TIME_PCT = 15.0
DEFAULT_SUPPORT_PCT = 5.0
MAX_PARTS = 50


def per_part_fee(business):
    """Fixed $ charged for every object (part) in an estimate — setup/handling."""
    try:
        fee = float(getattr(business, 'estimate_per_part_fee', 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return min(10000.0, fee) if math.isfinite(fee) and fee > 0 else 0.0


def multi_material_settings(business):
    return {
        'waste_pct': _setting(business.estimate_mm_waste_pct, DEFAULT_MM_WASTE_PCT, 0.1, 500.0),
        'time_pct': _setting(business.estimate_mm_time_pct, DEFAULT_MM_TIME_PCT, 0.1, 500.0),
        'support_pct': _setting(business.estimate_support_pct, DEFAULT_SUPPORT_PCT, 0.1, 100.0),
    }


def default_filament(filaments):
    """What an object is priced in when the customer didn't pick a material: the
    first in-stock PLA, else the first in-stock filament, else None."""
    for f in filaments:
        if 'PLA' in (f.material or f.name or '').upper():
            return f
    return filaments[0] if filaments else None


def combined_estimate(parts, filaments, business, multi_material=False, clean_supports=False,
                      support_filament=None):
    """Price several objects together.

    parts: [{'name': str, 'volume_cm3': float, 'filament_id': int|None, 'file': str?}]
    filaments: the filaments customers may choose (in stock).
    Returns a JSON-able dict with per-part lines, option surcharges and a ±25% range.
    """
    cfg = estimate_settings(business)
    mm = multi_material_settings(business)
    hourly_rate = business.hourly_rate or 0
    by_id = {f.id: f for f in filaments}
    fallback = default_filament(filaments)
    warnings = []

    lines = []
    material_cost = 0.0
    model_grams = 0.0
    hours = 0.0
    used_materials = set()
    for part in parts[:MAX_PARTS]:
        try:
            volume = max(0.0, float(part.get('volume_cm3') or 0))
        except (TypeError, ValueError):
            volume = 0.0
        if not math.isfinite(volume):
            volume = 0.0
        f = by_id.get(part.get('filament_id')) or fallback
        if f is None:
            continue
        grams = volume * density_for(f.material or f.name) * cfg['fill_factor']
        part_hours = grams / cfg['grams_per_hour']
        cost = grams / 1000.0 * f.sell_price_per_kg
        material_cost += cost
        model_grams += grams
        hours += part_hours
        used_materials.add(f.id)
        lines.append({
            'name': str(part.get('name') or 'Object')[:120], 'file': str(part.get('file') or '')[:200],
            'filament_id': f.id, 'filament': f.name, 'color': f.color or '',
            'chosen': part.get('filament_id') in by_id,
            'grams': round(grams, 1), 'hours': round(part_hours, 1),
        })

    if not lines:
        return {'ok': False, 'error': 'Nothing to price yet.', 'lines': [], 'warnings': warnings,
                'disclaimer': DISCLAIMER}

    support_grams = support_cost = 0.0
    support = None
    if clean_supports:
        if support_filament is not None:
            support_grams = model_grams * mm['support_pct'] / 100.0
            support_cost = support_grams / 1000.0 * support_filament.sell_price_per_kg
            hours += support_grams / cfg['grams_per_hour']
            support = {'filament': support_filament.name, 'grams': round(support_grams, 1)}
        else:
            warnings.append('Clean supports will be priced in your quote.')

    is_multi = bool(multi_material or clean_supports)
    waste_cost = (material_cost + support_cost) * mm['waste_pct'] / 100.0 if is_multi else 0.0
    extra_hours = hours * mm['time_pct'] / 100.0 if is_multi else 0.0
    if not multi_material and len(used_materials) > 1:
        warnings.append('Objects in different materials will be printed separately. '
                        'Tick “multi-material” to print them together.')

    total_hours = hours + extra_hours
    fee_each = per_part_fee(business)
    part_fee = {'count': len(lines), 'each': round(fee_each, 2), 'total': round(fee_each * len(lines), 2)} if fee_each else None
    price = material_cost + support_cost + waste_cost + total_hours * hourly_rate + (part_fee['total'] if part_fee else 0)
    low, high = price_range(price)
    return {
        'ok': True,
        'lines': lines,
        'multi_material': is_multi,
        'clean_supports': bool(clean_supports),
        'support': support,
        'part_fee': part_fee,
        'grams': round(model_grams + support_grams, 1),
        'waste_grams': round((model_grams + support_grams) * mm['waste_pct'] / 100.0, 1) if is_multi else 0,
        'hours': round(total_hours, 1),
        'low': low, 'high': high,
        'warnings': warnings,
        'disclaimer': DISCLAIMER,
    }


def describe_combined(est):
    """One-line plain-text summary for emails and the dashboard."""
    if not est or not est.get('ok'):
        return ''
    mats = {}
    for line in est['lines']:
        mats[line['filament']] = mats.get(line['filament'], 0) + line['grams']
    parts = [f'{name} ≈{grams:.0f} g' for name, grams in mats.items()]
    if est.get('support'):
        parts.append(f'{est["support"]["filament"]} supports ≈{est["support"]["grams"]:.0f} g')
    mode = ' · multi-material' if est.get('multi_material') else ''
    fee = est.get('part_fee')
    fee_text = f', incl. {fee["count"]} × ${fee["each"]:.2f} per-part fee' if fee else ''
    return f'${est["low"]}–${est["high"]} ({", ".join(parts)}{mode}, ≈{est["hours"]:g} h{fee_text})'
