/*
 * Lightweight "live preview" Markdown editor, in the spirit of Obsidian's live
 * render mode: as you type, **bold**, *italic*, `code`, # headings, - lists and
 * > quotes render inline immediately instead of showing raw HTML in a separate
 * preview pane. The Markdown syntax characters stay visible but dimmed, so you
 * can still see and edit them.
 *
 * Usage: <textarea class="md-editor" name="notes">...</textarea>
 * On page load this hides the textarea, replaces it with a contenteditable
 * surface that mirrors it, and keeps the original textarea's value in sync on
 * every keystroke — so forms submit exactly as before with zero server changes.
 */
(function () {
  'use strict';

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // Renders one line of Markdown-ish text to HTML, wrapping syntax markers in
  // <span class="md-syntax"> so they can be dimmed via CSS rather than hidden —
  // hiding them outright would make the raw text impossible to edit reliably.
  function renderLine(line) {
    var html = escapeHtml(line);
    var prefix = '';
    var lineClass = '';

    var headingMatch = html.match(/^(#{1,4})(\s+)(.*)$/);
    var quoteMatch = !headingMatch && html.match(/^(&gt;)(\s+)(.*)$/);
    var listMatch = !headingMatch && !quoteMatch && html.match(/^([-*])(\s+)(.*)$/);

    if (headingMatch) {
      var level = headingMatch[1].length;
      prefix = '<span class="md-syntax">' + headingMatch[1] + headingMatch[2] + '</span>';
      html = headingMatch[3];
      lineClass = ' md-h md-h' + level;
    } else if (quoteMatch) {
      prefix = '<span class="md-syntax">' + quoteMatch[1] + quoteMatch[2] + '</span>';
      html = quoteMatch[3];
      lineClass = ' md-quote';
    } else if (listMatch) {
      prefix = '<span class="md-syntax">' + listMatch[1] + listMatch[2] + '</span>';
      html = listMatch[3];
      lineClass = ' md-list-item';
    }

    // Inline rules, applied after the line-level prefix has been split off.
    // Code spans are protected first (their contents shouldn't be re-parsed for
    // bold/italic), then bold and italic are matched in a single combined pass —
    // running them as two separate global replaces lets the italic pattern match
    // across an already-substituted bold span's HTML and corrupt it.
    var codeStash = [];
    html = html.replace(/(`)([^`]+)\1/g, function (m, marker, inner) {
      codeStash.push('<span class="md-syntax">' + marker + '</span><code>' + inner + '</code><span class="md-syntax">' + marker + '</span>');
      return '\u0000' + (codeStash.length - 1) + '\u0000';
    });
    html = html.replace(/(\*\*|__)(\S(?:.*?\S)?)\1|(\*|_)(\S(?:.*?\S)?)\3/g, function (m, dMarker, dInner, sMarker, sInner) {
      if (dMarker !== undefined) {
        return '<span class="md-syntax">' + dMarker + '</span><strong>' + dInner + '</strong><span class="md-syntax">' + dMarker + '</span>';
      }
      return '<span class="md-syntax">' + sMarker + '</span><em>' + sInner + '</em><span class="md-syntax">' + sMarker + '</span>';
    });
    html = html.replace(/\u0000(\d+)\u0000/g, function (m, i) { return codeStash[+i]; });

    if (line === '') return '<span class="md-line' + lineClass + '"><br></span>';
    return '<span class="md-line' + lineClass + '">' + prefix + html + '</span>';
  }

  function render(text) {
    return text.split('\n').map(renderLine).join('');
  }

  // Plain-text extraction that treats each .md-line as ending in a newline —
  // matching how we rendered it — rather than relying on innerText, whose
  // whitespace handling varies across browsers.
  function getPlainText(root) {
    var lines = root.querySelectorAll('.md-line');
    if (!lines.length) return root.textContent || '';
    var out = [];
    lines.forEach(function (line) { out.push(line.textContent); });
    return out.join('\n');
  }

  // Caret position is tracked as a plain character offset into the plain-text
  // representation, then restored by walking text nodes after re-render —
  // simple, and robust to the DOM being fully rebuilt on every keystroke.
  function getCaretOffset(root) {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    var range = sel.getRangeAt(0);
    if (!root.contains(range.startContainer)) return null;
    var pre = range.cloneRange();
    pre.selectNodeContents(root);
    pre.setEnd(range.startContainer, range.startOffset);
    return pre.toString().length;
  }

  function setCaretOffset(root, offset) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node, count = 0;
    while ((node = walker.nextNode())) {
      var next = count + node.textContent.length;
      if (offset <= next) {
        var range = document.createRange();
        range.setStart(node, offset - count);
        range.collapse(true);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        return;
      }
      count = next;
    }
    // Offset ran past the end (e.g. trailing newline) — park at the very end.
    root.focus();
    var sel2 = window.getSelection();
    sel2.selectAllChildren(root);
    sel2.collapseToEnd();
  }

  function enhance(textarea) {
    if (textarea.dataset.mdEnhanced) return;
    textarea.dataset.mdEnhanced = '1';

    var wrap = document.createElement('div');
    wrap.className = 'md-editor-wrap';

    var surface = document.createElement('div');
    surface.className = 'md-editor-surface';
    surface.contentEditable = 'true';
    surface.spellcheck = true;
    if (textarea.dataset.placeholder) {
      surface.dataset.placeholder = textarea.dataset.placeholder;
    }

    var hint = document.createElement('div');
    hint.className = 'md-editor-hint';
    hint.textContent = 'Markdown supported — **bold**, *italic*, `code`, # heading, - list, > quote';

    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(surface);
    wrap.appendChild(hint);
    wrap.appendChild(textarea);
    textarea.classList.add('md-editor-source');

    surface.innerHTML = render(textarea.value || '');

    surface.addEventListener('input', function () {
      var offset = getCaretOffset(surface);
      var text = getPlainText(surface);
      textarea.value = text;
      surface.innerHTML = render(text);
      if (offset !== null) setCaretOffset(surface, offset);
    });

    // Plain-text paste only — pasted HTML would otherwise inject raw markup
    // that our escaper would then double-render as literal text.
    surface.addEventListener('paste', function (e) {
      e.preventDefault();
      var text = (e.clipboardData || window.clipboardData).getData('text/plain');
      document.execCommand('insertText', false, text);
    });
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
