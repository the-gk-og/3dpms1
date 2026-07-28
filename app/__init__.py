import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

basedir = os.path.abspath(os.path.dirname(__file__))
project_root = os.path.abspath(os.path.join(basedir, '..'))
instance_dir = os.path.join(project_root, 'instance')
instance_db = os.path.join(instance_dir, 'app.db')

os.makedirs(instance_dir, exist_ok=True)

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.session_protection = 'strong'
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
oauth = OAuth()

# Fallback key used only for tests. Never used in production — see the RuntimeError
# below, which refuses to start without a real SECRET_KEY from the environment.
_DEV_SECRET_KEY = 'dev-secret-key-do-not-use-in-production'


def _enable_sqlite_wal_mode(app):
    """WAL mode lets readers and writers work concurrently instead of blocking on a
    single file lock — needed because the Docker image runs 3 gunicorn worker
    processes against one SQLite file. Without this, concurrent requests can raise
    'database is locked' errors. No-op for Postgres.
    """
    if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
        return
    from sqlalchemy import event

    with app.app_context():
        @event.listens_for(db.engine, 'connect')
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA busy_timeout=5000')
            cursor.close()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)

    secret_key = os.environ.get('SECRET_KEY')
    testing = bool(test_config and test_config.get('TESTING'))
    if not secret_key:
        if testing:
            secret_key = _DEV_SECRET_KEY
        else:
            # Refuse to start with a guessable secret key — a weak SECRET_KEY lets an
            # attacker forge session cookies and CSRF tokens.
            raise RuntimeError(
                'SECRET_KEY environment variable is not set. Generate one with '
                '`python -c "import secrets; print(secrets.token_hex(32))"` and set it '
                'in your .env file before starting the app.'
            )

    behind_proxy = os.environ.get('BEHIND_PROXY', '1') != '0'

    app.config.from_mapping(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', f'sqlite:///{instance_db}'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # Session / auth cookie hardening. SECURE requires HTTPS — the app is expected
        # to sit behind a TLS-terminating reverse proxy (Caddy, Cloudflare Tunnel, etc.)
        # in production; set COOKIE_INSECURE=1 only for plain-HTTP local dev.
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_INSECURE') != '1',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        REMEMBER_COOKIE_SECURE=os.environ.get('COOKIE_INSECURE') != '1',
        REMEMBER_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,  # 12 hours
        # Hard cap on total request size (covers multi-file uploads) — mitigates
        # unauthenticated public-form upload DoS. Individual file-type/size limits
        # are enforced separately in app/helpers.py.
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,  # 32 MB
        WTF_CSRF_ENABLED=not testing,
        RATELIMIT_ENABLED=not testing,
    )

    if test_config:
        app.config.update(test_config)

    if behind_proxy:
        # Trust one hop of X-Forwarded-* headers from the reverse proxy (Caddy /
        # Cloudflare Tunnel) so request.remote_addr, request.is_secure, and
        # SESSION_COOKIE_SECURE behave correctly instead of seeing the proxy's own
        # address / scheme. Set BEHIND_PROXY=0 if the app is ever exposed directly.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    _enable_sqlite_wal_mode(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    oauth.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app.auth import auth_bp
    from app.main import main_bp
    from app.settings import settings_bp
    from app.filament import filament_bp
    from app.public import public_bp
    from app.export import export_bp
    from app.feedback import feedback_bp
    from app.imports import import_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(filament_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(feedback_bp)
    app.register_blueprint(import_bp)

    with app.app_context():
        db.create_all()
        from app.migrate import run_migrations
        run_migrations(db)

    _register_cli(app)
    _register_security_headers(app)

    from app.helpers import is_mobile_request
    app.jinja_env.globals['is_mobile_request'] = is_mobile_request

    return app


def _register_security_headers(app):
    @app.after_request
    def set_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = (
            'camera=(), microphone=(), geolocation=(), payment=()'
        )
        # Cloudflare Turnstile (bot-check widget) needs its script/frame origin
        # allow-listed; everything else is restricted to same-origin. Inline
        # <script>/<style> attributes are used throughout the dashboard templates,
        # so 'unsafe-inline' is kept for now rather than silently breaking the UI —
        # tightening this further would mean moving those into external files.
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "frame-src https://challenges.cloudflare.com; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        if app.config.get('SESSION_COOKIE_SECURE'):
            response.headers['Strict-Transport-Security'] = (
                'max-age=63072000; includeSubDomains'
            )
        return response


def _register_cli(app):
    import click

    @app.cli.command('create-admin')
    @click.option('--username', prompt=True)
    @click.option('--email', prompt=True)
    @click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(username, email, password):
        """Create a dashboard user. Use this to set up the first account — there is
        no public sign-up page, so this is the only way in until at least one user
        exists (after that, more can be added from the dashboard under Settings > Users).
        """
        from app.models import User

        if User.query.filter_by(username=username).first():
            click.echo(f'A user named "{username}" already exists.')
            return
        if User.query.filter_by(email=email).first():
            click.echo(f'A user with email "{email}" already exists.')
            return

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f'Created user "{username}". You can now log in at /dash/auth/login.')

    @app.cli.command('send-overdue-reminders')
    @click.option('--dry-run', is_flag=True, help='List what would be sent without actually emailing anyone.')
    def send_overdue_reminders(dry_run):
        """Email clients about overdue invoices and notify the business owner with a
        summary. Intended to run daily via an external cron job (e.g.
        `docker compose exec web flask send-overdue-reminders`) — this app has no
        built-in scheduler, so nothing runs this automatically on its own.

        Reminders are re-sent at most once every 7 days per invoice (tracked via
        Invoice.last_reminder_sent_at), so running this daily is safe and won't spam
        clients. Invoices with no client email on file are skipped and reported in
        the summary rather than erroring the whole run.
        """
        from datetime import datetime, timedelta
        from app.models import Invoice
        from app.helpers import get_business_settings, notify_admin_new_submission, EmailNotConfiguredError
        from app.main import send_invoice_reminder

        business = get_business_settings()
        cutoff = datetime.utcnow() - timedelta(days=7)

        candidates = Invoice.query.filter(
            Invoice.archived.is_(False),
            Invoice.status.notin_(['Paid', 'Cancelled']),
            Invoice.due_date.isnot(None),
        ).all()
        overdue = [inv for inv in candidates if inv.is_overdue]
        due_for_reminder = [
            inv for inv in overdue
            if not inv.last_reminder_sent_at or inv.last_reminder_sent_at < cutoff
        ]

        sent, skipped_no_email, failed = [], [], []

        for invoice in due_for_reminder:
            if not invoice.client or not invoice.client.email:
                skipped_no_email.append(invoice.display_number)
                continue

            if dry_run:
                sent.append(invoice.display_number)
                continue

            try:
                send_invoice_reminder(invoice, business)
                sent.append(invoice.display_number)
            except EmailNotConfiguredError:
                click.echo('SMTP is not configured — set it up in Settings before running this command.')
                return
            except Exception as e:
                failed.append(f'{invoice.display_number} ({e})')

        if dry_run:
            click.echo(f'Would send {len(sent)} reminder(s): {", ".join(sent) or "none"}')
            if skipped_no_email:
                click.echo(f'Skipped (no client email): {", ".join(skipped_no_email)}')
            return

        click.echo(f'Sent {len(sent)} reminder(s).')
        if skipped_no_email:
            click.echo(f'Skipped (no client email): {", ".join(skipped_no_email)}')
        if failed:
            click.echo(f'Failed: {", ".join(failed)}')

        if sent or skipped_no_email or failed:
            summary_lines = []
            if sent:
                summary_lines.append(f'Reminders sent for: {", ".join(sent)}')
            if skipped_no_email:
                summary_lines.append(f'Skipped, no client email on file: {", ".join(skipped_no_email)}')
            if failed:
                summary_lines.append(f'Failed to send: {", ".join(failed)}')
            try:
                notify_admin_new_submission(
                    business,
                    subject=f'Overdue invoice reminders — {len(sent)} sent',
                    body_text='\n'.join(summary_lines),
                )
            except Exception:
                pass  # best-effort — the reminders themselves already went out
