from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    two_factor_enabled = db.Column(db.Boolean, default=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class BusinessSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, default='Your Business')
    address = db.Column(db.String(500))
    contact_email = db.Column(db.String(200))
    email = db.Column(db.String(200))
    phone = db.Column(db.String(50))
    website = db.Column(db.String(200))
    logo_path = db.Column(db.String(200))
    quote_header = db.Column(db.Text)
    quote_footer = db.Column(db.Text)
    invoice_header = db.Column(db.Text)
    invoice_footer = db.Column(db.Text)
    payment_terms = db.Column(db.Text)
    terms_of_service = db.Column(db.Text)
    hourly_rate = db.Column(db.Float, default=0.0)
    # Payment details
    pay_id = db.Column(db.String(200))
    bank_name = db.Column(db.String(200))
    bank_account_name = db.Column(db.String(200))
    bank_bsb = db.Column(db.String(20))
    bank_account_number = db.Column(db.String(50))
    paypal_email = db.Column(db.String(200))
    stripe_link = db.Column(db.String(500))
    smtp_host = db.Column(db.String(200))
    smtp_port = db.Column(db.Integer, default=587)
    smtp_username = db.Column(db.String(200))
    smtp_password = db.Column(db.String(200))
    smtp_from_email = db.Column(db.String(200))
    # Default surcharge % applied per payment method (used to prefill new quotes/invoices)
    surcharge_bank_transfer = db.Column(db.Float, default=0.0)
    surcharge_pay_id = db.Column(db.Float, default=0.0)
    surcharge_cash = db.Column(db.Float, default=0.0)
    surcharge_eft = db.Column(db.Float, default=0.0)
    surcharge_credit_card = db.Column(db.Float, default=0.0)
    surcharge_stripe = db.Column(db.Float, default=0.0)
    # Custom email templates (optional — falls back to a plain-text default when blank)
    quote_email_subject = db.Column(db.String(300))
    quote_email_body_html = db.Column(db.Text)
    invoice_email_subject = db.Column(db.String(300))
    invoice_email_body_html = db.Column(db.Text)
    # Cloudflare Turnstile (bot protection) — optional, forms skip verification if unset
    turnstile_site_key = db.Column(db.String(200))
    turnstile_secret_key = db.Column(db.String(200))
    payment_terms_font_size = db.Column(db.Float, default=9.0)
    tos_font_size = db.Column(db.Float, default=8.0)


class Filament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    brand = db.Column(db.String(100))
    material = db.Column(db.String(50))
    color = db.Column(db.String(50))
    cost_per_kg = db.Column(db.Float, nullable=False, default=0.0)
    price_per_kg = db.Column(db.Float, nullable=False, default=0.0)
    charge_per_gram = db.Column(db.Float, nullable=False, default=0.0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    spools = db.relationship('FilamentSpool', backref='filament', lazy=True, cascade='all, delete-orphan')

    @property
    def sell_price_per_kg(self):
        if self.price_per_kg > 0:
            return self.price_per_kg
        if self.charge_per_gram > 0:
            return self.charge_per_gram * 1000
        return self.cost_per_kg

    @property
    def total_remaining_g(self):
        return sum(s.weight_remaining_g for s in self.spools)

    @property
    def in_stock(self):
        return self.total_remaining_g > 0

    @property
    def total_spools(self):
        return len(self.spools)

    @property
    def total_weight_g(self):
        return sum(s.weight_remaining_g for s in self.spools)

    @property
    def material_emoji(self):
        emojis = {
            'PLA': '🌿', 'ABS': '🔥', 'PETG': '💎', 'TPU': '🧊',
            'Nylon': '⚙️', 'Carbon Fiber': '🏎️', 'Wood': '🪵', 'Resin': '💧',
        }
        return emojis.get(self.material or '', '🎨')


class FilamentSpool(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filament_id = db.Column(db.Integer, db.ForeignKey('filament.id'), nullable=False)
    initial_weight_g = db.Column(db.Float, nullable=False, default=1000.0)
    weight_remaining_g = db.Column(db.Float, nullable=False, default=1000.0)
    purchase_cost = db.Column(db.Float, default=0.0)
    location = db.Column(db.String(100))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def used_percent(self):
        if self.initial_weight_g <= 0:
            return 0
        return max(0, min(100, (1 - self.weight_remaining_g / self.initial_weight_g) * 100))


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(50))
    email = db.Column(db.String(200))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Quote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quote_number = db.Column(db.String(50), unique=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    client = db.relationship('Client', backref='quotes')
    status = db.Column(db.String(50), default='Draft')
    total = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    digital_signature_enabled = db.Column(db.Boolean, default=False)
    payment_method = db.Column(db.String(200), default='Bank Transfer')
    surcharge_percent = db.Column(db.Float, default=0.0)
    surcharge_overrides = db.Column(db.Text, default='{}')
    markup_percent = db.Column(db.Float, default=0.0)
    show_markup_to_client = db.Column(db.Boolean, default=False)
    version = db.Column(db.String(20), default='1')
    version_history = db.Column(db.Text)
    valid_until = db.Column(db.Date)
    upload_token = db.Column(db.String(64), unique=True, index=True)
    signed_copy_filename = db.Column(db.String(300))
    signed_copy_uploaded_at = db.Column(db.DateTime)
    notify_me = db.Column(db.Boolean, default=False)
    items = db.relationship('QuoteItem', backref='quote', lazy=True, cascade='all, delete-orphan')

    @property
    def display_number(self):
        return self.quote_number or f'Q-{self.id:04d}'

    @property
    def subtotal(self):
        return sum(item.unit_price for item in self.items)

    @property
    def is_expired(self):
        from datetime import date
        return bool(self.valid_until) and self.valid_until < date.today() and self.status in ('Draft', 'Sent')

    @property
    def surcharge_map(self):
        import json
        try:
            return json.loads(self.surcharge_overrides or '{}')
        except (ValueError, TypeError):
            return {}

    @property
    def originating_request(self):
        """The public order-form Request this quote was converted from, if any."""
        return Request.query.filter_by(quote_id=self.id).first()


def _line_item_detail(item):
    """Human-readable summary of a line item, used in both the UI and the PDF."""
    if item.item_type == 'flat':
        qty = item.quantity if item.quantity is not None else 1
        return f'Qty {qty:g} × ${item.rate or 0:,.2f}'
    if item.item_type == 'hourly':
        hrs = item.quantity if item.quantity is not None else 0
        return f'{hrs:g}h × ${item.rate or 0:,.2f}/hr'
    parts = []
    if item.weight_g:
        label = f'{item.weight_g:.0f}g'
        if item.filament:
            label += f' {item.filament.name}'
        parts.append(label)
    if item.print_time_hours:
        parts.append(f'{item.print_time_hours:.1f}h print')
    if item.hardware_cost:
        parts.append(f'${item.hardware_cost:,.2f} hardware')
    return ' · '.join(parts) if parts else '—'


class QuoteItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'))
    description = db.Column(db.String(200))
    item_type = db.Column(db.String(20), default='print')  # 'print', 'flat', 'hourly'
    weight_g = db.Column(db.Float, default=0.0)
    filament_id = db.Column(db.Integer, db.ForeignKey('filament.id'))
    filament = db.relationship('Filament')
    hardware_cost = db.Column(db.Float, default=0.0)
    print_time_hours = db.Column(db.Float, default=0.0)
    quantity = db.Column(db.Float, default=1.0)
    rate = db.Column(db.Float, default=0.0)
    unit_price = db.Column(db.Float, default=0.0)

    @property
    def detail_line(self):
        return _line_item_detail(self)


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    client = db.relationship('Client', backref='invoices')
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'))
    quote = db.relationship('Quote')
    status = db.Column(db.String(50), default='Draft')
    total = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(200), default='Bank Transfer')
    surcharge_percent = db.Column(db.Float, default=0.0)
    surcharge_overrides = db.Column(db.Text, default='{}')
    markup_percent = db.Column(db.Float, default=0.0)
    show_markup_to_client = db.Column(db.Boolean, default=False)
    notify_me = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.Date)
    paid_at = db.Column(db.DateTime)
    archived = db.Column(db.Boolean, default=False)
    items = db.relationship('InvoiceItem', backref='invoice', lazy=True, cascade='all, delete-orphan')

    @property
    def display_number(self):
        return self.invoice_number or f'INV-{self.id:04d}'

    @property
    def subtotal(self):
        return sum(item.unit_price for item in self.items)

    @property
    def is_overdue(self):
        from datetime import date
        return bool(self.due_date) and self.due_date < date.today() and self.status not in ('Paid', 'Cancelled')

    @property
    def surcharge_map(self):
        import json
        try:
            return json.loads(self.surcharge_overrides or '{}')
        except (ValueError, TypeError):
            return {}


class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'))
    description = db.Column(db.String(200))
    item_type = db.Column(db.String(20), default='print')  # 'print', 'flat', 'hourly'
    weight_g = db.Column(db.Float, default=0.0)
    filament_id = db.Column(db.Integer, db.ForeignKey('filament.id'))
    filament = db.relationship('Filament')
    hardware_cost = db.Column(db.Float, default=0.0)
    print_time_hours = db.Column(db.Float, default=0.0)
    quantity = db.Column(db.Float, default=1.0)
    rate = db.Column(db.Float, default=0.0)
    unit_price = db.Column(db.Float, default=0.0)

    @property
    def detail_line(self):
        return _line_item_detail(self)


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_number = db.Column(db.String(50), unique=True)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'))
    quote = db.relationship('Quote', backref='jobs')
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    client = db.relationship('Client', backref='jobs')
    title = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(50), default='Queued')
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    notify_me = db.Column(db.Boolean, default=False)
    notify_sent_at = db.Column(db.DateTime)
    model_source = db.Column(db.String(20))  # 'has_model' | 'need_design' | None (internal job)
    order_details = db.Column(db.Text)  # JSON blob: files, links, materials, description, shipping address
    archived = db.Column(db.Boolean, default=False)

    @property
    def display_number(self):
        return self.job_number or f'JOB-{self.id:04d}'

    @property
    def order_data(self):
        import json
        try:
            return json.loads(self.order_details or '{}')
        except (ValueError, TypeError):
            return {}

    @property
    def originating_request(self):
        """The public order-form Request that led to this job, if any (via its quote)."""
        if not self.quote_id:
            return None
        return Request.query.filter_by(quote_id=self.quote_id).first()


class Request(db.Model):
    """A raw submission from the public order form, awaiting triage. The business
    reviews it here and converts it into a Quote (which can then flow into a Job and
    Invoice through the existing pipeline) once they're ready to price it up.
    """
    id = db.Column(db.Integer, primary_key=True)
    request_number = db.Column(db.String(50), unique=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    client = db.relationship('Client', backref='requests')
    status = db.Column(db.String(50), default='New')  # New, Reviewed, Converted, Archived
    model_source = db.Column(db.String(20))  # 'has_model' | 'need_design'
    order_details = db.Column(db.Text)
    notify_me = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    quote_id = db.Column(db.Integer, db.ForeignKey('quote.id'))
    quote = db.relationship('Quote', foreign_keys=[quote_id])

    @property
    def display_number(self):
        return self.request_number or f'REQ-{self.id:04d}'

    @property
    def title(self):
        name = self.client.name if self.client else 'a customer'
        if self.model_source == 'need_design':
            return f'Design request from {name}'
        return f'Print order from {name}'

    @property
    def order_data(self):
        import json
        try:
            return json.loads(self.order_details or '{}')
        except (ValueError, TypeError):
            return {}

    @property
    def tracking_status(self):
        """Customer-facing status shown on the public tracking page."""
        if self.status == 'Archived':
            return 'Archived'
        if self.status == 'Converted' and self.quote:
            if self.quote.jobs:
                return self.quote.jobs[0].status
            return 'Quote Sent'
        if self.status == 'Reviewed':
            return 'Being Reviewed'
        return 'Received'
