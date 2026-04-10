import json
from google import genai
from google.genai import types
from flask import current_app


def _get_client():
    api_key = current_app.config.get('GOOGLE_AI_API_KEY')
    if not api_key:
        return None
    return genai.Client(api_key=api_key)


def _model():
    return current_app.config.get('GEMMA_MODEL', 'gemma-4-27b-it')


def _parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    return json.loads(text)


def _generate(prompt, use_search=False):
    """Generate content, optionally with Google Search grounding."""
    client = _get_client()
    if not client:
        return None

    config = None
    if use_search:
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )

    response = client.models.generate_content(
        model=_model(),
        contents=prompt,
        config=config,
    )
    return response


def _extract_sources(response):
    """Extract source URLs from Google Search grounding metadata."""
    sources = []
    try:
        for candidate in response.candidates:
            meta = getattr(candidate, 'grounding_metadata', None)
            if not meta:
                continue
            chunks = getattr(meta, 'grounding_chunks', [])
            for chunk in chunks:
                web = getattr(chunk, 'web', None)
                if web:
                    sources.append({
                        'url': getattr(web, 'uri', ''),
                        'title': getattr(web, 'title', ''),
                    })
    except Exception:
        pass
    return sources


def search_legal_news(query):
    """Use Gemma + Google Search to find recent Indian legal cases."""
    prompt = f"""Search for recent Indian legal news (past 2 weeks) about: {query}

For each real news article found, return:
- "title": Article headline
- "summary": 2-3 sentence case summary
- "source_url": URL of the article
- "source_name": News source (e.g. "LiveLaw", "Bar and Bench", "The Hindu", "Economic Times")
- "lawyers": List of named lawyers/advocates with "name", "firm", "role" for each
- "practice_area": Area of law
- "court": Which court
- "status": "active", "concluded", or "developing"

IMPORTANT:
- Only include lawyers whose REAL FULL NAMES appear in sources. Never "Unknown".
- Only include real articles from real Indian news sources.
- Return 3-5 articles if available.

Return ONLY a valid JSON array, no markdown fences."""

    try:
        response = _generate(prompt, use_search=True)
        if not response:
            return []

        articles = _parse_json(response.text)
        if not isinstance(articles, list):
            articles = [articles]

        sources = _extract_sources(response)
        for i, article in enumerate(articles):
            if not article.get('source_url') and i < len(sources):
                article['source_url'] = sources[i]['url']
            if not article.get('source_name') and i < len(sources):
                article['source_name'] = sources[i].get('title', 'News')
            if 'lawyers' in article:
                article['lawyers'] = [
                    l for l in article['lawyers']
                    if l.get('name', '').lower() not in ('unknown', 'n/a', '', 'not mentioned')
                ]

        current_app.logger.warning(f"Google Search [{query[:40]}]: {len(articles)} articles")
        return articles
    except Exception as e:
        current_app.logger.error(f"Google Search news error: {e}")
        return []


def search_case_lawyers(case_title):
    """Use Gemma + Google Search to find lawyers involved in an Indian legal case."""
    prompt = f"""Search for the lawyers, advocates, and senior counsel involved in this Indian legal case: "{case_title}"

For each lawyer found, provide:
- "name": Full name (MUST be a real person, NEVER "Unknown")
- "firm": Law firm or organization
- "role": Their role (senior advocate, advocate, counsel for petitioner, solicitor general, etc.)

Return ONLY a valid JSON array. If none found, return []. No markdown fences."""

    try:
        response = _generate(prompt, use_search=True)
        if not response:
            return []
        result = _parse_json(response.text)
        if not isinstance(result, list):
            result = []
        return [l for l in result if l.get('name', '').lower() not in ('unknown', 'n/a', '')]
    except Exception as e:
        current_app.logger.error(f"Google Search lawyers error: {e}")
        return []


def search_lawyers_contact(case_title, lawyers):
    """Use Gemma + Google Search to find email and LinkedIn for lawyers."""
    if not lawyers:
        return []

    lawyers_text = '\n'.join(
        f"- {l.get('name', '')} ({l.get('firm', '')}, {l.get('role', '')})"
        for l in lawyers
    )

    prompt = f"""Search for the professional contact information of these Indian lawyers involved in "{case_title}":

{lawyers_text}

For each lawyer, search for their:
- Professional email address (from law firm website, legal directory, Bar Council, LinkedIn)
- LinkedIn profile URL

Return a JSON array where each object has:
- "name": The lawyer's name (same as input)
- "email": Professional email address or null if not found
- "linkedin": LinkedIn URL or null if not found
- "email_source": The website URL where the email was found, or null

Return ONLY valid JSON, no markdown fences."""

    try:
        response = _generate(prompt, use_search=True)
        if not response:
            return []

        result = _parse_json(response.text)
        if not isinstance(result, list):
            result = []

        sources = _extract_sources(response)
        for item in result:
            if not item.get('email_source') and sources:
                item['email_source'] = sources[0]['url']

        return result
    except Exception as e:
        current_app.logger.error(f"Google Search contact error: {e}")
        return []


def analyze_case(title, article_text):
    """Analyze a legal case article and extract structured info."""
    prompt = f"""You are an expert in Indian law. Analyze this legal case news article.

Extract ACTUAL FULL NAMES of lawyers/advocates mentioned. Never return "Unknown".
If no names are mentioned, return an EMPTY lawyers list.

Return a JSON object with:
- "summary": 2-3 sentence summary
- "lawyers": List with "name", "firm", "role" for each (real names only)
- "status": "active", "concluded", or "developing"
- "trending_reason": Why this case is newsworthy
- "practice_area": Area of law
- "court": Which court

Title: {title}

Article:
{article_text[:3000]}

Return ONLY valid JSON, no markdown fences."""

    try:
        response = _generate(prompt)
        if not response:
            return None
        result = _parse_json(response.text)
        if 'lawyers' in result:
            result['lawyers'] = [
                l for l in result['lawyers']
                if l.get('name', '').lower() not in ('unknown', 'n/a', '', 'not mentioned')
            ]
        return result
    except Exception as e:
        current_app.logger.error(f"Gemma analyze_case error: {e}")
        return None


def generate_outreach_email(lawyer_name, lawyer_firm, lawyer_role, case_title, case_summary, email_type='primary'):
    """Generate a personalized outreach email."""
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
        response = _generate(prompt)
        if not response:
            return None
        return _parse_json(response.text)
    except Exception as e:
        current_app.logger.error(f"Gemma email generation error: {e}")
        return None


def search_cases(query, cases_data):
    """Use AI to rank/search cases by relevance to a query."""
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
        response = _generate(prompt)
        if not response:
            return cases_data
        ranked_ids = _parse_json(response.text)
        id_order = {cid: i for i, cid in enumerate(ranked_ids)}
        return sorted(cases_data, key=lambda c: id_order.get(c['id'], 999))
    except Exception as e:
        current_app.logger.error(f"Gemma search error: {e}")
        return cases_data
