import csv
import io
import json
import os
import tempfile
from datetime import date, datetime

import pytest

from app import create_app, db
from app.models import BusinessSettings, User, Client, Invoice, InvoiceItem, Job, Filament
from app.reports import build_report, invoice_material_cost, period_bounds


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


def _filament(name='PLA Black', cost_per_kg=20.0, material='PLA'):
    f = Filament(name=name, cost_per_kg=cost_per_kg, material=material)
    db.session.add(f)
    db.session.flush()
    return f


def _paid_invoice(number, total, paid_at, client_name='Ava Thompson', lines=(), status='Paid'):
    """lines: (filament, grams, hardware_cost) tuples for print lines."""
    c = Client.query.filter_by(name=client_name).first() or Client(name=client_name)
    db.session.add(c)
    db.session.flush()
    inv = Invoice(invoice_number=number, client_id=c.id, total=total, status=status, paid_at=paid_at)
    db.session.add(inv)
    db.session.flush()
    for filament, grams, hardware in lines:
        db.session.add(InvoiceItem(invoice_id=inv.id, item_type='print', filament_id=filament.id,
                                   weight_g=grams, hardware_cost=hardware))
    db.session.flush()
    return inv


def test_cost_uses_estimated_grams_and_hardware(client):
    with client.application.app_context():
        pla = _filament(cost_per_kg=20)
        inv = _paid_invoice('I-1', 100, datetime(2026, 11, 3), lines=[(pla, 500, 4.5)])
        db.session.add(InvoiceItem(invoice_id=inv.id, item_type='flat', quantity=1, rate=10, unit_price=10))
        db.session.commit()
        cost = invoice_material_cost(inv)
        assert cost['source'] == 'estimated'
        assert cost['grams'] == 500
        assert cost['filament_cost'] == 10.0  # 500g × $20/kg
        assert cost['hardware_cost'] == 4.5
        assert cost['material_cost'] == 14.5


def test_cost_prefers_actual_grams_from_deducted_jobs(client):
    with client.application.app_context():
        pla = _filament(cost_per_kg=20)
        petg = _filament('PETG Blue', cost_per_kg=30, material='PETG')
        inv = _paid_invoice('I-1', 100, datetime(2026, 11, 3), lines=[(pla, 500, 2)])
        # The job actually used 300g PLA across two spools plus 100g PETG.
        usage = [
            {'spool_id': 1, 'filament_id': pla.id, 'filament_name': pla.name, 'grams': 200},
            {'spool_id': 2, 'filament_id': pla.id, 'filament_name': pla.name, 'grams': 100},
            {'spool_id': 3, 'filament_id': petg.id, 'filament_name': petg.name, 'grams': 100},
        ]
        db.session.add(Job(title='Bracket', invoice_id=inv.id, filament_deducted_at=datetime(2026, 11, 2),
                           filament_usage=json.dumps(usage)))
        # A job that hasn't been deducted adds nothing.
        db.session.add(Job(title='Spare', invoice_id=inv.id))
        db.session.commit()
        cost = invoice_material_cost(inv)
        assert cost['source'] == 'actual'
        assert cost['grams_by_filament'] == {pla.id: 300, petg.id: 100}
        assert cost['filament_cost'] == 9.0  # 300g × $20/kg + 100g × $30/kg
        assert cost['hardware_cost'] == 2
        assert cost['material_cost'] == 11.0


def test_period_bounds():
    today = date(2026, 1, 15)
    assert period_bounds('this_month', today) == (datetime(2026, 1, 1), datetime(2026, 2, 1))
    assert period_bounds('last_month', today) == (datetime(2025, 12, 1), datetime(2026, 1, 1))
    assert period_bounds('this_year', today) == (datetime(2026, 1, 1), datetime(2027, 1, 1))
    assert period_bounds('last_12_months', today) == (datetime(2025, 2, 1), datetime(2026, 2, 1))
    assert period_bounds('all', today) == (None, None)
    assert period_bounds('bogus', today) == period_bounds('this_month', today)


def test_report_filters_by_paid_period_and_totals(client):
    with client.application.app_context():
        pla = _filament(cost_per_kg=20)
        _paid_invoice('I-NOV', 200, datetime(2026, 11, 5), lines=[(pla, 1000, 0)])         # cost 20
        _paid_invoice('I-OCT', 50, datetime(2026, 10, 20), 'Ben Lee', lines=[(pla, 500, 5)])  # cost 15
        _paid_invoice('I-OLD', 80, datetime(2024, 3, 1))
        _paid_invoice('I-UNPAID', 999, None, status='Sent', lines=[(pla, 1000, 0)])
        db.session.commit()
        today = date(2026, 11, 17)

        nov = build_report('this_month', today)
        assert [r['number'] for r in nov['rows']] == ['I-NOV']
        assert nov['totals']['revenue'] == 200
        assert nov['totals']['cost'] == 20
        assert nov['totals']['profit'] == 180
        assert nov['totals']['margin'] == 90.0

        oct_ = build_report('last_month', today)
        assert [r['number'] for r in oct_['rows']] == ['I-OCT']

        year = build_report('this_year', today)
        assert year['totals']['count'] == 2
        assert year['totals']['average'] == 125
        assert len(year['monthly']) == 12
        assert year['by_client'][0] == ('Ava Thompson', 200)
        assert year['by_material'] == [('PLA', 1500)]
        assert [r['number'] for r in year['top']] == ['I-NOV', 'I-OCT']

        everything = build_report('all', today)
        assert everything['totals']['count'] == 3
        assert everything['monthly'][0]['month'] == date(2024, 3, 1)
        assert everything['monthly'][-1]['month'] == date(2026, 11, 1)


def test_reports_page_renders(client):
    r = client.get('/dash/reports/')
    assert r.status_code == 200
    assert b'No paid invoices' in r.data
    with client.application.app_context():
        pla = _filament()
        _paid_invoice('I-42', 120, datetime.utcnow(), lines=[(pla, 250, 1)])
        db.session.commit()
    for period in ('this_month', 'last_month', 'this_year', 'last_12_months', 'all'):
        r = client.get(f'/dash/reports/?period={period}')
        assert r.status_code == 200
    r = client.get('/dash/reports/?period=all')
    assert b'I-42' in r.data
    assert b'<svg class="rpt-chart"' in r.data
    assert b'not</strong> included' in r.data
    mobile = client.get('/dash/reports/?period=all', headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148'})
    assert mobile.status_code == 200
    assert b'I-42' in mobile.data


def test_reports_requires_login(client):
    client.get('/dash/auth/logout')
    r = client.get('/dash/reports/')
    assert r.status_code == 302


def test_csv_export(client):
    with client.application.app_context():
        pla = _filament(cost_per_kg=20)
        _paid_invoice('I-7', 100, datetime.utcnow(), '=HYPERLINK("x")', lines=[(pla, 500, 0)])
        db.session.commit()
    r = client.get('/dash/reports/export.csv?period=all')
    assert r.status_code == 200
    assert r.mimetype == 'text/csv'
    assert 'attachment' in r.headers['Content-Disposition']
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
    assert rows[0][0] == 'Invoice'
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1]))
    assert row['Invoice'] == 'I-7'
    assert row['Client'].startswith("'=")  # formula injection neutralised
    assert row['Revenue'] == '100.00'
    assert row['Material cost'] == '10.00'
    assert row['Profit'] == '90.00'
    assert row['Margin %'] == '90.0'
    assert row['Grams source'] == 'estimated'
