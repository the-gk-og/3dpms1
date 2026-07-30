"""Registry of Markdown-backed model fields that get their own full-page,
live-preview editor (Toast UI Editor) at /dash/notes/<key>/<id>/edit,
instead of the inline blur-preview textarea from md-editor.js.

To move another field onto the standalone editor, add one line here —
no route or template changes needed. The key becomes part of the URL,
so keep it stable once it's linked from a template.

Tuple shape: (Model, field_name, label, client_visible, back_endpoint, id_kwarg)

- Model:          the SQLAlchemy model class
- field_name:     the Text column holding Markdown
- label:          shown as the page title in the editor tab
- client_visible: True if this text can end up on a client-facing
                   document/email (shown as a banner in the editor so
                   nobody free-writes staff shorthand into it by habit)
- back_endpoint:  url_for() endpoint for the "back to record" link
- id_kwarg:       the url_for() kwarg name that endpoint expects
"""
from app.models import Client, Quote, Invoice, Job, Request

NOTE_FIELDS = {
    'job-notes':               (Job, 'notes', 'Job Notes', True, 'main.job_detail', 'job_id'),
    'job-internal_notes':      (Job, 'internal_notes', 'Job Internal Notes', False, 'main.job_detail', 'job_id'),

    'quote-notes':             (Quote, 'notes', 'Quote Notes', True, 'main.quote_detail', 'quote_id'),
    'quote-internal_notes':    (Quote, 'internal_notes', 'Quote Internal Notes', False, 'main.quote_detail', 'quote_id'),

    'invoice-notes':           (Invoice, 'notes', 'Invoice Notes', True, 'main.invoice_detail', 'invoice_id'),
    'invoice-internal_notes':  (Invoice, 'internal_notes', 'Invoice Internal Notes', False, 'main.invoice_detail', 'invoice_id'),

    'request-internal_notes':  (Request, 'internal_notes', 'Request Internal Notes', False, 'main.request_detail', 'request_id'),

    'client-notes':            (Client, 'notes', 'Client Notes', True, 'main.clients', None),
}
