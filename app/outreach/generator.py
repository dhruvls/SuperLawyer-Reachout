from app.ai.gemma import generate_outreach_email


def generate_email(lawyer, case, email_type='primary'):
    """Generate a personalized email for a lawyer about a case."""
    result = generate_outreach_email(
        lawyer_name=lawyer.name,
        lawyer_firm=lawyer.firm or 'Unknown Firm',
        lawyer_role=lawyer.role or 'Attorney',
        case_title=case.title,
        case_summary=case.summary or '',
        email_type=email_type,
    )

    if result:
        return {
            'subject': result.get('subject', f'Regarding: {case.title}'),
            'body': result.get('body', ''),
        }

    # Fallback if AI is unavailable
    if email_type == 'followup':
        return {
            'subject': f'Following up: {case.title}',
            'body': (
                f"Dear {lawyer.name},\n\n"
                f"I wanted to follow up on my previous email regarding {case.title}. "
                f"I understand you're busy, but I'd appreciate the opportunity to connect.\n\n"
                f"Best regards"
            ),
        }

    return {
        'subject': f'Regarding: {case.title}',
        'body': (
            f"Dear {lawyer.name},\n\n"
            f"I'm reaching out regarding your involvement in {case.title}. "
            f"As {'a ' + lawyer.role + ' at ' + lawyer.firm if lawyer.firm else 'an attorney'} "
            f"working on this matter, I'd welcome the chance to discuss this further.\n\n"
            f"Would you be available for a brief conversation?\n\n"
            f"Best regards"
        ),
    }
