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
        },
        'quote': {
            'valid_until': 'DATE',
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
