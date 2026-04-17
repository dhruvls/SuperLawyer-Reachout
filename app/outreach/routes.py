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
    return render_template('outreach.html', emails=emails, status=status_filter,
                           now=datetime.now(timezone.utc))


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

    # For interview emails, load which steps are already sent for the pipeline sidebar
    int_types_done: set = set()
    if email.email_type and email.email_type.startswith('int_') and email.lawyer_id:
        int_emails = OutreachEmail.query.filter(
            OutreachEmail.lawyer_id == email.lawyer_id,
            OutreachEmail.user_id == current_user.id,
            OutreachEmail.email_type.like('int_%'),
        ).all()
        int_types_done = {e.email_type for e in int_emails if e.status in ('sent', 'followed_up')}

    return render_template('edit_email.html', email=email, int_types_done=int_types_done)


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
        now = datetime.now(timezone.utc)
        outreach.sent_at = now

        if outreach.email_type == 'primary':
            # Primary sent — schedule follow-up in 3 days
            outreach.status = 'pending_followup'
            outreach.followup_date = now + timedelta(days=3)

        elif outreach.email_type == 'followup':
            # Follow-up sent — mark this email as sent and close the primary loop
            outreach.status = 'sent'
            primary = OutreachEmail.query.filter_by(
                lawyer_id=lawyer.id,
                user_id=current_user.id,
                email_type='primary',
            ).first()
            if primary:
                primary.status = 'followed_up'

        db.session.commit()
        flash(f'Email sent to {lawyer.email}.', 'success')
    except Exception as e:
        outreach.status = 'failed'
        db.session.commit()
        flash(f'Send failed: {str(e)}', 'error')

    return redirect(url_for('outreach.outreach_list'))


@outreach_bp.route('/outreach/<int:email_id>/retry', methods=['POST'])
@login_required
def retry_email(email_id):
    """Reset a failed email back to draft so the user can edit and resend."""
    outreach = db.get_or_404(OutreachEmail, email_id)
    if outreach.user_id != current_user.id:
        flash('Unauthorized.', 'error')
        return redirect(url_for('outreach.outreach_list'))
    if outreach.status != 'failed':
        flash('Only failed emails can be retried.', 'error')
        return redirect(url_for('outreach.outreach_list'))
    outreach.status = 'draft'
    db.session.commit()
    flash('Email reset to draft. Edit and resend.', 'info')
    return redirect(url_for('outreach.edit_email', email_id=outreach.id))


@outreach_bp.route('/outreach/interview/<int:lawyer_id>/step/<int:step>', methods=['POST'])
@login_required
def generate_interview_step(lawyer_id, step):
    """Generate a draft email for a specific Super Lawyer interview campaign step."""
    from app.outreach.interview_templates import INTERVIEW_STEPS
    from app.ai.gemma import personalize_interview_email, generate_interview_questionnaire

    lawyer = db.get_or_404(Lawyer, lawyer_id)
    case = lawyer.case

    if step not in INTERVIEW_STEPS:
        flash(f'Step {step} is an internal action — no email to generate.', 'warning')
        return redirect(url_for('cases.case_detail', case_id=case.id))

    step_data = INTERVIEW_STEPS[step]
    template_body = step_data['body']

    # Step 5: generate AI questionnaire and embed it in the template
    if step == 5:
        questionnaire = generate_interview_questionnaire(
            lawyer_name=lawyer.name,
            role=lawyer.role or '',
            firm=lawyer.firm or '',
            practice_area=case.practice_area or '',
            court=case.court or '',
        )
        template_body = template_body.replace('{questionnaire}', questionnaire)

    # Personalize the template with lawyer-specific details
    body = personalize_interview_email(
        step_name=step_data['name'],
        template_body=template_body,
        lawyer_name=lawyer.name,
        firm=lawyer.firm or '',
        role=lawyer.role or '',
    )

    outreach = OutreachEmail(
        lawyer_id=lawyer.id,
        case_id=case.id,
        user_id=current_user.id,
        subject=step_data['subject'],
        body=body,
        status='draft',
        email_type=step_data['email_type'],
    )
    db.session.add(outreach)
    db.session.commit()

    flash(f'Step {step} — {step_data["name"]} email drafted.', 'success')
    return redirect(url_for('outreach.edit_email', email_id=outreach.id))


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
