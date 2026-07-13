"""Lightweight schema migration for SQLite — adds missing columns without Alembic."""
from sqlalchemy import inspect, text


def run_migrations(db):
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()

    column_migrations = {
        'business_settings': {
            'phone': 'VARCHAR(50)',
            'terms_of_service': 'TEXT',
            'hourly_rate': 'FLOAT DEFAULT 0',
            'pay_id': 'VARCHAR(200)',
            'bank_name': 'VARCHAR(200)',
            'bank_account_name': 'VARCHAR(200)',
            'bank_bsb': 'VARCHAR(20)',
            'bank_account_number': 'VARCHAR(50)',
            'paypal_email': 'VARCHAR(200)',
            'stripe_link': 'VARCHAR(500)',
            'surcharge_bank_transfer': 'FLOAT DEFAULT 0',
            'surcharge_pay_id': 'FLOAT DEFAULT 0',
            'surcharge_cash': 'FLOAT DEFAULT 0',
            'surcharge_eft': 'FLOAT DEFAULT 0',
            'surcharge_credit_card': 'FLOAT DEFAULT 0',
            'surcharge_stripe': 'FLOAT DEFAULT 0',
            'quote_email_subject': 'VARCHAR(300)',
            'quote_email_body_html': 'TEXT',
            'invoice_email_subject': 'VARCHAR(300)',
            'invoice_email_body_html': 'TEXT',
            'turnstile_site_key': 'VARCHAR(200)',
            'turnstile_secret_key': 'VARCHAR(200)',
            'payment_terms_font_size': 'FLOAT DEFAULT 9',
            'tos_font_size': 'FLOAT DEFAULT 8',
        },
        'filament': {
            'brand': 'VARCHAR(100)',
            'price_per_kg': 'FLOAT DEFAULT 0',
            'notes': 'TEXT',
            'created_at': 'DATETIME',
        },
        'invoice': {
            'notes': 'TEXT',
            'due_date': 'DATE',
            'paid_at': 'DATETIME',
            'surcharge_overrides': "TEXT DEFAULT '{}'",
            'markup_percent': 'FLOAT DEFAULT 0',
            'show_markup_to_client': 'BOOLEAN DEFAULT 0',
            'notify_me': 'BOOLEAN DEFAULT 0',
            'archived': 'BOOLEAN DEFAULT 0',
        },
        'quote': {
            'valid_until': 'DATE',
            'surcharge_overrides': "TEXT DEFAULT '{}'",
            'markup_percent': 'FLOAT DEFAULT 0',
            'show_markup_to_client': 'BOOLEAN DEFAULT 0',
            'version': "VARCHAR(20) DEFAULT '1'",
            'version_history': 'TEXT',
            'upload_token': 'VARCHAR(64)',
            'signed_copy_filename': 'VARCHAR(300)',
            'signed_copy_uploaded_at': 'DATETIME',
            'notify_me': 'BOOLEAN DEFAULT 0',
        },
        'job': {
            'notify_me': 'BOOLEAN DEFAULT 0',
            'notify_sent_at': 'DATETIME',
            'model_source': 'VARCHAR(20)',
            'order_details': 'TEXT',
            'archived': 'BOOLEAN DEFAULT 0',
        },
        'quote_item': {
            'item_type': "VARCHAR(20) DEFAULT 'print'",
            'quantity': 'FLOAT DEFAULT 1',
            'rate': 'FLOAT DEFAULT 0',
        },
        'invoice_item': {
            'item_type': "VARCHAR(20) DEFAULT 'print'",
            'quantity': 'FLOAT DEFAULT 1',
            'rate': 'FLOAT DEFAULT 0',
        },
    }

    for table, columns in column_migrations.items():
        if table not in existing_tables:
            continue
        existing_cols = {c['name'] for c in inspector.get_columns(table)}
        for col_name, col_type in columns.items():
            if col_name not in existing_cols:
                db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {col_name} {col_type}'))
    db.session.commit()

    # Backfill price_per_kg from charge_per_gram where missing
    if 'filament' in existing_tables:
        db.session.execute(text(
            'UPDATE filament SET price_per_kg = charge_per_gram * 1000 '
            'WHERE (price_per_kg IS NULL OR price_per_kg = 0) AND charge_per_gram > 0'
        ))
        db.session.commit()

    # Backfill upload tokens for existing quotes so the signed-copy link works retroactively
    if 'quote' in existing_tables:
        import secrets
        from app.models import Quote
        for quote in Quote.query.filter(
            (Quote.upload_token.is_(None)) | (Quote.upload_token == '')
        ).all():
            quote.upload_token = secrets.token_hex(32)
        db.session.commit()
