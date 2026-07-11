from datetime import datetime

from app.models import BusinessSettings, Quote, Invoice, Job


def get_business_settings():
    business = BusinessSettings.query.first()
    if not business:
        business = BusinessSettings(name='Your Business')
        from app import db
        db.session.add(business)
        db.session.commit()
    return business


def calculate_line_price(filament, weight_g, hardware_cost, print_time_hours, hourly_rate=0):
    material = 0.0
    if filament:
        material = (weight_g / 1000.0) * filament.sell_price_per_kg
    time_cost = print_time_hours * hourly_rate
    return round(material + hardware_cost + time_cost, 2)


def recalculate_quote_total(quote):
    subtotal = sum(item.unit_price for item in quote.items)
    quote.total = round(subtotal * (1 + (quote.surcharge_percent or 0) / 100), 2)
    return quote.total


def generate_quote_number():
    year = datetime.utcnow().year
    count = Quote.query.filter(Quote.quote_number.like(f'Q-{year}-%')).count() + 1
    return f'Q-{year}-{count:04d}'


def generate_invoice_number():
    year = datetime.utcnow().year
    count = Invoice.query.filter(Invoice.invoice_number.like(f'INV-{year}-%')).count() + 1
    return f'INV-{year}-{count:04d}'


def generate_job_number():
    year = datetime.utcnow().year
    count = Job.query.filter(Job.job_number.like(f'JOB-{year}-%')).count() + 1
    return f'JOB-{year}-{count:04d}'


def copy_quote_items_to_invoice(quote, invoice):
    from app.models import InvoiceItem
    for item in quote.items:
        invoice.items.append(InvoiceItem(
            description=item.description,
            weight_g=item.weight_g,
            filament_id=item.filament_id,
            hardware_cost=item.hardware_cost,
            print_time_hours=item.print_time_hours,
            unit_price=item.unit_price,
        ))
    invoice.total = quote.total
    invoice.surcharge_percent = quote.surcharge_percent
    invoice.payment_method = quote.payment_method
    invoice.notes = quote.notes
