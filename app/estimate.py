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
