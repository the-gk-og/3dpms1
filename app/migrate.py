"""Lightweight schema migration for SQLite — adds missing columns without Alembic."""
from sqlalchemy import inspect, text


def run_migrations(db):
    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()

    column_migrations = {
        'business_settings': {
            'phone': 'VARCHAR(50)',
            'abn': 'VARCHAR(20)',
            'terms_of_service': 'TEXT',
            'privacy_policy': 'TEXT',
            'hourly_rate': 'FLOAT DEFAULT 0',
            'pay_id': 'VARCHAR(200)',
            'bank_name': 'VARCHAR(200)',
            'bank_account_name': 'VARCHAR(200)',
            'bank_bsb': 'VARCHAR(20)',
            'bank_account_number': 'VARCHAR(50)',
            'paypal_email': 'VARCHAR(200)',
            'stripe_link': 'VARCHAR(500)',
            'stripe_secret_key': 'VARCHAR(500)',
            'stripe_webhook_secret': 'VARCHAR(500)',
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
            'job_complete_email_subject': 'VARCHAR(300)',
            'job_complete_email_body_html': 'TEXT',
            'overdue_reminder_email_subject': 'VARCHAR(300)',
            'overdue_reminder_email_body_html': 'TEXT',
            'contact_notification_email_subject': 'VARCHAR(300)',
            'contact_notification_email_body_html': 'TEXT',
            'order_notification_email_subject': 'VARCHAR(300)',
            'order_notification_email_body_html': 'TEXT',
            'invoice_paid_notification_email_subject': 'VARCHAR(300)',
            'invoice_paid_notification_email_body_html': 'TEXT',
            'turnstile_site_key': 'VARCHAR(200)',
            'turnstile_secret_key': 'VARCHAR(200)',
            'payment_terms_font_size': 'FLOAT DEFAULT 9',
            'tos_font_size': 'FLOAT DEFAULT 8',
            'google_oauth_client_id': 'VARCHAR(300)',
            'google_oauth_client_secret': 'VARCHAR(500)',
        },
        'filament': {
            'brand': 'VARCHAR(100)',
            'price_per_kg': 'FLOAT DEFAULT 0',
            'notes': 'TEXT',
            'created_at': 'DATETIME',
        },
        'invoice': {
            'notes': 'TEXT',
            'internal_notes': 'TEXT',
            'pay_token': 'VARCHAR(64)',
            'due_date': 'DATE',
            'paid_at': 'DATETIME',
            'last_reminder_sent_at': 'DATETIME',
            'surcharge_overrides': "TEXT DEFAULT '{}'",
            'markup_percent': 'FLOAT DEFAULT 0',
            'show_markup_to_client': 'BOOLEAN DEFAULT 0',
            'notify_me': 'BOOLEAN DEFAULT 0',
            'archived': 'BOOLEAN DEFAULT 0',
            'stripe_enabled': 'BOOLEAN DEFAULT 1',
            'reference_number': 'VARCHAR(50)',
        },
        'quote': {
            'valid_until': 'DATE',
            'internal_notes': 'TEXT',
            'surcharge_overrides': "TEXT DEFAULT '{}'",
            'markup_percent': 'FLOAT DEFAULT 0',
            'show_markup_to_client': 'BOOLEAN DEFAULT 0',
            'version': "VARCHAR(20) DEFAULT '1'",
            'version_history': 'TEXT',
            'upload_token': 'VARCHAR(64)',
            'signed_copy_filename': 'VARCHAR(300)',
            'signed_copy_uploaded_at': 'DATETIME',
            'notify_me': 'BOOLEAN DEFAULT 0',
            'archived': 'BOOLEAN DEFAULT 0',
            'reference_number': 'VARCHAR(50)',
        },
        'job': {
            'notify_me': 'BOOLEAN DEFAULT 0',
            'internal_notes': 'TEXT',
            'notify_sent_at': 'DATETIME',
            'model_source': 'VARCHAR(20)',
            'order_details': 'TEXT',
            'archived': 'BOOLEAN DEFAULT 0',
            'reference_number': 'VARCHAR(50)',
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
        'user': {
            'totp_secret': 'TEXT',
            'google_sub': 'VARCHAR(255)',
        },
        'request': {
            'reference_number': 'VARCHAR(50)',
            'internal_notes': 'TEXT',
        },
        'feedback_survey': {
            'respondent_name': 'VARCHAR(200)',
            'respondent_email': 'VARCHAR(200)',
            'would_order_again': 'BOOLEAN',
            'print_quality_rating': 'INTEGER',
            'communication_rating': 'INTEGER',
            'turnaround_rating': 'INTEGER',
            'value_rating': 'INTEGER',
            'customer_service_rating': 'INTEGER',
            'referral_source': 'VARCHAR(100)',
            'improvements': 'TEXT',
            'testimonial_ok': 'BOOLEAN',
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

    # Backfill pay tokens for existing invoices so the Stripe payment link works retroactively
    if 'invoice' in existing_tables:
        import secrets
        from app.models import Invoice
        for invoice in Invoice.query.filter(
            (Invoice.pay_token.is_(None)) | (Invoice.pay_token == '')
        ).all():
            invoice.pay_token = secrets.token_hex(32)
        db.session.commit()

    # Backfill reference numbers for existing pipeline rows created before this
    # feature existed, so REF-xxxx ties old requests/quotes/jobs/invoices together
    # retroactively instead of leaving them blank. Requests go first (they're the
    # earliest possible origin point), then quotes inherit from their originating
    # request if any, then jobs/invoices inherit from their quote.
    if 'request' in existing_tables and 'quote' in existing_tables:
        from app.models import Request as OrderRequest, Quote, Job, Invoice
        from app.helpers import generate_reference_number

        for req in OrderRequest.query.filter(
            (OrderRequest.reference_number.is_(None)) | (OrderRequest.reference_number == '')
        ).order_by(OrderRequest.created_at).all():
            req.reference_number = generate_reference_number()
            db.session.commit()

        for quote in Quote.query.filter(
            (Quote.reference_number.is_(None)) | (Quote.reference_number == '')
        ).order_by(Quote.created_at).all():
            origin = OrderRequest.query.filter_by(quote_id=quote.id).first()
            quote.reference_number = (
                origin.reference_number if origin and origin.reference_number
                else generate_reference_number()
            )
            db.session.commit()

        for job in Job.query.filter(
            (Job.reference_number.is_(None)) | (Job.reference_number == '')
        ).order_by(Job.created_at).all():
            job.reference_number = (
                job.quote.reference_number if job.quote and job.quote.reference_number
                else generate_reference_number()
            )
            db.session.commit()

        for invoice in Invoice.query.filter(
            (Invoice.reference_number.is_(None)) | (Invoice.reference_number == '')
        ).order_by(Invoice.created_at).all():
            invoice.reference_number = (
                invoice.quote.reference_number if invoice.quote and invoice.quote.reference_number
                else generate_reference_number()
            )
            db.session.commit()

    _encrypt_legacy_secrets(db, existing_tables)


def _encrypt_legacy_secrets(db, existing_tables):
    """One-time pass that encrypts any plaintext smtp_password / turnstile_secret_key
    values left over from before field-level encryption was introduced. Done here via
    raw SQL rather than relying on the ORM to notice the field needs re-saving — if a
    legacy value is later assigned back to itself unchanged (e.g. saving the Settings
    form without touching that field), SQLAlchemy's dirty-tracking sees no diff and
    silently skips the UPDATE, so it would otherwise never get encrypted.
    """
    if 'business_settings' not in existing_tables:
        return
    from cryptography.fernet import InvalidToken
    from app.crypto import _get_fernet

    fernet = _get_fernet()
    rows = db.session.execute(
        text('SELECT id, smtp_password, turnstile_secret_key FROM business_settings')
    ).fetchall()
    for row_id, smtp_password, turnstile_secret_key in rows:
        updates = {}
        for column, value in (('smtp_password', smtp_password), ('turnstile_secret_key', turnstile_secret_key)):
            if not value:
                continue
            try:
                fernet.decrypt(value.encode('utf-8'))
            except (InvalidToken, ValueError):
                # Doesn't decrypt -> legacy plaintext -> encrypt it now.
                updates[column] = fernet.encrypt(value.encode('utf-8')).decode('utf-8')
        if updates:
            set_clause = ', '.join(f'{col} = :{col}' for col in updates)
            updates['row_id'] = row_id
            db.session.execute(text(f'UPDATE business_settings SET {set_clause} WHERE id = :row_id'), updates)
    db.session.commit()
