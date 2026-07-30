/*
 * Markdown editor: plain-text while editing, rendered preview once you leave
 * the field. No live/inline rendering while typing (that mode was unreliable
 * to edit in) — you get a normal textarea, and Markdown is rendered to HTML
 * on blur, matching the same {{ text|markdown }} output used elsewhere in
 * the app (reuses the .md-content styles).
 *
 * Also adds a full-screen toggle so the field can be expanded to edit/read
 * comfortably from anywhere in the app.
 *
 * Usage: <textarea class="md-editor" name="notes">...</textarea>
 * On page load this wraps the textarea in a toolbar + preview pane. The
 * textarea itself is left in the DOM with its normal name/value, so forms
 * submit exactly as before with zero server changes.
 */
(function () {
  'use strict';

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // Inline rules: code spans, links, bold, italic. Code spans are protected
  // first so their contents aren't re-parsed for emphasis markers.
  function renderInline(text) {
    var stash = [];
    function stow(html) {
      stash.push(html);
      return '\u0000' + (stash.length - 1) + '\u0000';
    }

    var html = escapeHtml(text);

    html = html.replace(/`([^`]+)`/g, function (m, inner) {
      return stow('<code>' + inner + '</code>');
    });
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (m, label, url) {
      return stow('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>');
    });
    html = html.replace(/(\*\*|__)(\S(?:.*?\S)?)\1/g, function (m, marker, inner) {
      return stow('<strong>' + inner + '</strong>');
    });
    html = html.replace(/(\*|_)(\S(?:.*?\S)?)\1/g, function (m, marker, inner) {
      return stow('<em>' + inner + '</em>');
    });

    html = html.replace(/\u0000(\d+)\u0000/g, function (m, i) { return stash[+i]; });
    return html;
  }

  // Block-level Markdown -> HTML. Deliberately simple (no tables, no nested
  // lists) — enough for notes/terms text, and close enough to the server's
  // `markdown` library output that switching between preview and the
  // saved/rendered version elsewhere on the page looks the same.
  function renderMarkdown(src) {
    var lines = (src || '').replace(/\r\n/g, '\n').split('\n');
    var out = [];
    var i = 0;
    var para = [];
    var list = null; // { type: 'ul'|'ol', items: [] }

    function flushPara() {
      if (para.length) {
        out.push('<p>' + para.map(renderInline).join('<br>') + '</p>');
        para = [];
      }
    }
    function flushList() {
      if (list) {
        out.push('<' + list.type + '>' + list.items.join('') + '</' + list.type + '>');
        list = null;
      }
    }

    while (i < lines.length) {
      var line = lines[i];

      var fence = line.match(/^```/);
      if (fence) {
        flushPara(); flushList();
        var code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++;
        out.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>');
        continue;
      }

      var heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushPara(); flushList();
        var level = heading[1].length;
        out.push('<h' + level + '>' + renderInline(heading[2]) + '</h' + level + '>');
        i++; continue;
      }

      var quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushPara(); flushList();
        var qLines = [quote[1]];
        i++;
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          qLines.push(lines[i].replace(/^>\s?/, ''));
          i++;
        }
        out.push('<blockquote>' + qLines.map(renderInline).join('<br>') + '</blockquote>');
        continue;
      }

      var ulItem = line.match(/^[-*]\s+(.*)$/);
      var olItem = !ulItem && line.match(/^\d+\.\s+(.*)$/);
      if (ulItem || olItem) {
        flushPara();
        var kind = ulItem ? 'ul' : 'ol';
        if (list && list.type !== kind) flushList();
        if (!list) list = { type: kind, items: [] };
        list.items.push('<li>' + renderInline((ulItem || olItem)[1]) + '</li>');
        i++; continue;
      }

      if (line.trim() === '') {
        flushPara(); flushList();
        i++; continue;
      }

      flushList();
      para.push(line);
      i++;
    }
    flushPara(); flushList();
    return out.join('');
  }

  function enhance(textarea) {
    if (textarea.dataset.mdEnhanced) return;
    textarea.dataset.mdEnhanced = '1';

    var wrap = document.createElement('div');
    wrap.className = 'md-editor-wrap';

    var toolbar = document.createElement('div');
    toolbar.className = 'md-editor-toolbar';

    var hint = document.createElement('span');
    hint.className = 'md-editor-hint';
    hint.textContent = 'Markdown — renders when you click away';

    var fsBtn = document.createElement('button');
    fsBtn.type = 'button';
    fsBtn.className = 'md-editor-fs-btn';
    fsBtn.setAttribute('aria-label', 'Expand full screen');
    fsBtn.title = 'Expand full screen';
    fsBtn.textContent = '⤢';
    // Prevent the button from stealing focus (and thus blurring the
    // textarea) on mousedown, so toggling full screen mid-edit doesn't
    // trigger a spurious blur -> preview -> re-edit flicker.
    fsBtn.addEventListener('mousedown', function (e) { e.preventDefault(); });

    toolbar.appendChild(hint);
    toolbar.appendChild(fsBtn);

    var preview = document.createElement('div');
    preview.className = 'md-content md-editor-preview';
    preview.tabIndex = 0;
    preview.setAttribute('role', 'button');
    preview.setAttribute('aria-label', 'Click to edit');

    var backdrop = document.createElement('div');
    backdrop.className = 'md-editor-backdrop';
    backdrop.addEventListener('click', function () {
      if (wrap.classList.contains('md-editor-fullscreen')) fsBtn.click();
    });

    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(toolbar);
    wrap.appendChild(textarea);
    wrap.appendChild(preview);
    wrap.appendChild(backdrop);
    textarea.classList.add('md-editor-source');

    function showEditMode(focus) {
      wrap.classList.add('md-editor-editing');
      textarea.style.display = '';
      preview.style.display = 'none';
      if (focus) textarea.focus();
    }

    function showPreviewMode() {
      var val = textarea.value || '';
      if (val.trim() === '') {
        preview.innerHTML = '<span class="md-editor-empty">' +
          escapeHtml(textarea.dataset.placeholder || 'Nothing written yet — click to add notes.') +
          '</span>';
      } else {
        preview.innerHTML = renderMarkdown(val);
      }
      wrap.classList.remove('md-editor-editing');
      textarea.style.display = 'none';
      preview.style.display = '';
    }

    preview.addEventListener('click', function () { showEditMode(true); });
    preview.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showEditMode(true); }
    });

    textarea.addEventListener('blur', function () {
      showPreviewMode();
    });

    fsBtn.addEventListener('click', function () {
      var goingFullscreen = !wrap.classList.contains('md-editor-fullscreen');
      wrap.classList.toggle('md-editor-fullscreen');
      document.body.classList.toggle('md-editor-fullscreen-lock', goingFullscreen);
      fsBtn.textContent = goingFullscreen ? '✕' : '⤢';
      fsBtn.title = goingFullscreen ? 'Exit full screen' : 'Expand full screen';
      fsBtn.setAttribute('aria-label', fsBtn.title);
      if (goingFullscreen) {
        showEditMode(true);
      } else {
        showPreviewMode();
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && wrap.classList.contains('md-editor-fullscreen')) {
        fsBtn.click();
      }
    });

    // Start ready to type — plain textarea, nothing rendered until you leave
    // the field.
    showEditMode(false);
  }

  function init() {
    document.querySelectorAll('textarea.md-editor').forEach(enhance);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
