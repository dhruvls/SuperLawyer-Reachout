import json
import google.generativeai as genai
from flask import current_app


def _get_model():
    api_key = current_app.config.get('GOOGLE_AI_API_KEY')
    if not api_key:
        current_app.logger.error("GOOGLE_AI_API_KEY not set")
        return None
    genai.configure(api_key=api_key)
    model_name = current_app.config.get('GEMMA_MODEL', 'gemma-3-27b-it')
    return genai.GenerativeModel(model_name)


def _parse_json(text):
    """Parse JSON from model response, stripping markdown fences."""
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text)


def analyze_case(title, article_text):
    """Analyze a legal case article and extract structured info."""
    model = _get_model()
    if not model:
        return None

    prompt = f"""You are an expert in Indian law. Analyze this legal case news article.

CRITICAL: Extract the ACTUAL FULL NAMES of lawyers and advocates mentioned in the article.
Look for: Senior Advocate, Advocate, Solicitor General, Additional Solicitor General,
Advocate General, Standing Counsel, Amicus Curiae, and any named legal representatives.

DO NOT return "Unknown" as a name. If no lawyer names are explicitly mentioned, return an EMPTY lawyers list.

Return a JSON object with:
- "summary": 2-3 sentence summary
- "lawyers": List of objects with:
  - "name": FULL NAME only (e.g. "Harish Salve", "Kapil Sibal") - NEVER "Unknown"
  - "firm": Law firm or organization
  - "role": senior advocate / advocate / solicitor general / counsel for petitioner / counsel for respondent / amicus curiae
- "status": "active", "concluded", or "developing"
- "trending_reason": Why this case is newsworthy
- "practice_area": Area of law (constitutional, corporate, criminal, tax, IP, environmental, cyber, insolvency)
- "court": Which court (Supreme Court of India, Delhi High Court, NCLT, etc.)

Title: {title}

Article:
{article_text[:3000]}

Return ONLY valid JSON, no markdown fences."""

    try:
        response = model.generate_content(prompt)
        result = _parse_json(response.text)
        if 'lawyers' in result:
            result['lawyers'] = [
                l for l in result['lawyers']
                if l.get('name', '').lower() not in ('unknown', 'n/a', '', 'not mentioned', 'unnamed')
            ]
        return result
    except Exception as e:
        current_app.logger.error(f"analyze_case error: {e}")
        return None


def identify_lawyers_from_search(case_title, search_text):
    """Use AI to extract lawyer names from web search results about a case."""
    model = _get_model()
    if not model:
        return []

    prompt = f"""From these web search results about the Indian legal case "{case_title}",
extract the names of lawyers, advocates, and senior counsel involved.

Search Results:
{search_text[:2500]}

Return a JSON list of objects, each with:
- "name": Full name (MUST be a real person's name, NEVER "Unknown")
- "firm": Law firm or organization
- "role": Their role in the case

Only include people whose full names are clearly stated.
Return ONLY valid JSON, no markdown fences. If no names found, return []."""

    try:
        response = model.generate_content(prompt)
        result = _parse_json(response.text)
        if not isinstance(result, list):
            return []
        return [l for l in result if l.get('name', '').lower() not in ('unknown', 'n/a', '')]
    except Exception as e:
        current_app.logger.error(f"identify_lawyers error: {e}")
        return []


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
        return _parse_json(response.text)
    except Exception as e:
        current_app.logger.error(f"email generation error: {e}")
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
        ranked_ids = _parse_json(response.text)
        id_order = {cid: i for i, cid in enumerate(ranked_ids)}
        return sorted(cases_data, key=lambda c: id_order.get(c['id'], 999))
    except Exception as e:
        current_app.logger.error(f"search error: {e}")
        return cases_data
