import json
import logging
from app.ai.gemma import generate_outreach_email

logger = logging.getLogger(__name__)


def _fill_placeholders(text: str, sender_name: str, sender_org: str) -> str:
    """Replace common AI-generated placeholder strings with real values."""
    if not text:
        return text
    org = sender_org or sender_name  # fallback to name if no org set
    replacements = {
        '[Your Name]': sender_name,
        '[your name]': sender_name,
        '[Your name]': sender_name,
        '[NAME]': sender_name,
        '[Client Name]': org,
        '[client name]': org,
        '[Client name]': org,
        '[fintech/startup/corporate client]': org,
        '[Fintech/Startup/Corporate Client]': org,
        '[Company/Firm Name]': org,
        '[company/firm name]': org,
        '[Your Firm/Company Name]': org,
        '[Your Organisation]': org,
        '[Your Company]': org,
    }
    for placeholder, value in replacements.items():
        if value:  # don't replace with empty string
            text = text.replace(placeholder, value)
    return text


def generate_email(lawyer, case, email_type='primary', sender_name='', sender_org=''):
    """
    Generate a personalized outreach email for a lawyer about a case.
    Tries AI first; falls back to a template if AI is unavailable.
    Returns {'subject': str, 'body': str}.
    """
    # Extract court and practice area from AI analysis if available
    court = ''
    practice_area = ''
    if case.ai_analysis:
        try:
            analysis = json.loads(case.ai_analysis)
            court = analysis.get('court', '')
            practice_area = analysis.get('practice_area', '')
        except Exception:
            pass

    result = generate_outreach_email(
        lawyer_name=lawyer.name,
        lawyer_firm=lawyer.firm or '',
        lawyer_role=lawyer.role or 'Advocate',
        case_title=case.title,
        case_summary=case.summary or '',
        court=court,
        practice_area=practice_area,
        email_type=email_type,
        sender_name=sender_name,
        sender_org=sender_org,
    )

    if result:
        subject = _fill_placeholders(result.get('subject', f'Regarding: {case.title}'), sender_name, sender_org)
        body = _fill_placeholders(result.get('body', ''), sender_name, sender_org)
        return {'subject': subject, 'body': body}

    # AI unavailable — use contextual fallback templates
    logger.warning(f"[EMAIL GEN] AI unavailable for {lawyer.name} / {case.title[:40]}; using fallback")

    role = lawyer.role or 'Advocate'
    article = 'an' if role and role[0].lower() in 'aeiou' else 'a'
    firm_clause = f' at {lawyer.firm}' if lawyer.firm else ''
    area_clause = f' in {practice_area.title()}' if practice_area else ''
    court_clause = f' before the {court}' if court else ''
    sign_off = f'\n\nBest regards,\n{sender_name}' if sender_name else '\n\nBest regards'

    if email_type == 'followup':
        return {
            'subject': f'Following up: {case.title[:50]}',
            'body': (
                f"Dear {lawyer.name},\n\n"
                f"I wanted to follow up on my earlier email regarding {case.title}"
                f"{court_clause}. "
                f"I understand you're extremely busy, and I appreciate your time.\n\n"
                f"I'd still welcome a brief 15-minute conversation when convenient."
                f"{sign_off}"
            ),
        }

    return {
        'subject': f'Regarding your role in {case.title[:50]}',
        'body': (
            f"Dear {lawyer.name},\n\n"
            f"I'm reaching out regarding your work as {article} {role}{firm_clause} "
            f"in {case.title}{court_clause}{area_clause}.\n\n"
            f"We represent clients with needs in this practice area and would value "
            f"the opportunity to connect with you for a brief introductory call.\n\n"
            f"Would you have 15 minutes available this week or next?"
            f"{sign_off}"
        ),
    }
