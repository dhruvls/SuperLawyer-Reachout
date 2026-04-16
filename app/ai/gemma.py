import json
from google import genai
from google.genai import types
from flask import current_app

GEMMA_MODEL = 'models/gemma-4-31b-it'   # full model path required for grounding


def _get_client():
    api_key = current_app.config.get('GOOGLE_AI_API_KEY')
    if not api_key:
        current_app.logger.error("GOOGLE_AI_API_KEY not set")
        return None, None
    client = genai.Client(api_key=api_key)
    model = current_app.config.get('GEMMA_MODEL', GEMMA_MODEL)
    return client, model


def _generate(prompt: str, grounding: bool = False) -> str | None:
    """
    Generate content with Gemma 4.
    grounding=True enables Google Search so the model can look up live data.
    """
    client, model = _get_client()
    if not client:
        current_app.logger.error("[AI] No client — GOOGLE_AI_API_KEY missing or invalid")
        return None
    try:
        cfg_kwargs: dict = dict(temperature=0.1, max_output_tokens=4096)
        if grounding:
            cfg_kwargs['tools'] = [types.Tool(google_search=types.GoogleSearch())]
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        return response.text
    except Exception as e:
        current_app.logger.error(f"[AI] generate error (model={model}): {e}")
        raise


def _parse_json(text: str):
    """Parse JSON from model response, handling all markdown fence variants."""
    if not text:
        return None
    text = text.strip()
    # Strip markdown fences (```json, ```JSON, ```, etc.)
    if text.startswith('```'):
        lines = text.split('\n')
        lines = lines[1:]  # drop opening fence line
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find outermost { } or [ ] and parse from there
        for start_char, end_char in [('{', '}'), ('[', ']')]:
            s = text.find(start_char)
            e = text.rfind(end_char)
            if s != -1 and e > s:
                try:
                    return json.loads(text[s:e + 1])
                except json.JSONDecodeError:
                    continue
    return None


def discover_cases_grounded() -> list:
    """
    Use Gemma 4 + Google Search grounding to find trending Indian legal cases
    from the past 7 days — replaces RSS + keyword filter + AI validation in one call.

    Returns a list of case dicts (same shape as analyze_case output).
    """
    prompt = """Search Google News India for the most recent and significant Indian legal cases
from the past 7 days (today's context matters — use search to find current news).

Focus on:
- Supreme Court of India judgments, orders, hearings
- High Court significant orders (Delhi, Bombay, Madras, Calcutta, etc.)
- NCLT / NCLAT insolvency & corporate matters
- SEBI, CCI, NGT regulatory orders
- Notable criminal matters, bail orders, constitutional petitions

For each case found, I need ONLY cases where named advocates/lawyers are mentioned.
Extract 8-12 cases maximum.

Return ONLY a JSON array, no markdown:
[
  {
    "case_name": "Party A v. Party B",
    "court": "Supreme Court of India",
    "summary": "2-3 sentence factual summary of what happened",
    "practice_area": "one of: constitutional/corporate/criminal/tax/ip/environmental/cyber/insolvency/banking/labour/family/real_estate/media/general",
    "status": "pending or decided or reserved",
    "trending_score": 0.0,
    "source_url": "https://...",
    "source_name": "LiveLaw or Bar and Bench or The Hindu or other",
    "lawyers": [
      {"name": "Full Name", "firm": "Firm name or empty", "role": "Senior Advocate / Advocate / ASG / SG", "side": "petitioner or respondent or unknown"}
    ]
  }
]"""

    raw = _generate(prompt, grounding=True)
    if not raw:
        return []
    result = _parse_json(raw)
    if not isinstance(result, list):
        current_app.logger.warning("[AI] discover_cases_grounded: unexpected format")
        return []
    # Clean out entries without lawyers
    return [c for c in result if isinstance(c, dict) and c.get('lawyers') and c.get('case_name')]


def discover_lawyers_grounded(case_name: str, court: str) -> list:
    """
    Use Gemma 4 + Google Search to find advocates appearing in a specific case.
    Supplements the existing multi-source lawyer discovery.
    """
    prompt = f"""Search Google for lawyers and advocates appearing in this Indian legal case:
Case: "{case_name}"
Court: {court or 'Indian court'}

Find the names of Senior Advocates, Advocates, Solicitor General, ASG, or amicus curiae
who are representing parties. Only include people whose full names appear in news or court records.

Return ONLY a JSON array, no markdown:
[{{"name": "Full Name", "firm": "Firm or empty string", "role": "Senior Advocate / Advocate / etc.", "side": "petitioner or respondent or unknown"}}]

If none found, return: []"""

    raw = _generate(prompt, grounding=True)
    if not raw:
        return []
    result = _parse_json(raw)
    if not isinstance(result, list):
        return []
    return [l for l in result if isinstance(l, dict) and l.get('name')
            and l['name'].lower() not in ('unknown', 'n/a', '')]


def discover_contact_grounded(name: str, firm: str) -> tuple[str | None, str | None]:
    """
    Use Gemma 4 + Google Search to find a lawyer's email and LinkedIn.
    Returns (email, linkedin_url).
    """
    firm_str = f' at {firm}' if firm and firm.lower() not in ('', 'n/a', 'unknown firm') else ''
    prompt = f"""Search Google for the contact information of Indian lawyer/advocate: {name}{firm_str}

Look on:
- Their law firm's website (team/contact/people page)
- LawRato.com, AdvocateKhoj.com, JustDial lawyer profiles
- Bar council directories
- LinkedIn profile

Return ONLY this JSON, no markdown:
{{"email": "email@domain.com or null", "linkedin_url": "https://linkedin.com/in/... or null"}}"""

    raw = _generate(prompt, grounding=True)
    if not raw:
        return None, None
    result = _parse_json(raw)
    if not isinstance(result, dict):
        return None, None
    email = result.get('email')
    linkedin = result.get('linkedin_url')
    # Validate email format
    import re
    if email and not re.match(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', email):
        email = None
    if linkedin and 'linkedin.com' not in str(linkedin):
        linkedin = None
    return email or None, linkedin or None


def analyze_case(title: str, article_text: str) -> dict | None:
    """
    Stage 1 of the pipeline: Validate whether an article is a real Indian court case
    and extract structured metadata + involved lawyers.

    Returns None if AI is unavailable.
    Returns dict with is_case=False if article is not a real court proceeding.
    """
    prompt = f"""You are a legal intelligence analyst specializing in Indian courts. Analyze this news article carefully.

STEP 1 — VALIDATION: Is this an ACTUAL COURT CASE?
An actual court case MUST satisfy ALL four of these criteria:
  (a) A specific named court or tribunal (e.g., Supreme Court of India, Delhi High Court, NCLT Mumbai, NGT, ITAT, CCI, SEBI SAT, National Consumer Forum)
  (b) Named parties in adversarial or quasi-adversarial positions (petitioner vs respondent, accused vs prosecution, company vs regulator, appellant vs state)
  (c) A specific legal proceeding that occurred or is occurring (a judgment was delivered, an order was passed, a bail was granted/denied, a PIL was filed/heard, an arbitration award was made)
  (d) At least one named lawyer/advocate/senior counsel/solicitor general representing a party (not just a judge or a party themselves)

If ANY of the four criteria above is NOT met, set "is_case": false.

NOT a real case: general legal news, policy updates, law amendments, opinion pieces, judicial appointments, law firm rankings, legal industry events, law college news, legislative debates.

STEP 2 — EXTRACTION (only if is_case is true):
Extract lawyers who are representing parties. These are advocates, senior advocates, solicitors general, additional solicitors general, counsel for petitioner/respondent, amicus curiae.
DO NOT include judges, justices, or parties themselves as lawyers.
NEVER use "Unknown" as a name — if you cannot find a real name, omit that entry entirely.

STEP 3 — TRENDING SCORE:
Rate how significant/newsworthy this case is for business development (finding new clients):
  0.9+ : Supreme Court constitutional/landmark case, major corporate battle, high-profile criminal matter
  0.7+ : Supreme Court regular matter, High Court significant order, major NCLT/SEBI/CCI proceeding
  0.5+ : High Court routine but notable, state-level tribunal order
  0.3  : District court or minor matter

Return ONLY this JSON (no markdown, no explanation):
{{
  "is_case": true or false,
  "case_name": "Party A v. Party B" or "",
  "court": "Specific court name" or "",
  "practice_area": one of: constitutional, corporate, criminal, tax, ip, environmental, cyber, insolvency, banking, labour, family, real_estate, media, general,
  "summary": "2-3 sentence factual summary of the case and what happened",
  "status": "pending" or "decided" or "reserved",
  "trending_score": 0.0 to 1.0,
  "lawyers": [
    {{"name": "Full Name", "firm": "Firm name or empty string", "role": "Senior Advocate / Advocate / Solicitor General / etc.", "side": "petitioner / respondent / unknown"}}
  ]
}}

Title: {title}

Article:
{article_text[:4000]}"""

    raw = _generate(prompt)   # exceptions propagate — let tracker.py log them
    if not raw:
        return None

    result = _parse_json(raw)
    if not isinstance(result, dict):
        current_app.logger.warning(f"[AI] analyze_case: unexpected format for '{title[:60]}'")
        return None

    # Clean lawyer list
    if 'lawyers' in result:
        result['lawyers'] = [
            l for l in result['lawyers']
            if isinstance(l, dict) and l.get('name', '').lower() not in
               ('unknown', 'n/a', '', 'not mentioned', 'unnamed', 'not specified')
        ]

    return result


def identify_lawyers_from_search(case_title: str, search_text: str) -> list:
    """
    Extract lawyer names from Bing search snippets about a specific case.
    Returns a list of {name, firm, role} dicts.
    """
    prompt = f"""From these web search results about the Indian legal case "{case_title}", extract the names of lawyers and advocates who are REPRESENTING PARTIES in this case.

Rules:
- Only include people who are advocates, senior advocates, solicitors general, or counsel
- Do NOT include judges or justices
- Do NOT include parties/clients themselves
- Only include people whose full names are clearly stated in the text
- NEVER use "Unknown" as a name

Search Results:
{search_text[:3000]}

Return ONLY a JSON array (no markdown, no explanation):
[
  {{"name": "Full Name", "firm": "Firm or empty string", "role": "Senior Advocate / Advocate / etc."}}
]

If no qualifying names found, return: []"""

    raw = _generate(prompt)
    if not raw:
        return []

    result = _parse_json(raw)
    if not isinstance(result, list):
        current_app.logger.warning(f"[AI] identify_lawyers: unexpected format")
        return []

    return [
        l for l in result
        if isinstance(l, dict) and l.get('name', '').lower() not in ('unknown', 'n/a', '')
    ]


def generate_outreach_email(lawyer_name: str, lawyer_firm: str, lawyer_role: str,
                            case_title: str, case_summary: str,
                            court: str = '', practice_area: str = '',
                            email_type: str = 'primary') -> dict | None:
    """
    Generate a personalized outreach email to a lawyer.
    Returns {subject, body} dict or None if AI unavailable.
    """
    context = f"""Lawyer: {lawyer_name}
Firm: {lawyer_firm or 'Unknown Firm'}
Role: {lawyer_role}
Case: {case_title}
Court: {court or 'not specified'}
Practice Area: {practice_area or 'not specified'}
Case Summary: {case_summary}"""

    if email_type == 'followup':
        prompt = f"""Write a brief, professional follow-up email to a lawyer in India. This is a follow-up to an outreach email sent 3-5 days ago. Keep it to 3-4 sentences: acknowledge no response, briefly restate the value, politely ask if they have time for a call.

{context}

Return ONLY this JSON (no markdown):
{{"subject": "Re: [original subject]", "body": "email body using \\n for newlines"}}"""
    else:
        prompt = f"""Write a concise, personalized outreach email to a senior Indian lawyer. The email should:
- Open with a specific reference to their role in this case (show you know the details)
- In one sentence explain why you're reaching out (you represent [fintech/startup/corporate client] with needs in this practice area)
- Ask for a 15-minute introductory call
- Stay under 120 words in the body
- Be formal but direct — Indian legal professionals value clarity

{context}

Return ONLY this JSON (no markdown):
{{"subject": "Compelling subject line under 60 chars", "body": "email body using \\n for newlines"}}"""

    raw = _generate(prompt)
    if not raw:
        current_app.logger.warning(f"[AI] generate_outreach_email: AI unavailable, will use fallback")
        return None

    result = _parse_json(raw)
    if not isinstance(result, dict) or 'subject' not in result or 'body' not in result:
        current_app.logger.warning(f"[AI] generate_outreach_email: missing subject/body in response")
        return None

    return result


def search_cases(query: str, cases_data: list) -> list:
    """Rank stored cases by relevance to a search query using AI."""
    if not cases_data:
        return []

    cases_text = "\n".join(
        f"ID:{c['id']} | {c['title']} | {c.get('summary', '')[:120]}"
        for c in cases_data[:30]
    )

    prompt = f"""Rank these Indian legal cases by relevance to the search query: "{query}"

Cases:
{cases_text}

Return ONLY a JSON array of case IDs in order of relevance (most relevant first).
Example: [5, 2, 8, 1]
No markdown, no explanation."""

    raw = _generate(prompt)
    if not raw:
        return cases_data

    try:
        ranked_ids = _parse_json(raw)
        if not isinstance(ranked_ids, list):
            return cases_data
        id_order = {cid: i for i, cid in enumerate(ranked_ids)}
        return sorted(cases_data, key=lambda c: id_order.get(c['id'], 999))
    except Exception as e:
        current_app.logger.error(f"[AI] search_cases error: {e}")
        return cases_data
