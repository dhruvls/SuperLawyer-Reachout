import json
import logging
from app.ai.gemma import generate_outreach_email

logger = logging.getLogger(__name__)


def generate_email(lawyer, case, email_type='primary'):
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
    )

    if result:
        return {
            'subject': result.get('subject', f'Regarding: {case.title}'),
            'body': result.get('body', ''),
        }

    # AI unavailable — use contextual fallback templates
    logger.warning(f"[EMAIL GEN] AI unavailable for {lawyer.name} / {case.title[:40]}; using fallback")

    role = lawyer.role or 'Advocate'
    article = 'an' if role and role[0].lower() in 'aeiou' else 'a'
    firm_clause = f' at {lawyer.firm}' if lawyer.firm else ''
    area_clause = f' in {practice_area.title()}' if practice_area else ''
    court_clause = f' before the {court}' if court else ''

    if email_type == 'followup':
        return {
            'subject': f'Following up: {case.title[:50]}',
            'body': (
                f"Dear {lawyer.name},\n\n"
                f"I wanted to follow up on my earlier email regarding {case.title}"
                f"{court_clause}. "
                f"I understand you're extremely busy, and I appreciate your time.\n\n"
                f"I'd still welcome a brief 15-minute conversation when convenient.\n\n"
                f"Best regards"
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
            f"Would you have 15 minutes available this week or next?\n\n"
            f"Best regards"
        ),
    }
