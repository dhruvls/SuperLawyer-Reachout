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

    return app
