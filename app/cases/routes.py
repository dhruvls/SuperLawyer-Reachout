import io
import csv
import json
import threading
from datetime import datetime, timedelta, timezone
from flask import (Blueprint, render_template, request, flash, redirect,
                   url_for, jsonify, current_app, Response)
from flask_login import login_required, current_user
from app import db
from app.models import LegalCase, Lawyer, OutreachEmail, CaseNote
from app.cases.tracker import scan_for_cases
from app.ai.gemma import search_cases

cases_bp = Blueprint('cases', __name__)

# ── Background scan state ────────────────────────────────────────────────────
_scan_state: dict = {
    'running': False,
    'started_at': None,
    'finished_at': None,
    'result': None,   # summary dict from scan_for_cases()
    'error': None,
}
_scan_lock = threading.Lock()


# ─── Dashboard ───────────────────────────────────────────────

@cases_bp.route('/dashboard')
@login_required
def dashboard():
    total_cases = LegalCase.query.count()
    active_cases = LegalCase.query.filter_by(status='active').count()
    total_lawyers = Lawyer.query.count()
    lawyers_with_email = Lawyer.query.filter(Lawyer.email.isnot(None)).count()
    emails_sent = OutreachEmail.query.filter_by(user_id=current_user.id, status='sent').count()
    drafts = OutreachEmail.query.filter_by(user_id=current_user.id, status='draft').count()

    # Follow-up reminders
    now = datetime.now(timezone.utc)
    overdue_followups = (
        OutreachEmail.query
        .filter_by(user_id=current_user.id, status='pending_followup')
        .filter(OutreachEmail.followup_date < now)
        .order_by(OutreachEmail.followup_date.asc())
        .all()
    )
    upcoming_followups = (
        OutreachEmail.query
        .filter_by(user_id=current_user.id, status='pending_followup')
        .filter(OutreachEmail.followup_date >= now)
        .filter(OutreachEmail.followup_date <= now + timedelta(days=7))
        .order_by(OutreachEmail.followup_date.asc())
        .all()
    )

    # Trending cases
    recent_cases = (
        LegalCase.query
        .order_by(LegalCase.trending_score.desc())
        .limit(5)
        .all()
    )

    # Bookmarked cases
    bookmarked_cases = current_user.bookmarked_cases.order_by(LegalCase.created_at.desc()).limit(5).all()

    # Charts data — practice area distribution
    pa_counts = {}
    for case in LegalCase.query.all():
        pa = case.practice_area or 'Other'
        pa = pa.strip().title()
        if pa:
            pa_counts[pa] = pa_counts.get(pa, 0) + 1
    pa_labels = list(pa_counts.keys())
    pa_values = list(pa_counts.values())

    # Charts data — outreach funnel
    funnel = {'Draft': drafts, 'Sent': emails_sent,
              'Pending Follow-up': OutreachEmail.query.filter_by(user_id=current_user.id, status='pending_followup').count(),
              'Failed': OutreachEmail.query.filter_by(user_id=current_user.id, status='failed').count()}

    # Charts data — cases by status
    status_counts = {
        'Active': LegalCase.query.filter_by(status='active').count(),
        'Monitoring': LegalCase.query.filter_by(status='monitoring').count(),
        'Archived': LegalCase.query.filter_by(status='archived').count(),
    }

    recent_emails = (
        OutreachEmail.query
        .filter_by(user_id=current_user.id)
        .order_by(OutreachEmail.created_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        'dashboard.html',
        total_cases=total_cases,
        active_cases=active_cases,
        total_lawyers=total_lawyers,
        lawyers_with_email=lawyers_with_email,
        emails_sent=emails_sent,
        drafts=drafts,
        overdue_followups=overdue_followups,
        upcoming_followups=upcoming_followups,
        recent_cases=recent_cases,
        recent_emails=recent_emails,
        bookmarked_cases=bookmarked_cases,
        pa_labels=json.dumps(pa_labels),
        pa_values=json.dumps(pa_values),
        funnel=json.dumps(funnel),
        status_counts=json.dumps(status_counts),
    )


# ─── Cases ───────────────────────────────────────────────────

@cases_bp.route('/cases')
@login_required
def case_list():
    query = request.args.get('q', '').strip()
    status_filter = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)

    bookmarked_ids = {c.id for c in current_user.bookmarked_cases}

    q = LegalCase.query
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    q = q.order_by(LegalCase.trending_score.desc())

    if query:
        cases_page = q.paginate(page=page, per_page=50, error_out=False)
        cases_data = [{'id': c.id, 'title': c.title, 'summary': c.summary} for c in cases_page.items]
        ranked = search_cases(query, cases_data)
        ranked_ids = [c['id'] for c in ranked]
        cases = LegalCase.query.filter(LegalCase.id.in_(ranked_ids)).all()
        id_order = {cid: i for i, cid in enumerate(ranked_ids)}
        cases.sort(key=lambda c: id_order.get(c.id, 999))
        return render_template('cases.html', cases=cases, query=query, status=status_filter,
                               pagination=None, bookmarked_ids=bookmarked_ids)

    cases_page = q.paginate(page=page, per_page=20, error_out=False)
    return render_template('cases.html', cases=cases_page.items, query=query,
                           status=status_filter, pagination=cases_page, bookmarked_ids=bookmarked_ids)


@cases_bp.route('/cases/<int:case_id>')
@login_required
def case_detail(case_id):
    case = db.get_or_404(LegalCase, case_id)
    analysis = json.loads(case.ai_analysis) if case.ai_analysis else {}
    emails = OutreachEmail.query.filter_by(case_id=case_id, user_id=current_user.id).all()
    is_bookmarked = case in current_user.bookmarked_cases.all()

    # Build per-lawyer outreach status for smart buttons
    lawyer_outreach = {}
    for lawyer in case.lawyers:
        lawyer_emails = [e for e in emails if e.lawyer_id == lawyer.id]
        primary = next((e for e in lawyer_emails if e.email_type == 'primary'), None)
        followup = next((e for e in lawyer_emails if e.email_type == 'followup'), None)
        if not primary:
            stage = 'none'
        elif primary.status == 'draft':
            stage = 'draft'
        elif primary.status == 'pending_followup':
            stage = 'followup_drafted' if followup else 'pending_followup'
        elif primary.status == 'sent':
            stage = 'followup_drafted' if followup else 'sent'
        elif primary.status == 'followed_up':
            stage = 'done'
        elif primary.status == 'failed':
            stage = 'failed'
        else:
            stage = 'draft'
        lawyer_outreach[lawyer.id] = {
            'stage': stage,
            'primary': primary,
            'followup': followup,
        }

    return render_template('case_detail.html', case=case, analysis=analysis,
                           emails=emails, is_bookmarked=is_bookmarked,
                           lawyer_outreach=lawyer_outreach)


@cases_bp.route('/cases/<int:case_id>/update-status', methods=['POST'])
@login_required
def update_case_status(case_id):
    case = db.get_or_404(LegalCase, case_id)
    new_status = request.form.get('status')
    if new_status in ('active', 'monitoring', 'archived'):
        case.status = new_status
        db.session.commit()
        flash(f'Status updated to {new_status}.', 'success')
    return redirect(url_for('cases.case_detail', case_id=case_id))


@cases_bp.route('/cases/<int:case_id>/bookmark', methods=['POST'])
@login_required
def toggle_bookmark(case_id):
    case = db.get_or_404(LegalCase, case_id)
    if case in current_user.bookmarked_cases.all():
        current_user.bookmarked_cases.remove(case)
        bookmarked = False
    else:
        current_user.bookmarked_cases.append(case)
        bookmarked = True
    db.session.commit()
    if request.headers.get('Accept') == 'application/json':
        return jsonify({'bookmarked': bookmarked})
    return redirect(request.referrer or url_for('cases.case_detail', case_id=case_id))


@cases_bp.route('/cases/<int:case_id>/notes', methods=['POST'])
@login_required
def add_note(case_id):
    case = db.get_or_404(LegalCase, case_id)
    content = request.form.get('content', '').strip()
    if content:
        note = CaseNote(case_id=case.id, user_id=current_user.id, content=content)
        db.session.add(note)
        db.session.commit()
        flash('Note added.', 'success')
    return redirect(url_for('cases.case_detail', case_id=case_id))


@cases_bp.route('/notes/<int:note_id>/delete', methods=['POST'])
@login_required
def delete_note(note_id):
    note = db.get_or_404(CaseNote, note_id)
    if note.user_id != current_user.id:
        flash('Cannot delete this note.', 'error')
        return redirect(url_for('cases.case_detail', case_id=note.case_id))
    case_id = note.case_id
    db.session.delete(note)
    db.session.commit()
    flash('Note deleted.', 'info')
    return redirect(url_for('cases.case_detail', case_id=case_id))


# ─── Scan ────────────────────────────────────────────────────

def _run_scan_in_background(app):
    """Run scan_for_cases() in a background thread with app context."""
    global _scan_state
    with app.app_context():
        try:
            summary = scan_for_cases()
            with _scan_lock:
                _scan_state['running'] = False
                _scan_state['finished_at'] = datetime.now(timezone.utc).isoformat()
                _scan_state['result'] = summary
                _scan_state['error'] = None
        except Exception as e:
            app.logger.error(f"[SCAN THREAD] error: {e}")
            with _scan_lock:
                _scan_state['running'] = False
                _scan_state['finished_at'] = datetime.now(timezone.utc).isoformat()
                _scan_state['error'] = str(e)


@cases_bp.route('/cases/scan', methods=['POST'])
@login_required
def trigger_scan():
    global _scan_state
    with _scan_lock:
        if _scan_state['running']:
            if request.headers.get('Accept') == 'application/json':
                return jsonify({'status': 'already_running'})
            flash('Scan is already running. Check back in a few minutes.', 'info')
            return redirect(url_for('cases.case_list'))

        _scan_state['running'] = True
        _scan_state['started_at'] = datetime.now(timezone.utc).isoformat()
        _scan_state['finished_at'] = None
        _scan_state['result'] = None
        _scan_state['error'] = None

    thread = threading.Thread(
        target=_run_scan_in_background,
        args=(current_app._get_current_object(),),
        daemon=True,
    )
    thread.start()

    if request.headers.get('Accept') == 'application/json':
        return jsonify({'status': 'started'})
    flash('Scan started in the background. This page will update when complete.', 'info')
    return redirect(url_for('cases.case_list'))


@cases_bp.route('/cases/scan/status')
@login_required
def scan_status():
    """Return current scan state as JSON (polled by the UI)."""
    with _scan_lock:
        state = dict(_scan_state)
    return jsonify(state)


@cases_bp.route('/cases/clear', methods=['POST'])
@login_required
def clear_cases():
    """
    Delete all cases and lawyers, preserving user-created outreach emails and notes.
    NOTE: Outreach emails and notes reference case/lawyer FKs — they become orphaned
    but are intentionally kept so user work is not lost.
    """
    global _scan_state
    with _scan_lock:
        if _scan_state['running']:
            flash('Cannot clear while a scan is running.', 'warning')
            return redirect(url_for('cases.case_list'))

    try:
        # Orphan outreach emails (set FK to null-safe) — preserve user drafts/sent history
        # We deliberately do NOT delete OutreachEmail or CaseNote records
        lawyer_count = Lawyer.query.count()
        case_count = LegalCase.query.count()
        Lawyer.query.delete()
        LegalCase.query.delete()
        db.session.commit()
        current_app.logger.info(
            f"[CLEAR] {case_count} cases, {lawyer_count} lawyers deleted by user {current_user.id}"
        )
        flash(f'Cleared {case_count} cases and {lawyer_count} lawyers. Your emails and notes are preserved.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[CLEAR] error: {e}")
        flash(f'Error clearing data: {str(e)}', 'error')
    return redirect(url_for('cases.case_list'))


# ─── Lawyer Directory ────────────────────────────────────────

@cases_bp.route('/lawyers')
@login_required
def lawyer_directory():
    query = request.args.get('q', '').strip()
    email_filter = request.args.get('email_status', 'all')
    page = request.args.get('page', 1, type=int)

    q = Lawyer.query.join(LegalCase)
    if email_filter == 'has_email':
        q = q.filter(Lawyer.email.isnot(None))
    elif email_filter == 'no_email':
        q = q.filter(Lawyer.email.is_(None))
    if query:
        q = q.filter(db.or_(Lawyer.name.ilike(f'%{query}%'), Lawyer.firm.ilike(f'%{query}%')))

    q = q.order_by(Lawyer.created_at.desc())
    lawyers_page = q.paginate(page=page, per_page=25, error_out=False)

    total = Lawyer.query.count()
    with_email = Lawyer.query.filter(Lawyer.email.isnot(None)).count()
    verified_count = Lawyer.query.filter(Lawyer.verified.is_(True)).count()

    return render_template('lawyers.html', lawyers=lawyers_page.items, pagination=lawyers_page,
                           query=query, email_status=email_filter,
                           total=total, with_email=with_email, without_email=total - with_email,
                           verified_count=verified_count)


@cases_bp.route('/lawyers/<int:lawyer_id>/update-email', methods=['POST'])
@login_required
def update_lawyer_email(lawyer_id):
    lawyer = db.get_or_404(Lawyer, lawyer_id)
    email = request.form.get('email', '').strip()
    if email:
        lawyer.email = email
        lawyer.email_source = 'Manual'
        db.session.commit()
        flash(f'Email updated for {lawyer.name}.', 'success')
    return redirect(request.referrer or url_for('cases.case_detail', case_id=lawyer.case_id))


# ─── Export ──────────────────────────────────────────────────

@cases_bp.route('/cases/export/csv')
@login_required
def export_cases_csv():
    cases = LegalCase.query.order_by(LegalCase.trending_score.desc()).all()
    output = io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM for Excel
    writer = csv.writer(output)
    writer.writerow(['ID', 'Title', 'Status', 'Practice Area', 'Court', 'Source',
                     'Published', 'Trending Score', 'Lawyers', 'Created'])
    for c in cases:
        pa = c.practice_area
        court = ''
        if c.ai_analysis:
            try:
                court = json.loads(c.ai_analysis).get('court', '')
            except Exception:
                pass
        writer.writerow([c.id, c.title, c.status, pa, court, c.source_name or '',
                         c.published_date.strftime('%Y-%m-%d') if c.published_date else '',
                         f'{c.trending_score:.1f}', len(c.lawyers),
                         c.created_at.strftime('%Y-%m-%d')])

    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=cases_{datetime.now().strftime("%Y%m%d")}.csv'})


@cases_bp.route('/lawyers/export/csv')
@login_required
def export_lawyers_csv():
    lawyers = Lawyer.query.join(LegalCase).order_by(Lawyer.created_at.desc()).all()
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['Name', 'Firm', 'Role', 'Email', 'Email Source', 'LinkedIn',
                     'Case Title', 'Case ID'])
    for l in lawyers:
        writer.writerow([l.name, l.firm or '', l.role or '', l.email or '',
                         l.email_source or '', l.linkedin_url or '',
                         l.case.title if l.case else '', l.case_id])

    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=lawyers_{datetime.now().strftime("%Y%m%d")}.csv'})


# ─── Debug ───────────────────────────────────────────────────

@cases_bp.route('/cases/debug')
@login_required
def debug_api():
    import traceback
    import requests as req
    import feedparser
    from google import genai
    from google.genai import types

    results = {}
    api_key = current_app.config.get('GOOGLE_AI_API_KEY')
    model_name = current_app.config.get('GEMMA_MODEL', 'gemini-2.0-flash-lite')
    results['api_key_set'] = bool(api_key)
    results['model_name'] = model_name

    if api_key:
        try:
            client = genai.Client(api_key=api_key)
            resp = client.models.generate_content(
                model=model_name,
                contents='Say hello in one word.',
                config=types.GenerateContentConfig(max_output_tokens=20),
            )
            results['generation_ok'] = True
            results['generation_response'] = resp.text[:100]
        except Exception as e:
            results['generation_ok'] = False
            results['generation_error'] = traceback.format_exc()

    try:
        headers = {'User-Agent': 'Mozilla/5.0 Chrome/120.0.0.0'}
        r = req.get(
            "https://www.bing.com/news/search?q=India+Supreme+Court&format=rss&count=3&mkt=en-IN",
            headers=headers, timeout=10,
        )
        feed = feedparser.parse(r.text)
        results['bing_ok'] = True
        results['bing_entries'] = len(feed.entries)
    except Exception as e:
        results['bing_ok'] = False
        results['bing_error'] = str(e)

    try:
        r2 = req.get("https://indiankanoon.org/search/?formInput=supreme+court", headers=headers, timeout=10)
        results['indiankanoon_ok'] = r2.status_code == 200
    except Exception as e:
        results['indiankanoon_ok'] = False
        results['indiankanoon_error'] = str(e)

    return jsonify(results)
