import os
import re
import tempfile
from datetime import datetime

import pytest

from app import create_app, db
from app.models import BusinessSettings, User, Client, Invoice, Payment
from app.payments import (
    record_payment, delete_payment, record_stripe_payment, record_stripe_checkout,
    checkout_amount, deposit_offered, payment_choice_needed,
)


@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}'})
    with app.app_context():
        db.create_all()
        db.session.add(BusinessSettings(name='Acme 3D'))
        user = User(username='admin', email='admin@example.com')
        user.set_password('password123')
        db.session.add(user)
        db.session.commit()
    with app.test_client() as test_client:
        test_client.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
        yield test_client
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


def _invoice(total=200.0, status='Sent', deposit_percent=0.0, number='INV-1'):
    c = Client(name='Ava Thompson', email='ava@example.com')
    db.session.add(c)
    db.session.flush()
    inv = Invoice(invoice_number=number, client_id=c.id, total=total, status=status,
                  deposit_percent=deposit_percent)
    db.session.add(inv)
    db.session.commit()
    return inv


def test_partial_then_full_payment_flips_status_and_paid_at(client):
    with client.application.app_context():
        inv = _invoice(200)
        record_payment(inv, 50, method='Cash', paid_at=datetime(2026, 3, 1))
        assert inv.status == 'Sent'
        assert inv.paid_at is None
        assert inv.amount_paid == 50
        assert inv.balance_due == 150
        assert inv.is_partially_paid

        record_payment(inv, 150, method='Bank Transfer', paid_at=datetime(2026, 3, 9))
        assert inv.status == 'Paid'
        assert inv.paid_at == datetime(2026, 3, 9)
        assert inv.balance_due == 0
        assert not inv.is_partially_paid


def test_deleting_payment_reverts_to_sent(client):
    with client.application.app_context():
        inv = _invoice(100)
        record_payment(inv, 40, paid_at=datetime(2026, 3, 1))
        final = record_payment(inv, 60, paid_at=datetime(2026, 3, 2))
        assert inv.status == 'Paid'
        delete_payment(final)
        inv = db.session.get(Invoice, inv.id)
        assert inv.status == 'Sent'
        assert inv.paid_at is None
        assert inv.balance_due == 60


def test_manually_paid_invoice_without_payments_is_untouched(client):
    with client.application.app_context():
        inv = _invoice(80, status='Paid')
        assert inv.amount_paid == 0
        assert inv.balance_due == 0
        assert not inv.is_partially_paid


def test_dashboard_outstanding_uses_balance_due(client):
    with client.application.app_context():
        part = _invoice(300, number='INV-A')
        _invoice(100, number='INV-B')
        _invoice(999, status='Paid', number='INV-C')
        record_payment(part, 120)
    resp = client.get('/dash/')
    assert resp.status_code == 200
    # 180 balance on the part-paid one + 100 untouched = 280
    assert b'$280.00' in resp.data


def test_record_and_delete_via_routes(client):
    with client.application.app_context():
        inv_id = _invoice(100).id
    resp = client.post(f'/dash/invoices/{inv_id}/payments', data={
        'amount': '30', 'method': 'Cash', 'paid_at': '2026-04-02', 'reference': 'r1',
    })
    assert resp.status_code == 302
    page = client.get(f'/dash/invoices/{inv_id}')
    assert page.status_code == 200
    assert b'Part paid' in page.data
    assert b'$70.00' in page.data
    assert b'Part paid' in client.get('/dash/invoices').data

    # Over-paying is refused.
    client.post(f'/dash/invoices/{inv_id}/payments', data={'amount': '500', 'paid_at': '2026-04-03'})
    with client.application.app_context():
        payments = Payment.query.filter_by(invoice_id=inv_id).all()
        assert [p.amount for p in payments] == [30]
        assert payments[0].created_by == 'admin'
        pid = payments[0].id
    client.post(f'/dash/invoices/{inv_id}/payments/{pid}/delete')
    with client.application.app_context():
        assert Payment.query.count() == 0


def test_deposit_amount_and_checkout_choice(client):
    with client.application.app_context():
        inv = _invoice(250, deposit_percent=30)
        assert inv.deposit_amount == 75
        assert deposit_offered(inv)
        assert payment_choice_needed(inv)
        assert checkout_amount(inv, 'deposit') == (75, 'deposit')
        assert checkout_amount(inv, 'full') == (250, 'balance')

        record_payment(inv, 75)
        assert not deposit_offered(inv)  # deposit only offered before anything is paid
        assert checkout_amount(inv, 'deposit') == (175, 'balance')

        plain = _invoice(90, number='INV-P')
        assert not payment_choice_needed(plain)
        assert checkout_amount(plain, None) == (90, 'balance')


def test_set_deposit_route(client):
    with client.application.app_context():
        inv_id = _invoice(100).id
    client.post(f'/dash/invoices/{inv_id}/deposit', data={'deposit_percent': '25'})
    with client.application.app_context():
        assert db.session.get(Invoice, inv_id).deposit_amount == 25


def test_stripe_payment_is_idempotent_on_session_id(client):
    with client.application.app_context():
        inv = _invoice(100, deposit_percent=50)
        p1, created1 = record_stripe_payment(inv, 'cs_test_fake_123', 50)
        p2, created2 = record_stripe_payment(inv, 'cs_test_fake_123', 50)
        assert created1 and not created2
        assert p1.id == p2.id
        assert Payment.query.count() == 1
        assert inv.status == 'Sent'  # deposit only — not fully covered
        assert inv.balance_due == 50

        # Balance via a session dict as the webhook sees it; delivered twice.
        session = {'id': 'cs_test_fake_456', 'metadata': {'invoice_amount': '50.00'}}
        _, created = record_stripe_checkout(inv, session)
        _, again = record_stripe_checkout(inv, session)
        assert created and not again
        assert inv.status == 'Paid'
        assert inv.paid_at is not None
        assert Payment.query.filter_by(source='stripe').count() == 2


def test_legacy_stripe_session_without_metadata_pays_in_full(client):
    with client.application.app_context():
        inv = _invoice(120)
        record_stripe_checkout(inv, {'id': 'cs_test_legacy'})
        assert inv.status == 'Paid'
        assert inv.amount_paid == 120


def test_public_pay_page_shows_choice_and_balance(client):
    with client.application.app_context():
        settings = BusinessSettings.query.first()
        settings.stripe_secret_key = 'sk_test_not_used'
        db.session.commit()
        dep = _invoice(200, deposit_percent=25, number='INV-D')
        dep_token = dep.pay_token
        part = _invoice(100, number='INV-Q')
        record_payment(part, 40)
        part_token = part.pay_token
        paid = _invoice(100, status='Paid', number='INV-Z')
        paid_token = paid.pay_token

    resp = client.get(f'/pay/{dep_token}')
    assert resp.status_code == 200
    assert b'Pay deposit ($50.00)' in resp.data
    assert b'Pay full balance ($200.00)' in resp.data

    resp = client.get(f'/pay/{part_token}')
    assert resp.status_code == 200
    assert b'Paid so far' in resp.data
    assert re.search(rb'Pay balance \(\$60\.00\)', resp.data)

    assert client.get(f'/pay/{paid_token}').status_code == 200


def test_invoice_pdf_with_payments_renders(client):
    with client.application.app_context():
        inv = _invoice(100)
        record_payment(inv, 25)
        inv_id = inv.id
    resp = client.get(f'/dash/invoices/{inv_id}/pdf/preview')
    assert resp.status_code == 200
    assert resp.data[:4] == b'%PDF'
