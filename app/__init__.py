import click
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from app.config import Config

db = SQLAlchemy()
login_manager = LoginManager()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Render gives postgres:// but SQLAlchemy needs postgresql://
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if uri and uri.startswith('postgres://'):
        app.config['SQLALCHEMY_DATABASE_URI'] = uri.replace('postgres://', 'postgresql://', 1)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message_category = 'info'

    from app.auth.routes import auth_bp
    from app.cases.routes import cases_bp
    from app.outreach.routes import outreach_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(cases_bp)
    app.register_blueprint(outreach_bp)

    with app.app_context():
        from app import models  # noqa: F401
        db.create_all()
        _seed_admin(app)

    @app.cli.command('create-user')
    @click.argument('email')
    @click.argument('name')
    @click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
    def create_user(email, name, password):
        """Create a new user. Usage: flask create-user EMAIL NAME"""
        from app.models import User
        if User.query.filter_by(email=email.lower()).first():
            click.echo(f'Error: {email} already exists.')
            return
        user = User(name=name, email=email.lower())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f'User {email} created successfully.')

    return app


def _seed_admin(app):
    """Create default admin user on first run."""
    import os
    from app.models import User

    email = os.getenv('ADMIN_EMAIL')
    password = os.getenv('ADMIN_PASSWORD')
    name = os.getenv('ADMIN_NAME', 'Admin')

    if not email or not password:
        return

    if User.query.filter_by(email=email.lower()).first():
        return

    user = User(name=name, email=email.lower())
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    app.logger.info(f'Admin user {email} created.')
