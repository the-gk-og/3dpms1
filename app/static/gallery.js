/* Public gallery lightbox — vanilla JS, no dependencies.
   Opens from any .pub-gallery-open button; Esc closes, ←/→ step through,
   Tab stays inside the dialog, focus returns to the thumbnail on close. */
(function () {
  var box = document.getElementById('pub-lightbox');
  if (!box) return;
  var items = Array.prototype.slice.call(document.querySelectorAll('.pub-gallery-open'));
  var img = document.getElementById('pub-lightbox-img');
  var title = document.getElementById('pub-lightbox-title');
  var material = document.getElementById('pub-lightbox-material');
  var caption = document.getElementById('pub-lightbox-caption');
  var count = document.getElementById('pub-lightbox-count');
  var current = 0, opener = null;

  function show(i) {
    current = (i + items.length) % items.length;
    var d = items[current].dataset;
    img.src = d.src;
    img.alt = d.title || 'Printed part';
    title.textContent = d.title || '';
    material.textContent = d.material || '';
    material.hidden = !d.material;
    caption.textContent = d.caption || '';
    count.textContent = items.length > 1 ? (current + 1) + ' of ' + items.length : '';
    box.querySelector('.pub-lightbox-prev').hidden = items.length < 2;
    box.querySelector('.pub-lightbox-next').hidden = items.length < 2;
  }

  function open(i, from) {
    opener = from || null;
    show(i);
    box.hidden = false;
    document.body.classList.add('pub-lightbox-open');
    box.querySelector('.pub-lightbox-close').focus();
  }

  function close() {
    box.hidden = true;
    img.removeAttribute('src');
    document.body.classList.remove('pub-lightbox-open');
    if (opener) opener.focus();
  }

  items.forEach(function (btn, i) {
    btn.addEventListener('click', function () { open(i, btn); });
  });

  box.addEventListener('click', function (e) {
    var action = e.target.closest('[data-lightbox]');
    if (action) {
      var a = action.getAttribute('data-lightbox');
      if (a === 'close') close();
      else if (a === 'prev') show(current - 1);
      else if (a === 'next') show(current + 1);
    } else if (e.target === box || e.target.classList.contains('pub-lightbox-inner')) {
      close();
    }
  });

  document.addEventListener('keydown', function (e) {
    if (box.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); show(current - 1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); show(current + 1); }
    else if (e.key === 'Tab') {
      var f = Array.prototype.filter.call(box.querySelectorAll('button'), function (b) { return !b.hidden; });
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  // Deep link from the home page strip: /gallery#photo-12 opens that photo.
  var m = /^#photo-(\d+)$/.exec(window.location.hash);
  if (m) {
    var target = document.getElementById('photo-' + m[1]);
    var btn = target && target.querySelector('.pub-gallery-open');
    if (btn) open(items.indexOf(btn), btn);
  }
})();
