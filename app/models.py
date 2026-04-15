from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app import db, login_manager


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


case_bookmarks = db.Table('case_bookmarks',
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('case_id', db.Integer, db.ForeignKey('legal_case.id'), primary_key=True),
    db.Column('created_at', db.DateTime, default=lambda: datetime.now(timezone.utc))
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    emails_sent = db.relationship('OutreachEmail', backref='sender', lazy=True)
    bookmarked_cases = db.relationship('LegalCase', secondary=case_bookmarks, backref='bookmarked_by', lazy='dynamic')
    notes = db.relationship('CaseNote', backref='author', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class LegalCase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    summary = db.Column(db.Text)
    source_url = db.Column(db.String(1000))
    source_name = db.Column(db.String(200))
    published_date = db.Column(db.DateTime)
    trending_score = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default='active')
    ai_analysis = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    lawyers = db.relationship('Lawyer', backref='case', lazy=True, cascade='all, delete-orphan')
    outreach_emails = db.relationship('OutreachEmail', backref='case', lazy=True)
    case_notes = db.relationship('CaseNote', backref='case', lazy=True, cascade='all, delete-orphan',
                                 order_by='CaseNote.created_at.desc()')

    @property
    def practice_area(self):
        if self.ai_analysis:
            try:
                import json
                data = json.loads(self.ai_analysis)
                return data.get('practice_area', '')
            except Exception:
                pass
        return ''


class Lawyer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200))
    firm = db.Column(db.String(300))
    role = db.Column(db.String(100))
    linkedin_url = db.Column(db.String(500))
    email_source = db.Column(db.String(500))
    verified = db.Column(db.Boolean, default=False)
    confidence_score = db.Column(db.Float, default=0.0)
    verification_sources = db.Column(db.Text)  # JSON array of source objects
    case_id = db.Column(db.Integer, db.ForeignKey('legal_case.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    outreach_emails = db.relationship('OutreachEmail', backref='lawyer', lazy=True)


class OutreachEmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lawyer_id = db.Column(db.Integer, db.ForeignKey('lawyer.id'), nullable=True)
    case_id = db.Column(db.Integer, db.ForeignKey('legal_case.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject = db.Column(db.String(500), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='draft')
    email_type = db.Column(db.String(20), default='primary')
    sent_at = db.Column(db.DateTime)
    followup_date = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class CaseNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey('legal_case.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
