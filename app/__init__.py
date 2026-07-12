import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

basedir = os.path.abspath(os.path.dirname(__file__))
project_root = os.path.abspath(os.path.join(basedir, '..'))
instance_dir = os.path.join(project_root, 'instance')
instance_db = os.path.join(instance_dir, 'app.db')

os.makedirs(instance_dir, exist_ok=True)

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev-secret-key'),
        SQLALCHEMY_DATABASE_URI=os.environ.get('DATABASE_URL', f'sqlite:///{instance_db}'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    login_manager.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app.auth import auth_bp
    from app.main import main_bp
    from app.settings import settings_bp
    from app.filament import filament_bp
    from app.public import public_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(filament_bp)
    app.register_blueprint(public_bp)

    with app.app_context():
        db.create_all()
        from app.migrate import run_migrations
        run_migrations(db)

    _register_cli(app)

    return app


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
