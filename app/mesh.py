"""Stdlib-only parsing of 3D model files (STL, OBJ, 3MF) for the instant estimate.

Everything here works on bytes already in memory — nothing is written to disk. The
goal is a rough volume / bounding box, not a faithful mesh loader: units are assumed
to be millimetres for STL and OBJ (3MF declares its unit and we honour it), and the
volume uses the signed-tetrahedron method, which is exact for a closed, consistently
wound mesh and degrades gracefully (but silently) for broken ones.
"""
import io
import math
import re
import struct
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

# Hard cap on how much geometry we're willing to chew through for one estimate.
# A 15 MB binary STL is ~300k triangles, so this is generous for real uploads while
# still bounding CPU/memory on a public endpoint.
MAX_TRIANGLES = 1_000_000
# A 3MF is a zip — cap the uncompressed size of any model part we read (zip-bomb guard).
MAX_3MF_PART_BYTES = 150 * 1024 * 1024
MAX_3MF_COMPONENT_DEPTH = 8

SUPPORTED_EXTENSIONS = ('stl', 'obj', '3mf')


class MeshError(ValueError):
    """The file couldn't be analysed. The message is safe to show a customer."""


@dataclass
class MeshInfo:
    volume_mm3: float
    size_x_mm: float
    size_y_mm: float
    size_z_mm: float
    triangle_count: int

    @property
    def volume_cm3(self):
        return self.volume_mm3 / 1000.0

    @property
    def dimensions(self):
        return (self.size_x_mm, self.size_y_mm, self.size_z_mm)


class _Accumulator:
    """Running signed volume + bounding box over triangles from any format."""

    def __init__(self):
        self.triangles = 0
        self.volume = 0.0  # absolute volumes of finished bodies
        self._body = 0.0   # signed volume of the body currently being added
        self.min = [math.inf] * 3
        self.max = [-math.inf] * 3

    def add_point(self, p):
        for i in range(3):
            if p[i] < self.min[i]:
                self.min[i] = p[i]
            if p[i] > self.max[i]:
                self.max[i] = p[i]

    def add_triangle(self, a, b, c):
        self.triangles += 1
        if self.triangles > MAX_TRIANGLES:
            raise MeshError(f'This model is too detailed to estimate automatically '
                            f'(more than {MAX_TRIANGLES:,} triangles).')
        # a · (b × c) / 6 — signed volume of the tetrahedron (origin, a, b, c)
        self._body += (a[0] * (b[1] * c[2] - b[2] * c[1])
                       - a[1] * (b[0] * c[2] - b[2] * c[0])
                       + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0

    def end_body(self):
        # Taken per body so a mirrored (inside-out) instance in a 3MF doesn't
        # cancel out a normal one.
        self.volume += abs(self._body)
        self._body = 0.0

    def result(self):
        self.end_body()
        if self.triangles == 0 or not all(math.isfinite(v) for v in self.min + self.max):
            raise MeshError('No printable geometry was found in this file.')
        if not math.isfinite(self.volume):
            raise MeshError('This file contains invalid coordinates.')
        return MeshInfo(
            volume_mm3=self.volume,
            size_x_mm=self.max[0] - self.min[0],
            size_y_mm=self.max[1] - self.min[1],
            size_z_mm=self.max[2] - self.min[2],
            triangle_count=self.triangles,
        )


# --- STL ---------------------------------------------------------------------------

_STL_VERTEX = re.compile(rb'vertex\s+(\S+)\s+(\S+)\s+(\S+)')


def _parse_stl(data):
    if len(data) >= 84:
        count = struct.unpack_from('<I', data, 80)[0]
        if len(data) == 84 + 50 * count and count > 0:
            return _parse_binary_stl(data, count)
    if data.lstrip()[:5].lower() == b'solid':
        return _parse_ascii_stl(data)
    if len(data) >= 84:
        count = struct.unpack_from('<I', data, 80)[0]
        # Some exporters pad the end of binary STLs; accept trailing bytes, never short files.
        if count > 0 and len(data) >= 84 + 50 * count:
            return _parse_binary_stl(data, count)
    raise MeshError('This doesn’t look like a valid STL file.')


def _parse_binary_stl(data, count):
    if count > MAX_TRIANGLES:
        raise MeshError(f'This model is too detailed to estimate automatically '
                        f'(more than {MAX_TRIANGLES:,} triangles).')
    acc = _Accumulator()
    for rec in struct.iter_unpack('<12fH', data[84:84 + 50 * count]):
        a, b, c = rec[3:6], rec[6:9], rec[9:12]
        acc.add_point(a)
        acc.add_point(b)
        acc.add_point(c)
        acc.add_triangle(a, b, c)
    return acc.result()


def _parse_ascii_stl(data):
    acc = _Accumulator()
    tri = []
    try:
        for m in _STL_VERTEX.finditer(data):
            p = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
            acc.add_point(p)
            tri.append(p)
            if len(tri) == 3:
                acc.add_triangle(*tri)
                tri = []
    except ValueError:
        raise MeshError('This STL file contains a malformed vertex.') from None
    if tri:
        raise MeshError('This STL file appears to be truncated.')
    return acc.result()


# --- OBJ ---------------------------------------------------------------------------

def _parse_obj(data):
    text = data.decode('utf-8', errors='replace')
    acc = _Accumulator()
    verts = []
    try:
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == 'v':
                verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == 'f':
                idx = []
                for token in parts[1:]:
                    i = int(token.split('/', 1)[0])
                    # OBJ indices are 1-based; negatives count back from the latest vertex.
                    i = i - 1 if i > 0 else len(verts) + i
                    if i < 0 or i >= len(verts):
                        raise MeshError('This OBJ file references a vertex that doesn’t exist.')
                    idx.append(i)
                if len(idx) < 3:
                    continue
                a = verts[idx[0]]
                for j in range(1, len(idx) - 1):  # fan-triangulate polygons
                    b, c = verts[idx[j]], verts[idx[j + 1]]
                    acc.add_point(a)
                    acc.add_point(b)
                    acc.add_point(c)
                    acc.add_triangle(a, b, c)
    except (ValueError, IndexError) as exc:
        if isinstance(exc, MeshError):
            raise
        raise MeshError('This OBJ file contains a malformed line.') from None
    return acc.result()


# --- 3MF ---------------------------------------------------------------------------

_UNIT_SCALE = {
    'micron': 0.001, 'millimeter': 1.0, 'centimeter': 10.0,
    'inch': 25.4, 'foot': 304.8, 'meter': 1000.0,
}
_IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _parse_transform(value):
    """3MF transforms are 12 numbers: a 3x3 matrix (row-major, row vectors) then
    the translation, i.e. p' = (x y z 1) · M."""
    if not value:
        return _IDENTITY
    try:
        nums = tuple(float(v) for v in value.split())
    except ValueError:
        raise MeshError('This 3MF file contains an invalid transform.') from None
    if len(nums) != 12:
        raise MeshError('This 3MF file contains an invalid transform.')
    return nums


def _compose(inner, outer):
    """Transform equivalent to applying `inner` then `outer`."""
    a = inner
    b = outer
    out = []
    for r in range(4):
        row = a[r * 3:r * 3 + 3]
        for col in range(3):
            v = row[0] * b[col] + row[1] * b[3 + col] + row[2] * b[6 + col]
            if r == 3:
                v += b[9 + col]
            out.append(v)
    return tuple(out)


def _apply(m, p):
    x, y, z = p
    return (x * m[0] + y * m[3] + z * m[6] + m[9],
            x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11])


class _ThreeMF:
    def __init__(self, zf):
        self.zf = zf
        self.models = {}  # part path -> (unit scale, objects, build items)

    def _read_model(self, path):
        path = path.lstrip('/')
        if path in self.models:
            return self.models[path]
        try:
            info = self.zf.getinfo(path)
        except KeyError:
            raise MeshError('This 3MF file is missing part of its model data.') from None
        if info.file_size > MAX_3MF_PART_BYTES:
            raise MeshError('This 3MF file is too large to estimate automatically.')

        objects = {}
        build = []
        scale = 1.0
        current = None  # (object id, kind, payload)
        vertices = None
        triangles = None
        try:
            with self.zf.open(info) as fh:
                head = fh.read(4096)
                if b'<!DOCTYPE' in head or b'<!ENTITY' in head:
                    raise MeshError('This 3MF file isn’t supported.')
                stream = io.BufferedReader(_Prefixed(head, fh))
                for event, el in ET.iterparse(stream, events=('start', 'end')):
                    tag = _local(el.tag)
                    if event == 'start':
                        if tag == 'model':
                            scale = _UNIT_SCALE.get(el.get('unit', 'millimeter'), 1.0)
                        elif tag == 'object':
                            current = el.get('id')
                        elif tag == 'mesh':
                            vertices, triangles = [], []
                        continue
                    # end events
                    if tag == 'vertex' and vertices is not None:
                        vertices.append((float(el.get('x')), float(el.get('y')), float(el.get('z'))))
                    elif tag == 'triangle' and triangles is not None:
                        triangles.append((int(el.get('v1')), int(el.get('v2')), int(el.get('v3'))))
                        if len(triangles) > MAX_TRIANGLES:
                            raise MeshError(f'This model is too detailed to estimate automatically '
                                            f'(more than {MAX_TRIANGLES:,} triangles).')
                    elif tag == 'mesh' and current is not None:
                        objects[current] = ('mesh', (vertices, triangles))
                        vertices = triangles = None
                    elif tag == 'component' and current is not None:
                        kind, comps = objects.get(current, ('components', []))
                        if kind == 'components':
                            comps.append((
                                _component_path(el) or path,
                                el.get('objectid'),
                                _parse_transform(el.get('transform')),
                            ))
                            objects[current] = ('components', comps)
                    elif tag == 'object':
                        current = None
                    elif tag == 'item':
                        build.append((
                            _component_path(el) or path,
                            el.get('objectid'),
                            _parse_transform(el.get('transform')),
                        ))
                    if tag in ('vertex', 'triangle', 'component', 'item'):
                        el.clear()
        except MeshError:
            raise
        except (ET.ParseError, TypeError, ValueError, zipfile.BadZipFile, OSError):
            raise MeshError('This 3MF file is damaged or uses an unsupported format.') from None
        self.models[path] = (scale, objects, build)
        return self.models[path]

    def _emit(self, acc, path, object_id, transform, depth):
        if depth > MAX_3MF_COMPONENT_DEPTH:
            raise MeshError('This 3MF file nests its parts too deeply.')
        scale, objects, _ = self._read_model(path)
        obj = objects.get(object_id)
        if obj is None:
            raise MeshError('This 3MF file references a part that doesn’t exist.')
        kind, payload = obj
        if kind == 'components':
            for comp_path, comp_id, comp_tf in payload:
                self._emit(acc, comp_path, comp_id, _compose(comp_tf, transform), depth + 1)
            return
        vertices, triangles = payload
        # Unit scaling happens first (in the object's own model file), then transforms.
        unit_tf = (scale, 0, 0, 0, scale, 0, 0, 0, scale, 0, 0, 0)
        tf = _compose(unit_tf, transform)
        pts = [_apply(tf, v) for v in vertices]
        n = len(pts)
        for p in pts:
            acc.add_point(p)
        for v1, v2, v3 in triangles:
            if not (0 <= v1 < n and 0 <= v2 < n and 0 <= v3 < n):
                raise MeshError('This 3MF file references a vertex that doesn’t exist.')
            acc.add_triangle(pts[v1], pts[v2], pts[v3])
        acc.end_body()

    def parse(self):
        root = _root_model_path(self.zf)
        _, objects, build = self._read_model(root)
        acc = _Accumulator()
        if build:
            for path, object_id, tf in build:
                self._emit(acc, path, object_id, tf, 0)
        else:
            for object_id, (kind, _) in objects.items():
                if kind == 'mesh':
                    self._emit(acc, root, object_id, _IDENTITY, 0)
        return acc.result()


def _component_path(el):
    for key, value in el.attrib.items():
        if _local(key) == 'path':
            return value.lstrip('/')
    return None


def _root_model_path(zf):
    try:
        rels = zf.read('_rels/.rels') if zf.getinfo('_rels/.rels').file_size < 1_000_000 else b''
        for el in ET.fromstring(rels).iter():
            if _local(el.tag) == 'Relationship' and el.get('Type', '').endswith('/3dmodel'):
                return el.get('Target', '').lstrip('/')
    except (KeyError, ET.ParseError):
        pass
    return '3D/3dmodel.model'


class _Prefixed(io.RawIOBase):
    """Re-attach bytes we already peeked at to the front of a stream."""

    def __init__(self, head, rest):
        self._head = head
        self._rest = rest

    def readable(self):
        return True

    def readinto(self, buf):
        if self._head:
            n = min(len(buf), len(self._head))
            buf[:n] = self._head[:n]
            self._head = self._head[n:]
            return n
        chunk = self._rest.read(len(buf))
        buf[:len(chunk)] = chunk
        return len(chunk)


def _parse_3mf(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MeshError('This doesn’t look like a valid 3MF file.') from None
    with zf:
        return _ThreeMF(zf).parse()


# --- Entry point -------------------------------------------------------------------

def analyse_mesh(data, filename):
    """Parse model bytes and return a MeshInfo, or raise MeshError."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
    if ext not in SUPPORTED_EXTENSIONS:
        raise MeshError('Instant estimates are available for STL, OBJ and 3MF files only.')
    if not data:
        raise MeshError('This file is empty.')
    if ext == 'stl':
        return _parse_stl(data)
    if ext == 'obj':
        return _parse_obj(data)
    return _parse_3mf(data)
