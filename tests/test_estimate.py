import io
import os
import struct
import tempfile
import zipfile

import pytest

from app import create_app, db
from app.estimate import density_for, estimate_for_mesh, price_range
from app.mesh import MeshError, MeshInfo, analyse_mesh
from app.models import BusinessSettings, Filament, FilamentSpool

# --- A 10 mm cube, outward-facing triangles ------------------------------------------

S = 10.0
CUBE_VERTS = [(x, y, z) for x in (0, S) for y in (0, S) for z in (0, S)]
# index = x*4 + y*2 + z ; quads wound counter-clockwise seen from outside
CUBE_QUADS = [
    (0, 1, 3, 2),  # x = 0
    (4, 6, 7, 5),  # x = S
    (0, 4, 5, 1),  # y = 0
    (2, 3, 7, 6),  # y = S
    (0, 2, 6, 4),  # z = 0
    (1, 5, 7, 3),  # z = S
]
CUBE_TRIS = [t for a, b, c, d in CUBE_QUADS for t in ((a, b, c), (a, c, d))]


def cube_binary_stl(offset=(0, 0, 0)):
    out = bytearray(b'solid but actually binary'.ljust(80, b' '))
    out += struct.pack('<I', len(CUBE_TRIS))
    for tri in CUBE_TRIS:
        out += struct.pack('<3f', 0, 0, 0)
        for i in tri:
            out += struct.pack('<3f', *(CUBE_VERTS[i][k] + offset[k] for k in range(3)))
        out += b'\0\0'
    return bytes(out)


def cube_ascii_stl():
    lines = ['solid cube']
    for tri in CUBE_TRIS:
        lines += ['  facet normal 0 0 0', '    outer loop']
        lines += ['      vertex %g %g %g' % CUBE_VERTS[i] for i in tri]
        lines += ['    endloop', '  endfacet']
    lines.append('endsolid cube')
    return '\n'.join(lines).encode()


def cube_obj_quads():
    lines = ['# cube', 'o cube']
    lines += ['v %g %g %g' % v for v in CUBE_VERTS]
    lines += ['vn 0 0 1', 'vt 0 0']
    # mix of plain, v/vt/vn and negative indices
    for n, q in enumerate(CUBE_QUADS):
        if n % 3 == 0:
            lines.append('f ' + ' '.join(str(i + 1) for i in q))
        elif n % 3 == 1:
            lines.append('f ' + ' '.join(f'{i + 1}/1/1' for i in q))
        else:
            lines.append('f ' + ' '.join(str(i - len(CUBE_VERTS)) for i in q))
    return '\n'.join(lines).encode()


def cube_3mf(transform=None, unit='millimeter'):
    verts = ''.join('<vertex x="%g" y="%g" z="%g"/>' % v for v in CUBE_VERTS)
    tris = ''.join('<triangle v1="%d" v2="%d" v3="%d"/>' % t for t in CUBE_TRIS)
    tf = f' transform="{transform}"' if transform else ''
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model"><mesh><vertices>{verts}</vertices>'
        f'<triangles>{tris}</triangles></mesh></object></resources>'
        f'<build><item objectid="1"{tf}/></build></model>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', '<Types/>')
        zf.writestr('_rels/.rels',
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        zf.writestr('3D/3dmodel.model', model)
    return buf.getvalue()


def assert_cube(info, size=10.0, volume=1000.0):
    assert info.triangle_count == 12
    assert info.volume_mm3 == pytest.approx(volume, rel=1e-4)
    assert info.size_x_mm == pytest.approx(size)
    assert info.size_y_mm == pytest.approx(size)
    assert info.size_z_mm == pytest.approx(size)


# --- Parsing -------------------------------------------------------------------------

def test_binary_stl_cube():
    assert_cube(analyse_mesh(cube_binary_stl(), 'cube.stl'))


def test_binary_stl_volume_is_translation_invariant():
    assert_cube(analyse_mesh(cube_binary_stl(offset=(100, -50, 7)), 'cube.STL'))


def test_ascii_stl_cube():
    assert_cube(analyse_mesh(cube_ascii_stl(), 'cube.stl'))


def test_obj_quads_cube():
    assert_cube(analyse_mesh(cube_obj_quads(), 'cube.obj'))


def test_3mf_cube():
    assert_cube(analyse_mesh(cube_3mf(), 'cube.3mf'))


def test_3mf_build_transform_scales_and_mirrors():
    # Scale x2 in x, mirror in y (negative determinant) and translate.
    info = analyse_mesh(cube_3mf(transform='2 0 0 0 -1 0 0 0 1 5 5 5'), 'cube.3mf')
    assert info.volume_mm3 == pytest.approx(2000.0)
    assert info.dimensions == pytest.approx((20.0, 10.0, 10.0))


def test_3mf_unit_is_honoured():
    info = analyse_mesh(cube_3mf(unit='centimeter'), 'cube.3mf')
    assert info.volume_mm3 == pytest.approx(1_000_000.0)


@pytest.mark.parametrize('data,name', [
    (b'', 'empty.stl'),
    (b'not a model at all', 'junk.stl'),
    (b'solid x\nfacet normal 0 0 0\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n', 'trunc.stl'),
    (b'v 0 0 0\nv 1 0 0\nf 1 2 9\n', 'bad.obj'),
    (b'v 0 0 zero\n', 'bad.obj'),
    (b'# nothing\n', 'empty.obj'),
    (b'PK not really a zip', 'bad.3mf'),
    (cube_binary_stl(), 'model.step'),
])
def test_invalid_files_raise_mesh_error(data, name):
    with pytest.raises(MeshError):
        analyse_mesh(data, name)


def test_binary_stl_claiming_too_many_triangles_is_rejected():
    data = bytearray(cube_binary_stl())
    struct.pack_into('<I', data, 80, 10_000_000)
    with pytest.raises(MeshError):
        analyse_mesh(bytes(data), 'huge.stl')


# --- Pricing -------------------------------------------------------------------------

def test_price_range_maths():
    assert price_range(100) == (75, 125)
    assert price_range(10.1) == (7, 13)   # floor(7.575), ceil(12.625)
    assert price_range(0.5) == (0, 1)
    assert price_range(0) == (0, 0)


def test_density_lookup():
    assert density_for('PLA') == 1.24
    assert density_for('PLA+') == 1.24
    assert density_for('petg') == 1.27
    assert density_for('ABS') == 1.04
    assert density_for('TPU 95A') == 1.21
    assert density_for('Nylon') == 1.14
    assert density_for('PA12-CF') == 1.14
    assert density_for('Mystery') == 1.24
    assert density_for(None) == 1.24


def test_estimate_uses_quote_formula():
    business = BusinessSettings(name='T', hourly_rate=6.0, estimate_fill_factor=0.5,
                                estimate_grams_per_hour=10.0, estimate_max_mm=256.0)
    pla = Filament(id=1, name='PLA Black', material='PLA', price_per_kg=40.0)
    mesh = MeshInfo(volume_mm3=100_000.0, size_x_mm=50, size_y_mm=50, size_z_mm=40, triangle_count=12)
    result = estimate_for_mesh(mesh, [pla], business)
    # 100 cm³ × 1.24 × 0.5 = 62 g → $2.48 material + 6.2 h × $6 = $37.20 → $39.68
    est = result['estimates'][0]
    assert est['grams'] == pytest.approx(62.0)
    assert est['hours'] == pytest.approx(6.2)
    assert (est['low'], est['high']) == price_range(39.68) == (29, 50)
    assert result['warnings'] == []
    assert 'estimate' in result['disclaimer'].lower()


def test_oversize_warning():
    business = BusinessSettings(name='T', hourly_rate=0, estimate_max_mm=200.0)
    mesh = MeshInfo(volume_mm3=1000.0, size_x_mm=250, size_y_mm=10, size_z_mm=10, triangle_count=12)
    result = estimate_for_mesh(mesh, [], business)
    assert any('200 mm' in w for w in result['warnings'])


# --- Endpoint ------------------------------------------------------------------------

@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.close(db_fd)
    app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}'})
    with app.app_context():
        db.create_all()
    with app.test_client() as client:
        yield client
    with app.app_context():
        db.drop_all()
    os.remove(db_path)


def _setup(client, enabled):
    with client.application.app_context():
        db.session.add(BusinessSettings(name='Acme 3D', hourly_rate=5.0, instant_estimate_enabled=enabled))
        pla = Filament(name='PLA Black', material='PLA', price_per_kg=40.0)
        petg = Filament(name='PETG Clear', material='PETG', price_per_kg=50.0)
        empty = Filament(name='ABS Red', material='ABS', price_per_kg=30.0)
        db.session.add_all([pla, petg, empty])
        db.session.flush()
        db.session.add_all([FilamentSpool(filament_id=pla.id, weight_remaining_g=800),
                            FilamentSpool(filament_id=petg.id, weight_remaining_g=500),
                            FilamentSpool(filament_id=empty.id, weight_remaining_g=0)])
        db.session.commit()
        return pla.id


def _post(client, data, name='cube.stl', **form):
    form['file'] = (io.BytesIO(data), name)
    return client.post('/order/estimate', data=form, content_type='multipart/form-data')


def test_endpoint_404_when_disabled(client):
    _setup(client, enabled=False)
    assert _post(client, cube_binary_stl()).status_code == 404
    page = client.get('/order')
    assert page.status_code == 200
    assert b'id="instant-estimate"' not in page.data


def test_endpoint_returns_estimate_when_enabled(client):
    pla_id = _setup(client, enabled=True)
    page = client.get('/order')
    assert b'instant-estimate' in page.data and b'Rough guide' in page.data

    resp = _post(client, cube_binary_stl())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['dimensions_mm'] == {'x': 10.0, 'y': 10.0, 'z': 10.0}
    assert body['volume_cm3'] == pytest.approx(1.0)
    names = [e['name'] for e in body['estimates']]
    assert names == ['PETG Clear', 'PLA Black']  # out-of-stock ABS excluded
    for e in body['estimates']:
        assert e['high'] >= e['low'] >= 0

    one = _post(client, cube_ascii_stl(), filament_id=str(pla_id)).get_json()
    assert [e['filament_id'] for e in one['estimates']] == [pla_id]


def test_endpoint_rejects_bad_files(client):
    _setup(client, enabled=True)
    resp = _post(client, b'garbage', name='junk.obj')
    assert resp.status_code == 422
    assert resp.get_json()['ok'] is False
    assert _post(client, b'x', name='thing.step').status_code == 422
    resp = client.post('/order/estimate', data={}, content_type='multipart/form-data')
    assert resp.status_code == 400


def test_endpoint_oversize_model_warns(client):
    _setup(client, enabled=True)
    with client.application.app_context():
        BusinessSettings.query.first().estimate_max_mm = 8.0
        db.session.commit()
    body = _post(client, cube_3mf(), name='cube.3mf').get_json()
    assert body['ok'] is True
    assert any('8 mm' in w for w in body['warnings'])
