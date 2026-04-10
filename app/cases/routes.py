import json
from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import LegalCase, Lawyer, OutreachEmail
from app.cases.tracker import scan_for_cases
from app.ai.gemma import search_cases

cases_bp = Blueprint('cases', __name__)


@cases_bp.route('/dashboard')
@login_required
def dashboard():
    total_cases = LegalCase.query.count()
    active_cases = LegalCase.query.filter_by(status='active').count()
    total_lawyers = Lawyer.query.filter(Lawyer.email.isnot(None)).count()
    emails_sent = OutreachEmail.query.filter_by(user_id=current_user.id, status='sent').count()
    pending_followups = OutreachEmail.query.filter_by(
        user_id=current_user.id, status='pending_followup'
    ).count()

    recent_cases = (
        LegalCase.query
        .order_by(LegalCase.trending_score.desc())
        .limit(5)
        .all()
    )

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
        emails_sent=emails_sent,
        pending_followups=pending_followups,
        recent_cases=recent_cases,
        recent_emails=recent_emails,
    )


@cases_bp.route('/cases')
@login_required
def case_list():
    query = request.args.get('q', '').strip()
    status_filter = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)

    q = LegalCase.query
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    q = q.order_by(LegalCase.trending_score.desc())

    if query:
        # Try AI search if available, fall back to LIKE
        cases_page = q.paginate(page=page, per_page=50, error_out=False)
        cases_data = [{'id': c.id, 'title': c.title, 'summary': c.summary} for c in cases_page.items]
        ranked = search_cases(query, cases_data)
        ranked_ids = [c['id'] for c in ranked]
        cases = LegalCase.query.filter(LegalCase.id.in_(ranked_ids)).all()
        id_order = {cid: i for i, cid in enumerate(ranked_ids)}
        cases.sort(key=lambda c: id_order.get(c.id, 999))
        return render_template('cases.html', cases=cases, query=query, status=status_filter, pagination=None)

    cases_page = q.paginate(page=page, per_page=20, error_out=False)
    return render_template(
        'cases.html',
        cases=cases_page.items,
        query=query,
        status=status_filter,
        pagination=cases_page,
    )


@cases_bp.route('/cases/<int:case_id>')
@login_required
def case_detail(case_id):
    case = db.get_or_404(LegalCase, case_id)
    analysis = json.loads(case.ai_analysis) if case.ai_analysis else {}
    emails = OutreachEmail.query.filter_by(case_id=case_id, user_id=current_user.id).all()
    return render_template('case_detail.html', case=case, analysis=analysis, emails=emails)


@cases_bp.route('/cases/<int:case_id>/update-status', methods=['POST'])
@login_required
def update_case_status(case_id):
    case = db.get_or_404(LegalCase, case_id)
    new_status = request.form.get('status')
    if new_status in ('active', 'monitoring', 'archived'):
        case.status = new_status
        db.session.commit()
        flash(f'Case status updated to {new_status}.', 'success')
    return redirect(url_for('cases.case_detail', case_id=case_id))


@cases_bp.route('/cases/scan', methods=['POST'])
@login_required
def trigger_scan():
    try:
        new_cases = scan_for_cases()
        flash(f'Scan complete. Found {len(new_cases)} new cases.', 'success')
    except Exception as e:
        flash(f'Scan error: {str(e)}', 'error')
    return redirect(url_for('cases.case_list'))


@cases_bp.route('/cases/clear', methods=['POST'])
@login_required
def clear_cases():
    """Delete all cases and associated data, then rescan."""
    try:
        OutreachEmail.query.delete()
        Lawyer.query.delete()
        LegalCase.query.delete()
        db.session.commit()
        flash('All old cases cleared.', 'info')

        new_cases = scan_for_cases()
        flash(f'Fresh scan complete. Found {len(new_cases)} new cases.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {str(e)}', 'error')
    return redirect(url_for('cases.case_list'))


@cases_bp.route('/cases/debug')
@login_required
def debug_api():
    """Test the AI API and Google Search to diagnose scan issues."""
    from google import genai
    from google.genai import types
    import traceback

    results = {}
    api_key = current_app.config.get('GOOGLE_AI_API_KEY')
    model_name = current_app.config.get('GEMMA_MODEL', 'gemma-4-27b-it')
    results['api_key_set'] = bool(api_key)
    results['model_name'] = model_name

    if not api_key:
        return jsonify(results)

    client = genai.Client(api_key=api_key)

    # List available models
    try:
        models = []
        for m in client.models.list():
            name = m.name if hasattr(m, 'name') else str(m)
            if 'gemma' in name.lower() or 'gemini' in name.lower():
                models.append(name)
        results['available_models'] = models[:20]
    except Exception as e:
        results['list_models_error'] = str(e)

    # Test basic generation
    try:
        resp = client.models.generate_content(
            model=model_name,
            contents='Say hello in one word.'
        )
        results['basic_generation'] = resp.text[:200]
    except Exception as e:
        results['basic_generation_error'] = str(e)
        results['basic_traceback'] = traceback.format_exc()

    # Test Google Search
    try:
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
        resp = client.models.generate_content(
            model=model_name,
            contents='Search for the latest Indian Supreme Court case from April 2026. Return the case title.',
            config=config,
        )
        results['search_generation'] = resp.text[:500]

        # Check grounding metadata
        sources = []
        for candidate in resp.candidates:
            meta = getattr(candidate, 'grounding_metadata', None)
            if meta:
                chunks = getattr(meta, 'grounding_chunks', [])
                for chunk in chunks:
                    web = getattr(chunk, 'web', None)
                    if web:
                        sources.append(getattr(web, 'uri', ''))
        results['search_sources'] = sources[:5]
    except Exception as e:
        results['search_error'] = str(e)
        results['search_traceback'] = traceback.format_exc()

    # If search failed with configured model, try gemini-2.0-flash
    if 'search_error' in results:
        try:
            resp = client.models.generate_content(
                model='gemini-2.0-flash',
                contents='Search for the latest Indian Supreme Court case. Return the title.',
                config=config,
            )
            results['gemini_flash_search'] = resp.text[:500]
        except Exception as e:
            results['gemini_flash_error'] = str(e)

    return jsonify(results)


@cases_bp.route('/lawyers/<int:lawyer_id>/update-email', methods=['POST'])
@login_required
def update_lawyer_email(lawyer_id):
    lawyer = db.get_or_404(Lawyer, lawyer_id)
    email = request.form.get('email', '').strip()
    if email:
        lawyer.email = email
        db.session.commit()
        flash(f'Email updated for {lawyer.name}.', 'success')
    return redirect(url_for('cases.case_detail', case_id=lawyer.case_id))
