"""Branded error pages.

Public visitors get a page in the public site's style; anything under /dash gets the
dashboard style (which already shows the plain signed-out layout to visitors who
aren't logged in). JSON/background requests — the order form's live estimate calls,
the Stripe webhook, the note editor's autosave — get a small JSON body instead of
HTML so their scripts can show a sensible message.
"""
import logging

from flask import jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException, InternalServerError

log = logging.getLogger(__name__)

JSON_PATHS = ('/order/estimate', '/webhooks/')

MESSAGES = {
    400: ('Something in that request didn’t look right',
          'The page sent something we couldn’t understand. Go back, refresh the page and try again.'),
    401: ('Please sign in', 'You need to be signed in to see this page.'),
    403: ('You don’t have access to this', 'This page isn’t available to your account.'),
    404: ('We couldn’t find that page', 'The link may be old or mistyped, or the page may have been moved.'),
    405: ('That didn’t work', 'This page can’t be used that way. Go back and try again from the page itself.'),
    410: ('This link has expired', 'It’s no longer available.'),
    413: ('That upload is too large', None),  # filled in from MAX_CONTENT_LENGTH
    429: ('Slow down a little', 'You’ve made a lot of requests in a short time. Wait a minute, then try again.'),
    500: ('Something went wrong on our end', 'Sorry about that. It’s not you — please try again in a moment.'),
    503: ('We’ll be right back', 'The site is briefly unavailable. Please try again in a few minutes.'),
}

CSRF_MESSAGE = ('Your form needs a refresh',
                'For your security, forms expire after a while or if the page was open in another tab. '
                'Go back, refresh the page and submit it again — anything you typed may need re-entering.')

# Used only if even the error template can't render (e.g. the database is down).
FALLBACK_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{code} · {title}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,sans-serif;
background:#faf8f5;color:#1c1a17;padding:1.5rem}}@media(prefers-color-scheme:dark){{body{{background:#0e0f11;color:#ededef}}}}
main{{max-width:460px;text-align:center}}b{{display:block;font-size:4rem;color:#ea580c;letter-spacing:-.04em}}
a{{color:#ea580c}}</style></head><body><main><b>{code}</b><h1>{title}</h1><p>{message}</p>
<p><a href="/">Go to the home page</a></p></main></body></html>"""


def _wants_json():
    if request.is_json or request.path.startswith(JSON_PATHS):
        return True
    best = request.accept_mimetypes.best_match(['application/json', 'text/html'])
    return best == 'application/json' and request.accept_mimetypes[best] > request.accept_mimetypes['text/html']


def _texts(code, error):
    if isinstance(error, CSRFError):
        return CSRF_MESSAGE
    title, message = MESSAGES.get(code, MESSAGES[500] if code >= 500 else MESSAGES[400])
    if code == 413:
        from flask import current_app
        limit = current_app.config.get('MAX_CONTENT_LENGTH') or 0
        if not limit:
            mb = 'the limit'
        elif limit >= 1024 * 1024:
            mb = f'{limit / (1024 * 1024):g} MB'
        else:
            mb = f'{max(1, round(limit / 1024))} KB'
        message = (f'Everything sent at once has to be under {mb}. Remove some files or send '
                   f'large models as a link instead, then try again.')
    return title, message


def render_error(code, error=None):
    title, message = _texts(code, error)
    if _wants_json():
        return jsonify(ok=False, error=message, status=code), code
    try:
        is_dash = request.path.startswith('/dash')
        context = dict(code=code, title=title, message=message, error_page=True)
        if is_dash:
            return render_template('errors/dash.html', **context), code
        from app.helpers import get_business_settings
        return render_template('errors/public.html', business=get_business_settings(), **context), code
    except Exception:
        log.exception('Error page failed to render for %s', code)
        return FALLBACK_HTML.format(code=code, title=title, message=message), code


def register_error_handlers(app):
    @app.errorhandler(CSRFError)
    def _csrf(error):
        log.info('CSRF failure on %s: %s', request.path, error.description)
        return render_error(400, error)

    @app.errorhandler(HTTPException)
    def _http(error):
        if error.code is None or error.code < 400:
            return error
        return render_error(error.code, error)

    @app.errorhandler(Exception)
    def _unhandled(error):
        if isinstance(error, HTTPException):
            return _http(error)
        log.exception('Unhandled error on %s', request.path)
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass
        if app.testing:
            raise error  # let tests see the real exception
        return render_error(500, InternalServerError())
