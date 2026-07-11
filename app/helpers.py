from datetime import datetime, timedelta
import smtplib
from email.message import EmailMessage

from app.models import BusinessSettings, Quote, Invoice, Job


def get_business_settings():
    business = BusinessSettings.query.first()
    if not business:
        business = BusinessSettings(name='Your Business')
        from app import db
        db.session.add(business)
        db.session.commit()
    return business


def calculate_line_price(item_type, filament, weight_g, hardware_cost, print_time_hours,
                          hourly_rate=0, quantity=1, rate=0):
    if item_type == 'flat':
        return round((quantity or 0) * (rate or 0), 2)
    if item_type == 'hourly':
        return round((quantity or 0) * (rate or 0), 2)
    material = 0.0
    if filament:
        material = (weight_g / 1000.0) * filament.sell_price_per_kg
    time_cost = print_time_hours * hourly_rate
    return round(material + hardware_cost + time_cost, 2)


def recalculate_quote_total(quote):
    subtotal = sum(item.unit_price for item in quote.items)
    quote.total = round(subtotal * (1 + (quote.surcharge_percent or 0) / 100), 2)
    return quote.total


def recalculate_invoice_total(invoice):
    subtotal = sum(item.unit_price for item in invoice.items)
    invoice.total = round(subtotal * (1 + (invoice.surcharge_percent or 0) / 100), 2)
    return invoice.total


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


def default_due_date(days=14):
    return (datetime.utcnow() + timedelta(days=days)).date()


def copy_quote_items_to_invoice(quote, invoice):
    from app.models import InvoiceItem
    for item in quote.items:
        invoice.items.append(InvoiceItem(
            description=item.description,
            item_type=item.item_type,
            weight_g=item.weight_g,
            filament_id=item.filament_id,
            hardware_cost=item.hardware_cost,
            print_time_hours=item.print_time_hours,
            quantity=item.quantity,
            rate=item.rate,
            unit_price=item.unit_price,
        ))
    invoice.total = quote.total
    invoice.surcharge_percent = quote.surcharge_percent
    invoice.payment_method = quote.payment_method
    invoice.notes = quote.notes
    invoice.due_date = default_due_date()


class EmailNotConfiguredError(Exception):
    """Raised when a business has not set up SMTP details."""


def send_document_email(business, to_email, subject, body_text, pdf_bytes, filename):
    """Send an email with a PDF attachment using the business's configured SMTP server.

    Raises EmailNotConfiguredError if SMTP hasn't been set up, or smtplib exceptions
    if the send itself fails (bad credentials, unreachable host, etc.) so callers can
    show the user an accurate error instead of a false "sent" message.
    """
    if not business.smtp_host or not business.smtp_username or not business.smtp_password:
        raise EmailNotConfiguredError(
            'Email is not configured yet. Add your SMTP details under Settings → Email.'
        )
    if not to_email:
        raise EmailNotConfiguredError('This client has no email address on file.')

    from_email = business.smtp_from_email or business.smtp_username

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f'{business.name} <{from_email}>' if business.name else from_email
    msg['To'] = to_email
    msg.set_content(body_text)
    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=filename)

    port = business.smtp_port or 587
    with smtplib.SMTP(business.smtp_host, port, timeout=20) as server:
        server.ehlo()
        if port != 465:
            server.starttls()
            server.ehlo()
        server.login(business.smtp_username, business.smtp_password)
        server.send_message(msg)
