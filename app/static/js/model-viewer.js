/* Tiny dependency-free 3D model viewer (WebGL 1) for STL, OBJ and 3MF files.
 *
 *   ModelViewer.parse(arrayBuffer, filename) -> Promise<[{name, positions: Float32Array}]>
 *   var v = ModelViewer.create(containerElement);
 *   v.setObjects(objects); v.setColor(index, '#ff7a1a'); v.reset(); v.destroy();
 *   ModelViewer.colorFor(filamentColourName, index) -> '#rrggbb'
 *
 * Objects follow the same order the server uses for its estimate (one per STL/OBJ,
 * one per 3MF build item) so per-object material colours line up. Z is up, units mm.
 * Everything runs in the browser — files are never uploaded just to preview them.
 */
(function () {
  'use strict';

  var MAX_PREVIEW_TRIANGLES = 600000;

  // ---------------------------------------------------------------- parsing ---
  function parse(buffer, filename) {
    var ext = (filename.split('.').pop() || '').toLowerCase();
    try {
      if (ext === 'stl') return Promise.resolve([{ name: baseName(filename), positions: parseSTL(buffer) }]);
      if (ext === 'obj') return Promise.resolve([{ name: baseName(filename), positions: parseOBJ(buffer) }]);
      if (ext === '3mf') return parse3MF(buffer);
    } catch (e) {
      return Promise.reject(e);
    }
    return Promise.reject(new Error('Preview is available for STL, OBJ and 3MF files.'));
  }

  function baseName(name) { return name.replace(/^.*[\\/]/, '').replace(/\.[^.]+$/, '') || 'Model'; }

  function parseSTL(buffer) {
    var dv = new DataView(buffer);
    if (buffer.byteLength >= 84) {
      var count = dv.getUint32(80, true);
      if (84 + count * 50 === buffer.byteLength) {
        var out = new Float32Array(count * 9);
        for (var i = 0, o = 84; i < count; i++, o += 50) {
          for (var k = 0; k < 9; k++) out[i * 9 + k] = dv.getFloat32(o + 12 + k * 4, true);
        }
        return out;
      }
    }
    var text = new TextDecoder().decode(buffer);
    var re = /vertex\s+(\S+)\s+(\S+)\s+(\S+)/g, m, pts = [];
    while ((m = re.exec(text))) pts.push(+m[1], +m[2], +m[3]);
    if (!pts.length) throw new Error('No geometry found in this STL file.');
    return new Float32Array(pts.slice(0, pts.length - (pts.length % 9)));
  }

  function parseOBJ(buffer) {
    var lines = new TextDecoder().decode(buffer).split(/\r?\n/), verts = [], out = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      if (line.charAt(0) === 'v' && line.charAt(1) === ' ') {
        var p = line.split(/\s+/);
        verts.push([+p[1], +p[2], +p[3]]);
      } else if (line.charAt(0) === 'f' && line.charAt(1) === ' ') {
        var idx = line.split(/\s+/).slice(1).map(function (t) {
          var n = parseInt(t.split('/')[0], 10);
          return n < 0 ? verts.length + n : n - 1;
        });
        for (var j = 1; j + 1 < idx.length; j++) {
          [idx[0], idx[j], idx[j + 1]].forEach(function (vi) {
            var v = verts[vi] || [0, 0, 0];
            out.push(v[0], v[1], v[2]);
          });
        }
      }
    }
    if (!out.length) throw new Error('No geometry found in this OBJ file.');
    return new Float32Array(out);
  }

  // --- 3MF: a zip of XML. Minimal zip reader + DecompressionStream for deflate.
  function readZip(buffer) {
    var dv = new DataView(buffer), bytes = new Uint8Array(buffer), eocd = -1;
    for (var i = buffer.byteLength - 22; i >= Math.max(0, buffer.byteLength - 65557); i--) {
      if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error('This 3MF file is damaged.');
    var entries = {}, n = dv.getUint16(eocd + 10, true), off = dv.getUint32(eocd + 16, true);
    for (var e = 0; e < n; e++) {
      if (dv.getUint32(off, true) !== 0x02014b50) break;
      var method = dv.getUint16(off + 10, true), csize = dv.getUint32(off + 20, true);
      var nameLen = dv.getUint16(off + 28, true), extraLen = dv.getUint16(off + 30, true);
      var commentLen = dv.getUint16(off + 32, true), local = dv.getUint32(off + 42, true);
      var name = new TextDecoder().decode(bytes.subarray(off + 46, off + 46 + nameLen));
      entries[name.replace(/^\//, '').toLowerCase()] = { method: method, csize: csize, local: local };
      off += 46 + nameLen + extraLen + commentLen;
    }
    return {
      read: function (path) {
        var ent = entries[path.replace(/^\//, '').toLowerCase()];
        if (!ent) return Promise.reject(new Error('This 3MF file is missing part of its model.'));
        var start = ent.local + 30 + dv.getUint16(ent.local + 26, true) + dv.getUint16(ent.local + 28, true);
        var data = bytes.subarray(start, start + ent.csize);
        if (ent.method === 0) return Promise.resolve(new TextDecoder().decode(data));
        if (ent.method !== 8 || typeof DecompressionStream === 'undefined') {
          return Promise.reject(new Error('This browser can’t preview 3MF files.'));
        }
        var stream = new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
        return new Response(stream).text();
      }
    };
  }

  var IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];
  var UNIT = { micron: 0.001, millimeter: 1, centimeter: 10, inch: 25.4, foot: 304.8, meter: 1000 };

  function parseTransform(s) {
    if (!s) return IDENTITY;
    var v = s.trim().split(/\s+/).map(Number);
    return v.length === 12 && v.every(isFinite) ? v : IDENTITY;
  }
  // 3MF matrices are row-vector 4x3: p' = p · M  (m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32)
  function compose(a, b) { // apply a, then b
    var r = [];
    for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++) {
      r[i * 3 + j] = a[i * 3] * b[j] + a[i * 3 + 1] * b[3 + j] + a[i * 3 + 2] * b[6 + j];
    }
    for (var j2 = 0; j2 < 3; j2++) r[9 + j2] = a[9] * b[j2] + a[10] * b[3 + j2] + a[11] * b[6 + j2] + b[9 + j2];
    return r;
  }

  function attr(el, local) {
    for (var i = 0; i < el.attributes.length; i++) {
      var a = el.attributes[i];
      if ((a.localName || a.name.split(':').pop()) === local) return a.value;
    }
    return null;
  }

  function parse3MF(buffer) {
    var zip = readZip(buffer), models = {};
    function model(path) {
      path = path.replace(/^\//, '');
      if (!models[path]) {
        models[path] = zip.read(path).then(function (xml) {
          var doc = new DOMParser().parseFromString(xml, 'application/xml');
          var root = doc.documentElement;
          var scale = UNIT[root.getAttribute('unit') || 'millimeter'] || 1;
          var objects = {}, build = [];
          Array.prototype.forEach.call(doc.getElementsByTagNameNS('*', 'object'), function (o) {
            var mesh = o.getElementsByTagNameNS('*', 'mesh')[0];
            var entry = { name: o.getAttribute('name') };
            if (mesh) {
              var vs = mesh.getElementsByTagNameNS('*', 'vertex'), ts = mesh.getElementsByTagNameNS('*', 'triangle');
              var v = new Float32Array(vs.length * 3);
              for (var i = 0; i < vs.length; i++) {
                v[i * 3] = +vs[i].getAttribute('x'); v[i * 3 + 1] = +vs[i].getAttribute('y'); v[i * 3 + 2] = +vs[i].getAttribute('z');
              }
              var t = new Uint32Array(ts.length * 3);
              for (var j = 0; j < ts.length; j++) {
                t[j * 3] = +ts[j].getAttribute('v1'); t[j * 3 + 1] = +ts[j].getAttribute('v2'); t[j * 3 + 2] = +ts[j].getAttribute('v3');
              }
              entry.mesh = { v: v, t: t };
            } else {
              entry.components = Array.prototype.map.call(o.getElementsByTagNameNS('*', 'component'), function (c) {
                return { path: attr(c, 'path') || path, id: c.getAttribute('objectid'), tf: parseTransform(c.getAttribute('transform')) };
              });
            }
            objects[o.getAttribute('id')] = entry;
          });
          Array.prototype.forEach.call(doc.getElementsByTagNameNS('*', 'item'), function (it) {
            build.push({ path: attr(it, 'path') || path, id: it.getAttribute('objectid'), tf: parseTransform(it.getAttribute('transform')) });
          });
          return { scale: scale, objects: objects, build: build };
        });
      }
      return models[path];
    }

    function emit(out, path, id, tf, depth) {
      if (depth > 8) return Promise.resolve();
      return model(path).then(function (m) {
        var obj = m.objects[id];
        if (!obj) return;
        if (obj.components) {
          return obj.components.reduce(function (p, c) {
            return p.then(function () { return emit(out, c.path, c.id, compose(c.tf, tf), depth + 1); });
          }, Promise.resolve());
        }
        var M = compose([m.scale, 0, 0, 0, m.scale, 0, 0, 0, m.scale, 0, 0, 0], tf), v = obj.mesh.v, t = obj.mesh.t;
        for (var i = 0; i < t.length; i++) {
          var k = t[i] * 3, x = v[k], y = v[k + 1], z = v[k + 2];
          out.push(x * M[0] + y * M[3] + z * M[6] + M[9], x * M[1] + y * M[4] + z * M[7] + M[10], x * M[2] + y * M[5] + z * M[8] + M[11]);
        }
      });
    }

    return zip.read('_rels/.rels').then(function (rels) {
      var m = /Target="([^"]+)"[^>]*Type="[^"]*3dmodel"|Type="[^"]*3dmodel"[^>]*Target="([^"]+)"/.exec(rels);
      return (m && (m[1] || m[2])) || '3D/3dmodel.model';
    }, function () { return '3D/3dmodel.model'; }).then(function (rootPath) {
      return model(rootPath).then(function (root) {
        var items = root.build.length ? root.build : Object.keys(root.objects).filter(function (id) {
          return root.objects[id].mesh;
        }).map(function (id) { return { path: rootPath, id: id, tf: IDENTITY }; });
        var result = [];
        return items.reduce(function (p, item, n) {
          return p.then(function () {
            var out = [];
            return emit(out, item.path, item.id, item.tf, 0).then(function () {
              return model(item.path).then(function (mm) {
                var o = mm.objects[item.id] || {};
                result.push({ name: o.name || ('Object ' + (n + 1)), positions: new Float32Array(out) });
              });
            });
          });
        }, Promise.resolve()).then(function () {
          if (!result.some(function (r) { return r.positions.length; })) throw new Error('No geometry found in this 3MF file.');
          return result;
        });
      });
    });
  }

  // ---------------------------------------------------------------- colours ---
  var NAMED = [
    [/black|onyx|charcoal/, '#2a2a2e'], [/white|ivory|bone/, '#ecebe7'], [/silver/, '#b8bcc2'],
    [/gr[ae]y|ash|slate/, '#8a8d93'], [/gold|brass/, '#c9a227'], [/orange/, '#f07c2a'],
    [/yellow|lemon/, '#f2c933'], [/red|crimson|scarlet/, '#d33b33'], [/pink|magenta|rose/, '#e5649b'],
    [/purple|violet|lilac/, '#8b5cc7'], [/navy/, '#27407a'], [/blue|cyan|teal|sky/, '#3a7bd5'],
    [/green|olive|lime|mint/, '#3fa35b'], [/brown|wood|walnut|bronze|copper/, '#8a5a3b'],
    [/beige|tan|sand|natural|cream/, '#d8c7a3'], [/clear|transparent|translucent/, '#cfe3ea']
  ];
  var PALETTE = ['#f07c2a', '#3a7bd5', '#3fa35b', '#d33b33', '#8b5cc7', '#f2c933', '#2a9d8f', '#e5649b'];
  function colorFor(name, index) {
    var s = (name || '').toLowerCase();
    for (var i = 0; i < NAMED.length; i++) if (NAMED[i][0].test(s)) return NAMED[i][1];
    return PALETTE[(index || 0) % PALETTE.length];
  }
  function hexToRgb(hex) {
    var n = parseInt(String(hex).replace('#', ''), 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }

  // ------------------------------------------------------------------ maths ---
  function perspective(fovy, aspect, near, far) {
    var f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0];
  }
  function lookAt(e, c, up) {
    var z = norm([e[0] - c[0], e[1] - c[1], e[2] - c[2]]), x = norm(cross(up, z)), y = cross(z, x);
    return [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
      -dot(x, e), -dot(y, e), -dot(z, e), 1];
  }
  function mul(a, b) {
    var r = new Array(16);
    for (var i = 0; i < 4; i++) for (var j = 0; j < 4; j++) {
      r[j * 4 + i] = a[i] * b[j * 4] + a[4 + i] * b[j * 4 + 1] + a[8 + i] * b[j * 4 + 2] + a[12 + i] * b[j * 4 + 3];
    }
    return r;
  }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
  function norm(a) { var l = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / l, a[1] / l, a[2] / l]; }

  // --------------------------------------------------------------- renderer ---
  var VS = 'attribute vec3 p;attribute vec3 n;uniform mat4 mvp;varying vec3 vn;' +
    'void main(){vn=n;gl_Position=mvp*vec4(p,1.0);}';
  var FS = 'precision mediump float;varying vec3 vn;uniform vec3 color;uniform vec3 light;uniform float flat_;' +
    'void main(){if(flat_>0.5){gl_FragColor=vec4(color,1.0);return;}vec3 N=normalize(vn);' +
    'float d=abs(dot(N,light));float fill=abs(dot(N,normalize(vec3(-0.4,0.3,0.85))));' +
    'vec3 c=max(color,vec3(0.2));gl_FragColor=vec4(c*(0.28+0.62*d+0.18*fill),1.0);}';

  function create(container) {
    var canvas = document.createElement('canvas');
    canvas.className = 'mv-canvas';
    canvas.setAttribute('role', 'img');
    canvas.setAttribute('aria-label', '3D preview — drag to rotate, scroll or pinch to zoom');
    container.appendChild(canvas);
    var gl = canvas.getContext('webgl', { antialias: true, alpha: true, premultipliedAlpha: false }) ||
      canvas.getContext('experimental-webgl');
    if (!gl) {
      canvas.remove();
      var msg = document.createElement('div');
      msg.className = 'mv-message';
      msg.textContent = '3D preview isn’t available in this browser.';
      container.appendChild(msg);
      return { setObjects: function () {}, setColor: function () {}, reset: function () {}, destroy: function () { msg.remove(); } };
    }

    function shader(type, src) { var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; }
    var prog = gl.createProgram();
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, VS));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    var loc = {
      p: gl.getAttribLocation(prog, 'p'), n: gl.getAttribLocation(prog, 'n'),
      mvp: gl.getUniformLocation(prog, 'mvp'), color: gl.getUniformLocation(prog, 'color'),
      light: gl.getUniformLocation(prog, 'light'), flat: gl.getUniformLocation(prog, 'flat_')
    };

    var objects = [], grid = null, bounds = null;
    var state = { yaw: -0.6, pitch: 0.55, dist: 1 }, frame = 0, destroyed = false;

    function setObjects(list) {
      objects.forEach(function (o) { gl.deleteBuffer(o.pb); gl.deleteBuffer(o.nb); });
      objects = [];
      var total = list.reduce(function (s, o) { return s + o.positions.length / 9; }, 0);
      var stride = total > MAX_PREVIEW_TRIANGLES ? Math.ceil(total / MAX_PREVIEW_TRIANGLES) : 1;
      var lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
      list.forEach(function (o, idx) {
        var src = o.positions, tris = Math.floor(src.length / 9), keep = Math.ceil(tris / stride);
        var pos = new Float32Array(keep * 9), nrm = new Float32Array(keep * 9), w = 0;
        for (var t = 0; t < tris; t += stride) {
          var b = t * 9;
          var ax = src[b], ay = src[b + 1], az = src[b + 2], bx = src[b + 3], by = src[b + 4], bz = src[b + 5],
            cx = src[b + 6], cy = src[b + 7], cz = src[b + 8];
          var nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay), ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az),
            nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax), l = Math.hypot(nx, ny, nz) || 1;
          for (var k = 0; k < 9; k++) {
            var val = src[b + k];
            pos[w + k] = val;
            var axis = k % 3;
            if (val < lo[axis]) lo[axis] = val;
            if (val > hi[axis]) hi[axis] = val;
          }
          for (var v = 0; v < 3; v++) { nrm[w + v * 3] = nx / l; nrm[w + v * 3 + 1] = ny / l; nrm[w + v * 3 + 2] = nz / l; }
          w += 9;
        }
        var pb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, pb); gl.bufferData(gl.ARRAY_BUFFER, pos.subarray(0, w), gl.STATIC_DRAW);
        var nb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, nb); gl.bufferData(gl.ARRAY_BUFFER, nrm.subarray(0, w), gl.STATIC_DRAW);
        objects.push({ pb: pb, nb: nb, count: w / 3, color: hexToRgb(o.color || colorFor('', idx)) });
      });
      if (!isFinite(lo[0])) { lo = [0, 0, 0]; hi = [1, 1, 1]; }
      bounds = { lo: lo, hi: hi, center: [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2],
        radius: Math.max(1, Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2) };
      buildGrid();
      container.classList.toggle('mv-simplified', stride > 1);
      reset();
    }

    function buildGrid() {
      if (grid) gl.deleteBuffer(grid.b);
      var size = Math.max(bounds.hi[0] - bounds.lo[0], bounds.hi[1] - bounds.lo[1]) * 1.6 + 10;
      var step = Math.pow(10, Math.floor(Math.log10(size / 4)));
      var half = Math.ceil(size / 2 / step) * step, z = bounds.lo[2], cx = bounds.center[0], cy = bounds.center[1], pts = [];
      for (var g = -half; g <= half + 1e-6; g += step) {
        pts.push(cx + g, cy - half, z, cx + g, cy + half, z, cx - half, cy + g, z, cx + half, cy + g, z);
      }
      var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pts), gl.STATIC_DRAW);
      grid = { b: b, count: pts.length / 3 };
    }

    function reset() { state.yaw = -0.6; state.pitch = 0.55; state.dist = bounds ? bounds.radius * 2.8 : 10; draw(); }

    function cssColor(name, fallback) {
      var v = getComputedStyle(container).getPropertyValue(name).trim();
      if (/^#[0-9a-f]{6}$/i.test(v)) return hexToRgb(v);
      return fallback;
    }

    function draw() {
      if (destroyed || frame) return;
      frame = requestAnimationFrame(function () {
        frame = 0;
        var dpr = Math.min(window.devicePixelRatio || 1, 2);
        var w = Math.max(1, Math.round(canvas.clientWidth * dpr)), h = Math.max(1, Math.round(canvas.clientHeight * dpr));
        if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
        gl.viewport(0, 0, w, h);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        if (!bounds) return;
        gl.enable(gl.DEPTH_TEST);
        gl.useProgram(prog);
        var c = bounds.center, cp = Math.cos(state.pitch);
        var eye = [c[0] + state.dist * cp * Math.cos(state.yaw), c[1] + state.dist * cp * Math.sin(state.yaw), c[2] + state.dist * Math.sin(state.pitch)];
        var mvp = mul(perspective(0.7, w / h, state.dist / 100, state.dist * 10), lookAt(eye, c, [0, 0, 1]));
        gl.uniformMatrix4fv(loc.mvp, false, new Float32Array(mvp));
        gl.uniform3fv(loc.light, new Float32Array(norm([eye[0] - c[0], eye[1] - c[1], eye[2] - c[2] + bounds.radius])));
        // build plate grid
        gl.uniform1f(loc.flat, 1);
        gl.uniform3fv(loc.color, new Float32Array(cssColor('--mv-grid', [0.6, 0.6, 0.6])));
        gl.disableVertexAttribArray(loc.n);
        gl.bindBuffer(gl.ARRAY_BUFFER, grid.b);
        gl.enableVertexAttribArray(loc.p);
        gl.vertexAttribPointer(loc.p, 3, gl.FLOAT, false, 0, 0);
        gl.drawArrays(gl.LINES, 0, grid.count);
        // models
        gl.uniform1f(loc.flat, 0);
        gl.enableVertexAttribArray(loc.n);
        objects.forEach(function (o) {
          gl.uniform3fv(loc.color, new Float32Array(o.color));
          gl.bindBuffer(gl.ARRAY_BUFFER, o.pb); gl.vertexAttribPointer(loc.p, 3, gl.FLOAT, false, 0, 0);
          gl.bindBuffer(gl.ARRAY_BUFFER, o.nb); gl.vertexAttribPointer(loc.n, 3, gl.FLOAT, false, 0, 0);
          gl.drawArrays(gl.TRIANGLES, 0, o.count);
        });
      });
    }

    // -- interaction: drag to orbit, wheel / pinch to zoom, double-click to reset
    var pointers = {}, lastPinch = 0;
    canvas.addEventListener('pointerdown', function (e) {
      pointers[e.pointerId] = [e.clientX, e.clientY];
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', function (e) {
      var prev = pointers[e.pointerId];
      if (!prev) return;
      var ids = Object.keys(pointers);
      if (ids.length === 1) {
        state.yaw -= (e.clientX - prev[0]) * 0.01;
        state.pitch = Math.max(-1.5, Math.min(1.5, state.pitch + (e.clientY - prev[1]) * 0.01));
      }
      pointers[e.pointerId] = [e.clientX, e.clientY];
      if (ids.length === 2) {
        var a = pointers[ids[0]], b = pointers[ids[1]], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (lastPinch) zoom(lastPinch / d);
        lastPinch = d;
      }
      draw();
    });
    function up(e) { delete pointers[e.pointerId]; lastPinch = 0; }
    canvas.addEventListener('pointerup', up);
    canvas.addEventListener('pointercancel', up);
    canvas.addEventListener('wheel', function (e) { e.preventDefault(); zoom(Math.exp(e.deltaY * 0.0015)); }, { passive: false });
    canvas.addEventListener('dblclick', reset);
    function zoom(f) {
      if (!bounds) return;
      state.dist = Math.max(bounds.radius * 0.6, Math.min(bounds.radius * 20, state.dist * f));
      draw();
    }
    var ro = window.ResizeObserver ? new ResizeObserver(draw) : null;
    if (ro) ro.observe(canvas); else window.addEventListener('resize', draw);
    var mq = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
    if (mq && mq.addEventListener) mq.addEventListener('change', draw);

    return {
      setObjects: setObjects,
      setColor: function (i, hex) { if (objects[i]) { objects[i].color = hexToRgb(hex); draw(); } },
      reset: reset,
      destroy: function () {
        destroyed = true;
        if (ro) ro.disconnect();
        objects.forEach(function (o) { gl.deleteBuffer(o.pb); gl.deleteBuffer(o.nb); });
        var ext = gl.getExtension('WEBGL_lose_context');
        if (ext) ext.loseContext();
        canvas.remove();
      }
    };
  }

  window.ModelViewer = { parse: parse, create: create, colorFor: colorFor };
})();
