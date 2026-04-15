import json
import click
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from app.config import Config

db = SQLAlchemy()
login_manager = LoginManager()

PRACTICE_AREA_COLORS = {
    'constitutional': 'danger',
    'corporate': 'primary',
    'criminal': 'dark',
    'tax': 'warning',
    'ip': 'info',
    'environmental': 'success',
    'cyber': 'purple',
    'insolvency': 'secondary',
    'antitrust': 'purple',
    'securities': 'primary',
    'banking': 'warning',
    'labour': 'orange',
    'employment': 'orange',
    'family': 'pink',
    'real estate': 'success',
    'media': 'info',
}


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if uri and uri.startswith('postgres://'):
        app.config['SQLALCHEMY_DATABASE_URI'] = uri.replace('postgres://', 'postgresql://', 1)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message_category = 'info'

    # Register Jinja filters
    @app.template_filter('practice_area')
    def practice_area_filter(ai_analysis_json):
        if not ai_analysis_json:
            return ''
        try:
            data = json.loads(ai_analysis_json)
            return data.get('practice_area', '')
        except Exception:
            return ''

    @app.template_filter('pa_color')
    def practice_area_color_filter(practice_area):
        if not practice_area:
            return 'secondary'
        pa = practice_area.lower().strip()
        for key, color in PRACTICE_AREA_COLORS.items():
            if key in pa:
                return color
        return 'secondary'

    @app.template_filter('from_json')
    def from_json_filter(value):
        if not value:
            return []
        try:
            return json.loads(value)
        except Exception:
            return []

    from app.auth.routes import auth_bp
    from app.cases.routes import cases_bp
    from app.outreach.routes import outreach_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(cases_bp)
    app.register_blueprint(outreach_bp)

    with app.app_context():
        from app import models  # noqa: F401
        db.create_all()
        _migrate_columns(app)
        _seed_admin(app)

    # Auto-scan scheduler
    _setup_scheduler(app)

    @app.cli.command('create-user')
    @click.argument('email')
    @click.argument('name')
    @click.option('--password', prompt=True, hide_input=True, confirmation_prompt=True)
    def create_user(email, name, password):
        """Create a new user."""
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


def _setup_scheduler(app):
    """Set up daily auto-scan using APScheduler."""
    import os
    if os.getenv('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            hour = app.config.get('DAILY_SCAN_HOUR', 6)
            scheduler = BackgroundScheduler()
            scheduler.add_job(
                func=lambda: _run_daily_scan(app),
                trigger='cron',
                hour=hour,
                id='daily_scan',
                replace_existing=True,
            )
            scheduler.start()
            import atexit
            atexit.register(lambda: scheduler.shutdown(wait=False))
            app.logger.info(f'Daily scan scheduled at {hour}:00 UTC')
        except Exception as e:
            app.logger.error(f'Scheduler setup failed: {e}')


def _run_daily_scan(app):
    """Run scan in app context."""
    with app.app_context():
        from app.cases.tracker import scan_for_cases
        app.logger.info("Starting daily scheduled scan...")
        try:
            summary = scan_for_cases()
            app.logger.info(
                f"Daily scan complete: {summary.get('new_cases', 0)} new cases, "
                f"{summary.get('lawyers_found', 0)} lawyers"
            )
        except Exception as e:
            app.logger.error(f"Daily scan failed: {e}")


def _migrate_columns(app):
    """Add columns / alter constraints that db.create_all() can't handle on existing tables."""
    from sqlalchemy import text, inspect as sa_inspect
    try:
        with db.engine.connect() as conn:
            # ── Add new lawyer columns ──────────────────────────────────────
            columns = [c['name'] for c in sa_inspect(db.engine).get_columns('lawyer')]
            new_cols = {
                'email_source': 'VARCHAR(500)',
                'verified': 'BOOLEAN DEFAULT FALSE',
                'confidence_score': 'FLOAT DEFAULT 0.0',
                'verification_sources': 'TEXT',
            }
            for col_name, col_type in new_cols.items():
                if col_name not in columns:
                    conn.execute(text(
                        f"ALTER TABLE lawyer ADD COLUMN {col_name} {col_type}"
                    ))
                    conn.commit()
                    app.logger.info(f'Added {col_name} column.')

            # ── Make outreach_email FKs nullable ───────────────────────────
            # Required so scan can wipe lawyers/cases without FK violations.
            # outreach_email rows are preserved but lawyer_id/case_id become NULL.
            is_pg = str(db.engine.url).startswith('postgresql')
            if is_pg:
                for col in ('lawyer_id', 'case_id'):
                    try:
                        conn.execute(text(
                            f"ALTER TABLE outreach_email ALTER COLUMN {col} DROP NOT NULL"
                        ))
                        conn.commit()
                        app.logger.info(f'outreach_email.{col} made nullable.')
                    except Exception:
                        pass  # already nullable — safe to ignore
    except Exception:
        pass


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
