import json
import google.generativeai as genai
from flask import current_app


def _get_model():
    api_key = current_app.config.get('GOOGLE_AI_API_KEY')
    if not api_key:
        return None
    genai.configure(api_key=api_key)
    model_name = current_app.config.get('GEMMA_MODEL', 'gemma-3-27b-it')
    return genai.GenerativeModel(model_name)


def analyze_case(title, article_text):
    """Analyze a legal case article and extract structured info."""
    model = _get_model()
    if not model:
        return None

    prompt = f"""Analyze this legal case from the news and return a JSON object with these fields:
- "summary": A 2-3 sentence summary of the case
- "lawyers": A list of objects with "name", "firm", "role" (partner/associate/counsel/judge/prosecutor/defense attorney/unknown)
- "status": Whether the case is "active", "concluded", or "developing"
- "trending_reason": Why this case is newsworthy
- "practice_area": The area of law (e.g. corporate, criminal, IP, employment)

Title: {title}

Article:
{article_text[:3000]}

Return ONLY valid JSON, no markdown fences."""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        current_app.logger.error(f"Gemma analyze_case error: {e}")
        return None


def generate_outreach_email(lawyer_name, lawyer_firm, lawyer_role, case_title, case_summary, email_type='primary'):
    """Generate a personalized outreach email."""
    model = _get_model()
    if not model:
        return None

    if email_type == 'followup':
        prompt = f"""Write a brief, professional follow-up email to a lawyer about a legal case.
This is a follow-up to a previous email that was sent 3-5 days ago.
Keep it short (3-4 sentences max), polite, and reference the original outreach.

Lawyer: {lawyer_name}
Firm: {lawyer_firm}
Role: {lawyer_role}
Case: {case_title}
Case Summary: {case_summary}

Return JSON with "subject" and "body" fields. Body should use \\n for newlines.
Return ONLY valid JSON, no markdown fences."""
    else:
        prompt = f"""Write a personalized, professional outreach email to a lawyer involved in a notable legal case.
The email should:
- Reference their specific involvement in the case
- Be respectful of their role and expertise
- Be concise (under 150 words for the body)
- Have a compelling but professional subject line
- Include a clear call to action

Lawyer: {lawyer_name}
Firm: {lawyer_firm}
Role: {lawyer_role}
Case: {case_title}
Case Summary: {case_summary}

Return JSON with "subject" and "body" fields. Body should use \\n for newlines.
Return ONLY valid JSON, no markdown fences."""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        current_app.logger.error(f"Gemma email generation error: {e}")
        return None


def search_cases(query, cases_data):
    """Use AI to rank/search cases by relevance to a query."""
    model = _get_model()
    if not model:
        return cases_data

    cases_text = "\n".join(
        f"ID:{c['id']} | {c['title']} | {c.get('summary', '')[:100]}"
        for c in cases_data[:20]
    )

    prompt = f"""Given this search query: "{query}"

Rank these legal cases by relevance. Return a JSON list of the case IDs in order of relevance.

Cases:
{cases_text}

Return ONLY a JSON array of ID numbers, e.g. [3, 1, 5]. No markdown fences."""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        ranked_ids = json.loads(text)
        id_order = {cid: i for i, cid in enumerate(ranked_ids)}
        return sorted(cases_data, key=lambda c: id_order.get(c['id'], 999))
    except Exception as e:
        current_app.logger.error(f"Gemma search error: {e}")
        return cases_data
