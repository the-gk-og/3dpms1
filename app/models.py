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
    valid_until = db.Column(db.Date)
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
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.Date)
    paid_at = db.Column(db.DateTime)
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

    @property
    def display_number(self):
        return self.job_number or f'JOB-{self.id:04d}'
