"""Deposits and part payments against invoices.

An invoice can carry any number of Payment rows (recorded by staff, or by the Stripe
webhook). Once they add up to the invoice total the invoice flips to Paid, with paid_at
set to the date of the payment that completed it; deleting a payment that takes it back
under the total reopens it as Sent. Invoices with no payment rows behave exactly as they
always have (status set by hand, or by a single full Stripe payment).

Dashboard revenue still counts only fully paid invoices by paid_at — a deposit isn't
revenue until the invoice is settled — while "Outstanding" uses balance_due.
"""
from datetime import datetime

from flask import Blueprint, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Invoice, Payment
from app.helpers import PAYMENT_METHOD_DEFS, log_audit

payments_bp = Blueprint('payments', __name__, url_prefix='/dash')

# Same names the invoice's "accepted payment methods" use.
PAYMENT_METHODS = [label for _key, label in PAYMENT_METHOD_DEFS]

# Floats + cents: anything within half a cent counts as settled.
_EPSILON = 0.005


# --------------------------------------------------------------------------- core

def outstanding_amount(invoice):
    """Total minus payments, ignoring status — what a Stripe 'pay full balance' charges.
    For an invoice with no payments this is simply invoice.total (the old behaviour)."""
    return max(round((invoice.total or 0) - invoice.amount_paid, 2), 0.0)


def sync_invoice_status(invoice):
    """Derive Paid / paid_at from the invoice's payment rows. Only called after a payment
    is recorded or deleted, so invoices managed by hand are never touched."""
    paid = invoice.amount_paid
    total = invoice.total or 0
    if invoice.payments and total > 0 and paid >= total - _EPSILON:
        # Already settled (e.g. marked Paid by hand, then a late Stripe payment lands):
        # keep the original paid_at so revenue doesn't jump months.
        if invoice.status != 'Paid' or not invoice.paid_at:
            invoice.paid_at = max(p.paid_at for p in invoice.payments)
        invoice.status = 'Paid'
    elif invoice.status == 'Paid':
        invoice.status = 'Sent'
        invoice.paid_at = None


def record_payment(invoice, amount, method=None, paid_at=None, reference=None,
                   source='manual', stripe_session_id=None, created_by=None):
    """Add a payment and re-derive the invoice status. Commits."""
    payment = Payment(
        invoice=invoice, amount=round(float(amount), 2), method=method,
        paid_at=paid_at or datetime.utcnow(), reference=reference, source=source,
        stripe_session_id=stripe_session_id, created_by=created_by,
    )
    db.session.add(payment)
    db.session.flush()
    db.session.refresh(invoice)
    sync_invoice_status(invoice)
    db.session.commit()
    return payment


def delete_payment(payment):
    """Remove a payment and re-derive the invoice status. Commits."""
    invoice = payment.invoice
    db.session.delete(payment)
    db.session.flush()
    db.session.refresh(invoice)
    sync_invoice_status(invoice)
    db.session.commit()
    return invoice


def record_stripe_payment(invoice, session_id, amount, paid_at=None):
    """Record a completed Stripe Checkout Session as a payment, at most once per session.
    Returns (payment, created). Stripe retries webhooks, so a repeat is a no-op."""
    existing = Payment.query.filter_by(stripe_session_id=session_id).first()
    if existing:
        return existing, False
    try:
        payment = record_payment(
            invoice, amount, method='Stripe', paid_at=paid_at, reference=f'Stripe {session_id}',
            source='stripe', stripe_session_id=session_id, created_by='Stripe',
        )
    except IntegrityError:  # a concurrent delivery of the same event won the race
        db.session.rollback()
        return Payment.query.filter_by(stripe_session_id=session_id).first(), False
    return payment, True


def record_stripe_checkout(invoice, session):
    """Webhook entry point: the amount is the invoice portion we stored in the session
    metadata (excluding any card surcharge). Sessions created before part payments
    existed have no metadata and always charged the full remaining amount."""
    metadata = session.get('metadata') or {}
    try:
        amount = float(metadata.get('invoice_amount'))
    except (TypeError, ValueError):
        amount = outstanding_amount(invoice) or (invoice.total or 0)
    return record_stripe_payment(invoice, session.get('id'), amount)


# ------------------------------------------------------------- public pay page

def deposit_offered(invoice):
    """A deposit option is shown only before anything has been paid."""
    dep = invoice.deposit_amount
    return dep > 0 and invoice.amount_paid <= 0 and dep < (invoice.total or 0) - _EPSILON


def payment_choice_needed(invoice):
    """Show the pay page (instead of going straight to Stripe) when there's a deposit
    to choose or a part payment to show. Plain invoices redirect as before."""
    return deposit_offered(invoice) or invoice.amount_paid > 0


def checkout_amount(invoice, option):
    """(amount, kind) to charge for the client's choice: 'deposit' or the full balance."""
    if option == 'deposit' and deposit_offered(invoice):
        return invoice.deposit_amount, 'deposit'
    return outstanding_amount(invoice), 'balance'


def notify_part_payment(business, invoice, payment):
    """Best-effort heads-up to staff when a Stripe deposit / part payment lands."""
    from app.helpers import notify_admin_new_submission
    try:
        client_name = invoice.client.name if invoice.client else 'A client'
        notify_admin_new_submission(
            business,
            subject=f'Payment received on {invoice.display_number} — ${payment.amount:,.2f}',
            body_text=(
                f'{client_name} paid ${payment.amount:,.2f} by card towards invoice '
                f'{invoice.display_number}.\nPaid to date: ${invoice.amount_paid:,.2f} of '
                f'${invoice.total or 0:,.2f}. Balance due: ${invoice.balance_due:,.2f}.\n\n'
                + url_for('main.invoice_detail', invoice_id=invoice.id, _external=True)
            ),
        )
    except Exception:
        pass


# ------------------------------------------------------------- dashboard routes

def _parse_paid_date(value):
    if not value:
        return datetime.utcnow()
    try:
        return datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        return None


@payments_bp.route('/invoices/<int:invoice_id>/payments', methods=['POST'])
@login_required
def add_payment(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    back = redirect(url_for('main.invoice_detail', invoice_id=invoice.id))
    try:
        amount = round(float(request.form.get('amount') or 0), 2)
    except ValueError:
        amount = 0
    if amount <= 0:
        flash('Enter a payment amount greater than zero.')
        return back
    balance = outstanding_amount(invoice)
    if amount > balance + _EPSILON:
        flash(f'That’s more than the balance due (${balance:,.2f}).')
        return back
    paid_at = _parse_paid_date(request.form.get('paid_at'))
    if paid_at is None:
        flash('Enter a valid payment date.')
        return back
    method = request.form.get('method') or None
    if method and method not in PAYMENT_METHODS:
        method = None
    payment = record_payment(
        invoice, amount, method=method, paid_at=paid_at,
        reference=(request.form.get('reference') or '').strip()[:300] or None,
        created_by=getattr(current_user, 'username', None),
    )
    log_audit('payment_recorded', target_type='invoice', target_id=invoice.id,
              detail=f'{invoice.display_number}: ${payment.amount:,.2f} {method or ""}'.strip())
    flash(f'Payment of ${payment.amount:,.2f} recorded'
          + (' — invoice is now paid in full' if invoice.status == 'Paid' else ''))
    return back


@payments_bp.route('/invoices/<int:invoice_id>/payments/<int:payment_id>/delete', methods=['POST'])
@login_required
def remove_payment(invoice_id, payment_id):
    payment = Payment.query.filter_by(id=payment_id, invoice_id=invoice_id).first()
    if not payment:
        abort(404)
    detail = f'{payment.invoice.display_number}: ${payment.amount:,.2f} {payment.method or ""} ({payment.source})'.strip()
    delete_payment(payment)
    log_audit('payment_deleted', target_type='invoice', target_id=invoice_id, detail=detail)
    flash('Payment deleted')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice_id))


@payments_bp.route('/invoices/<int:invoice_id>/deposit', methods=['POST'])
@login_required
def set_deposit(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    try:
        pct = float(request.form.get('deposit_percent') or 0)
    except ValueError:
        pct = 0.0
    invoice.deposit_percent = min(max(pct, 0.0), 100.0)
    db.session.commit()
    flash(f'Deposit set to {invoice.deposit_percent:g}%' if invoice.deposit_percent else 'Deposit removed')
    return redirect(url_for('main.invoice_detail', invoice_id=invoice.id))


@payments_bp.app_context_processor
def _payment_template_globals():
    from datetime import date
    return {'payment_method_names': PAYMENT_METHODS, 'now_date_iso': date.today().isoformat()}
