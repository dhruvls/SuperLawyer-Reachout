from datetime import datetime, timedelta, timezone
from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import LegalCase, Lawyer, OutreachEmail
from app.outreach.generator import generate_email
from app.outreach.email_sender import send_email

outreach_bp = Blueprint('outreach', __name__)


@outreach_bp.route('/outreach')
@login_required
def outreach_list():
    status_filter = request.args.get('status', 'all')
    q = OutreachEmail.query.filter_by(user_id=current_user.id)
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    emails = q.order_by(OutreachEmail.created_at.desc()).all()
    return render_template('outreach.html', emails=emails, status=status_filter)


@outreach_bp.route('/outreach/generate/<int:lawyer_id>', methods=['POST'])
@login_required
def generate(lawyer_id):
    lawyer = db.get_or_404(Lawyer, lawyer_id)
    case = lawyer.case

    # Auto-detect: only allow followup if primary was already sent
    existing_primary = OutreachEmail.query.filter_by(
        lawyer_id=lawyer.id, user_id=current_user.id, email_type='primary'
    ).first()

    requested_type = request.form.get('email_type', 'primary')

    if requested_type == 'followup' and (
        not existing_primary or existing_primary.status not in ('sent', 'pending_followup', 'followed_up')
    ):
        email_type = 'primary'
    else:
        email_type = requested_type

    result = generate_email(lawyer, case, email_type)

    outreach = OutreachEmail(
        lawyer_id=lawyer.id,
        case_id=case.id,
        user_id=current_user.id,
        subject=result['subject'],
        body=result['body'],
        status='draft',
        email_type=email_type,
    )

    if email_type == 'followup':
        outreach.followup_date = datetime.now(timezone.utc) + timedelta(days=3)

    db.session.add(outreach)
    db.session.commit()

    flash(f'{"Follow-up" if email_type == "followup" else "Outreach"} email drafted.', 'success')
    return redirect(url_for('outreach.edit_email', email_id=outreach.id))


@outreach_bp.route('/outreach/<int:email_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_email(email_id):
    email = db.get_or_404(OutreachEmail, email_id)
    if email.user_id != current_user.id:
        flash('Unauthorized.', 'error')
        return redirect(url_for('outreach.outreach_list'))

    if request.method == 'POST':
        email.subject = request.form.get('subject', email.subject)
        email.body = request.form.get('body', email.body)
        db.session.commit()
        flash('Email updated.', 'success')
        return redirect(url_for('outreach.edit_email', email_id=email.id))

    return render_template('edit_email.html', email=email)


@outreach_bp.route('/outreach/<int:email_id>/send', methods=['POST'])
@login_required
def send(email_id):
    outreach = db.get_or_404(OutreachEmail, email_id)
    if outreach.user_id != current_user.id:
        flash('Unauthorized.', 'error')
        return redirect(url_for('outreach.outreach_list'))

    lawyer = outreach.lawyer
    if not lawyer.email:
        flash('Lawyer email address is missing. Add it first.', 'error')
        return redirect(url_for('cases.case_detail', case_id=outreach.case_id))

    try:
        send_email(
            to_address=lawyer.email,
            subject=outreach.subject,
            body=outreach.body,
        )
        outreach.status = 'sent'
        outreach.sent_at = datetime.now(timezone.utc)

        if outreach.email_type == 'primary':
            outreach.followup_date = datetime.now(timezone.utc) + timedelta(days=3)
            outreach.status = 'pending_followup'

        db.session.commit()
        flash(f'Email sent to {lawyer.email}.', 'success')
    except Exception as e:
        outreach.status = 'failed'
        db.session.commit()
        flash(f'Send failed: {str(e)}', 'error')

    return redirect(url_for('outreach.outreach_list'))


@outreach_bp.route('/outreach/<int:email_id>/delete', methods=['POST'])
@login_required
def delete_email(email_id):
    outreach = db.get_or_404(OutreachEmail, email_id)
    if outreach.user_id != current_user.id:
        flash('Unauthorized.', 'error')
        return redirect(url_for('outreach.outreach_list'))

    db.session.delete(outreach)
    db.session.commit()
    flash('Email deleted.', 'success')
    return redirect(url_for('outreach.outreach_list'))
