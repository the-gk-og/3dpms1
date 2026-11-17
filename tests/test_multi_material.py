"""Per-object materials, multi-material / clean-supports pricing, the customer order
confirmation email, and the estimate stored with an order."""
import io
import json
import os
import tempfile
import zipfile

import pytest

from app import create_app, db
from app.estimate import combined_estimate, describe_combined
from app.mesh import analyse_mesh
from app.models import BusinessSettings, Filament, FilamentSpool, Request
from tests.test_estimate import CUBE_TRIS, CUBE_VERTS, cube_binary_stl


def two_object_3mf():
    """A 3MF with two separate build items: a 10 mm cube ("Body") and a 20 mm cube
    ("Lid", 8000 mm³) — the second via a component in another model part, the way
    Bambu Studio lays out its files."""
    verts = ''.join('<vertex x="%g" y="%g" z="%g"/>' % v for v in CUBE_VERTS)
    tris = ''.join('<triangle v1="%d" v2="%d" v3="%d"/>' % t for t in CUBE_TRIS)
    ns = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" ' \
         'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
    sub = (f'<?xml version="1.0"?><model unit="millimeter" {ns}><resources>'
           f'<object id="1" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>'
           f'</resources><build/></model>')
    root = (f'<?xml version="1.0"?><model unit="millimeter" {ns}><resources>'
            f'<object id="1" name="Body" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>'
            f'<object id="2" name="Lid" type="model"><components>'
            f'<component p:path="/3D/Objects/object_2.model" objectid="1" transform="2 0 0 0 2 0 0 0 2 30 0 0"/>'
            f'</components></object></resources>'
            f'<build><item objectid="1"/><item objectid="2"/></build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Target="/3D/3dmodel.model" Id="r" '
                    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        zf.writestr('3D/3dmodel.model', root)
        zf.writestr('3D/Objects/object_2.model', sub)
    return buf.getvalue()


def test_3mf_objects_are_reported_separately():
    info = analyse_mesh(two_object_3mf(), 'set.3mf')
    assert [o.name for o in info.objects] == ['Body', 'Lid']
    assert info.objects[0].volume_mm3 == pytest.approx(1000, rel=1e-4)
    assert info.objects[1].volume_mm3 == pytest.approx(8000, rel=1e-4)
    assert info.objects[1].dimensions == pytest.approx((20, 20, 20))
    assert info.volume_mm3 == pytest.approx(9000, rel=1e-4)


def test_stl_is_one_object_named_after_the_file():
    info = analyse_mesh(cube_binary_stl(), 'bracket.stl')
    assert [o.name for o in info.objects] == ['bracket']


# --- pricing -------------------------------------------------------------------------

class _B:
    hourly_rate = 5.0
    estimate_fill_factor = 0.35
    estimate_grams_per_hour = 12.0
    estimate_max_mm = 256.0
    estimate_mm_waste_pct = 10.0
    estimate_mm_time_pct = 20.0
    estimate_support_pct = 5.0


class _F:
    def __init__(self, id, name, material, price):
        self.id, self.name, self.material, self.color = id, name, material, ''
        self.sell_price_per_kg = price


PLA = _F(1, 'PLA Black', 'PLA', 40.0)
PETG = _F(2, 'PETG Clear', 'PETG', 50.0)
PARTS = [{'name': 'Body', 'volume_cm3': 10.0, 'filament_id': 1},
         {'name': 'Lid', 'volume_cm3': 8.0, 'filament_id': 2}]


def test_each_object_is_priced_in_its_own_material():
    est = combined_estimate(PARTS, [PLA, PETG], _B())
    body, lid = est['lines']
    assert body['filament'] == 'PLA Black' and body['grams'] == pytest.approx(10 * 1.24 * 0.35, abs=0.1)
    assert lid['filament'] == 'PETG Clear' and lid['grams'] == pytest.approx(8 * 1.27 * 0.35, abs=0.1)
    material = body['grams'] / 1000 * 40 + lid['grams'] / 1000 * 50
    hours = (body['grams'] + lid['grams']) / 12
    price = material + hours * 5
    assert est['low'] <= price <= est['high']
    assert not est['multi_material']
    assert any('printed separately' in w for w in est['warnings'])


def test_multi_material_adds_waste_and_time():
    plain = combined_estimate(PARTS, [PLA, PETG], _B())
    multi = combined_estimate(PARTS, [PLA, PETG], _B(), multi_material=True)
    assert multi['multi_material'] and multi['hours'] == pytest.approx(plain['hours'] * 1.2, abs=0.11)
    assert multi['waste_grams'] > 0 and multi['high'] > plain['high']
    assert not any('printed separately' in w for w in multi['warnings'])


def test_clean_supports_add_support_material_and_count_as_multi_material():
    est = combined_estimate(PARTS, [PLA, PETG], _B(), clean_supports=True, support_filament=PETG)
    model_grams = sum(l['grams'] for l in est['lines'])
    assert est['support']['filament'] == 'PETG Clear'
    assert est['support']['grams'] == pytest.approx(model_grams * 0.05, abs=0.1)
    assert est['multi_material'] and est['clean_supports']
    assert 'PETG Clear supports' in describe_combined(est)


def test_clean_supports_without_a_support_material_defers_to_quote():
    est = combined_estimate(PARTS, [PLA, PETG], _B(), clean_supports=True, support_filament=None)
    assert est['support'] is None
    assert any('priced in your quote' in w for w in est['warnings'])


def test_unchosen_or_unknown_material_falls_back_to_pla():
    est = combined_estimate([{'name': 'X', 'volume_cm3': 5, 'filament_id': None},
                             {'name': 'Y', 'volume_cm3': 5, 'filament_id': 999}], [PETG, PLA], _B())
    assert [l['filament'] for l in est['lines']] == ['PLA Black', 'PLA Black']
    assert not any(l['chosen'] for l in est['lines'])


def test_garbage_volumes_are_ignored_safely():
    est = combined_estimate([{'name': 'X', 'volume_cm3': 'nan', 'filament_id': 1},
                             {'name': 'Y', 'volume_cm3': -5, 'filament_id': 1}], [PLA], _B())
    assert est['ok'] and est['grams'] == 0


# --- routes --------------------------------------------------------------------------

@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}'})
    with app.app_context():
        db.create_all()
    with app.test_client() as c:
        yield c
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


def _setup(client, enabled=True):
    with client.application.app_context():
        pla = Filament(name='PLA Black', material='PLA', color='Black', price_per_kg=40.0)
        petg = Filament(name='PETG Clear', material='PETG', color='Clear', price_per_kg=50.0)
        db.session.add_all([pla, petg])
        db.session.flush()
        db.session.add_all([FilamentSpool(filament_id=pla.id, weight_remaining_g=800),
                            FilamentSpool(filament_id=petg.id, weight_remaining_g=500)])
        db.session.add(BusinessSettings(name='Acme 3D', hourly_rate=5.0, instant_estimate_enabled=enabled,
                                        contact_email='owner@example.test', estimate_support_filament_id=petg.id))
        db.session.commit()
        return pla.id, petg.id


def test_estimate_endpoint_lists_objects(client):
    _setup(client)
    data = client.post('/order/estimate', data={'file': (io.BytesIO(two_object_3mf()), 'set.3mf')},
                       content_type='multipart/form-data').get_json()
    assert [o['name'] for o in data['objects']] == ['Body', 'Lid']
    assert data['objects'][1]['volume_cm3'] == pytest.approx(8.0, rel=1e-3)


def test_total_endpoint(client):
    pla, petg = _setup(client)
    est = client.post('/order/estimate/total', json={
        'parts': [{'name': 'Body', 'volume_cm3': 1, 'filament_id': pla},
                  {'name': 'Lid', 'volume_cm3': 8, 'filament_id': petg}],
        'multi_material': True, 'clean_supports': True}).get_json()
    assert est['ok'] and est['multi_material'] and est['support']['filament'] == 'PETG Clear'
    assert client.post('/order/estimate/total', json={'parts': 'nope'}).status_code == 400


def test_total_endpoint_404_when_disabled(client):
    _setup(client, enabled=False)
    assert client.post('/order/estimate/total', json={'parts': []}).status_code == 404


def _submit(client, pla, petg, monkeypatch, **extra):
    sent = []
    import app.public as public
    monkeypatch.setattr(public, 'send_plain_email',
                        lambda business, to, subject, text, html_body=None: sent.append((to, subject, html_body)))
    monkeypatch.setattr(public, 'notify_admin_new_submission',
                        lambda business, subject, body_text, html_body=None: sent.append(('admin', subject, html_body)))
    form = {
        'name': 'Ava Thompson', 'phone': '0400000000', 'email': 'ava@example.test',
        'model_source': 'has_model', 'notify_me': 'on',
        'model_files': [(io.BytesIO(two_object_3mf()), 'set.3mf'), (io.BytesIO(cube_binary_stl()), 'peg.stl')],
        # file 0 = set.3mf (Body, Lid), file 1 = peg.stl
        'part_material_0_0': str(pla), 'part_material_0_1': str(petg), 'part_material_1_0': str(petg),
    }
    form.update(extra)
    resp = client.post('/order', data=form, content_type='multipart/form-data')
    assert resp.status_code == 200 and b'Order received' in resp.data
    return sent


def test_order_stores_server_side_estimate_and_options(client, monkeypatch):
    pla, petg = _setup(client)
    _submit(client, pla, petg, monkeypatch, multi_material='on', clean_supports='on')
    with client.application.app_context():
        details = json.loads(Request.query.one().order_details)
    assert details['multi_material'] and details['clean_supports']
    est = details['estimate']
    assert [(l['name'], l['filament']) for l in est['lines']] == [
        ('Body', 'PLA Black'), ('Lid', 'PETG Clear'), ('peg', 'PETG Clear')]
    assert est['support']['filament'] == 'PETG Clear' and est['multi_material']
    # the uploaded files were still saved after being analysed
    assert len(details['model_files']) == 2


def test_customer_gets_confirmation_email_with_estimate(client, monkeypatch):
    pla, petg = _setup(client)
    sent = _submit(client, pla, petg, monkeypatch)
    customer = [s for s in sent if s[0] == 'ava@example.test']
    admin = [s for s in sent if s[0] == 'admin']
    assert len(customer) == 1 and len(admin) == 1
    to, subject, html = customer[0]
    assert 'REF-' in subject
    assert 'Rough estimate' in html and 'PLA Black' in html and 'Lid' in html
    assert 'Instant estimate' in admin[0][2]


def test_confirmation_email_without_estimate_when_disabled(client, monkeypatch):
    pla, petg = _setup(client, enabled=False)
    sent = _submit(client, pla, petg, monkeypatch)
    to, subject, html = [s for s in sent if s[0] == 'ava@example.test'][0]
    assert 'Rough estimate' not in html and 'set.3mf' in html
    with client.application.app_context():
        assert 'estimate' not in json.loads(Request.query.one().order_details)


def test_request_page_shows_estimate_and_preview(client, monkeypatch):
    from app.models import User
    pla, petg = _setup(client)
    _submit(client, pla, petg, monkeypatch, multi_material='on')
    with client.application.app_context():
        u = User(username='admin', email='admin@example.test')
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
        req_id = Request.query.one().id
    client.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
    html = client.get(f'/dash/requests/{req_id}').get_data(as_text=True)
    assert 'Instant Estimate Shown to Customer' in html and 'Multi-material' in html
    assert 'req-preview-btn' in html and 'model-viewer.js' in html


# --- per-part fee ----------------------------------------------------------------------

class _BFee(_B):
    estimate_per_part_fee = 2.5


def test_per_part_fee_is_charged_once_per_object():
    plain = combined_estimate(PARTS, [PLA, PETG], _B())
    fee = combined_estimate(PARTS, [PLA, PETG], _BFee())
    assert plain['part_fee'] is None
    assert fee['part_fee'] == {'count': 2, 'each': 2.5, 'total': 5.0}
    assert fee['high'] > plain['high'] and fee['low'] >= plain['low'] + 3  # +$5 before the ±25% range
    assert '2 × $2.50 per-part fee' in describe_combined(fee)


def test_bad_per_part_fee_is_ignored():
    class _BBad(_B):
        estimate_per_part_fee = float('nan')
    assert combined_estimate(PARTS, [PLA, PETG], _BBad())['part_fee'] is None


def test_per_part_fee_saved_with_order_and_shown_in_emails(client, monkeypatch):
    pla, petg = _setup(client)
    with client.application.app_context():
        BusinessSettings.query.first().estimate_per_part_fee = 3.0
        db.session.commit()
    sent = _submit(client, pla, petg, monkeypatch)
    with client.application.app_context():
        est = json.loads(Request.query.one().order_details)['estimate']
    assert est['part_fee'] == {'count': 3, 'each': 3.0, 'total': 9.0}  # Body, Lid, peg
    customer_html = [s for s in sent if s[0] == 'ava@example.test'][0][2]
    assert '3 × $3.00 per-part fee' in customer_html


def test_per_part_fee_setting_saves(client):
    from app.models import User
    _setup(client)
    with client.application.app_context():
        u = User(username='admin', email='admin@example.test')
        u.set_password('password123')
        db.session.add(u)
        db.session.commit()
    client.post('/dash/auth/login', data={'username': 'admin', 'password': 'password123'})
    client.post('/dash/settings/save', data={'tab': 'business', 'name': 'Acme 3D', 'estimate_per_part_fee': '4.5',
                                              'instant_estimate_enabled': 'on'})
    with client.application.app_context():
        assert BusinessSettings.query.first().estimate_per_part_fee == 4.5
