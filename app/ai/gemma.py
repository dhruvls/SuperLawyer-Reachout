import json
import re
from datetime import date
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
    Three targeted Gemma 4 + Google Search calls covering different case types.
    Results are merged and deduplicated by case name.

    Lawyer filter REMOVED — cases with no lawyers in the article are still
    returned; discover_lawyers_grounded() will find them in a follow-up search.
    """
    today = date.today().strftime('%B %d, %Y')

    # Three targeted searches covering the full spectrum of Indian courts
    targets = [
        (
            "Supreme Court & Constitutional",
            """\
- Supreme Court of India judgments, orders, hearings (any bench)
- Constitutional petitions (Article 32, Article 226)
- PIL hearings with substantive orders or directions
- Five-judge / seven-judge Constitution bench matters
- Electoral, fundamental rights, criminal appeals admitted/decided by SC
- SLP (Special Leave Petition) disposals with significant directions"""
        ),
        (
            "High Courts & Regulatory Tribunals",
            """\
- Delhi, Bombay, Madras, Calcutta, Allahabad, Karnataka, Telangana High Courts
- NCLT / NCLAT — insolvency, liquidation, resolution plan approvals
- SEBI enforcement orders and SAT (Securities Appellate Tribunal) appeals
- CCI (Competition Commission) orders and NCLAT competition appeals
- NGT (National Green Tribunal) orders
- ITAT (Income Tax Appellate Tribunal) significant tax rulings
- DRT / DRAT debt recovery matters
- TDSAT, TRAI regulatory orders"""
        ),
        (
            "Criminal, Bail, PMLA & Corporate Fraud",
            """\
- High-profile bail orders granted or denied (any court)
- Sessions court significant criminal verdicts
- PMLA / ED arrest, remand, attachment orders
- CBI / NIA chargesheet filings and trial court proceedings
- Corporate fraud and white-collar crime hearings
- Contempt of court proceedings (civil or criminal)
- Extradition matters and international mutual legal assistance"""
        ),
    ]

    all_cases: list = []
    seen_keys: set = set()

    for label, focus in targets:
        prompt = f"""Today is {today}. Search Google News India right now for the most recent Indian legal cases from the past 7 days.

CATEGORY: {label}
Search for proceedings in these specific courts and case types:
{focus}

Rules for what counts as a valid case:
- Must be a real court/tribunal proceeding (judgment delivered, order passed, bail granted/denied, hearing concluded with directions)
- Must have specific named parties — not just "government vs company"
- Exclude: law firm rankings, judicial appointments, legislative debates, opinion pieces, law college events

Extract up to 6 cases for this category.
For lawyers: include whoever is named in the news — advocates, senior advocates, SG, ASG, amicus curiae.
If the article does not name any lawyers, still include the case with "lawyers": [] — we will find them separately.

Return ONLY a JSON array, no markdown, no explanation:
[
  {{
    "case_name": "Party A v. Party B",
    "court": "Exact court or tribunal name",
    "summary": "2-3 sentence factual summary of what specifically happened in this proceeding",
    "practice_area": "one of: constitutional/corporate/criminal/tax/ip/environmental/cyber/insolvency/banking/labour/family/real_estate/media/general",
    "status": "pending or decided or reserved",
    "trending_score": 0.0,
    "source_url": "https://...",
    "source_name": "LiveLaw or Bar and Bench or The Hindu or Economic Times or other",
    "lawyers": [
      {{"name": "Full Name", "firm": "Firm name or empty string", "role": "Senior Advocate / Advocate / ASG / SG / Amicus", "side": "petitioner or respondent or unknown"}}
    ]
  }}
]

If no cases found for this category, return: []"""

        try:
            raw = _generate(prompt, grounding=True)
            if not raw:
                current_app.logger.warning(f"[AI] discover_cases_grounded ({label}): empty response")
                continue
            result = _parse_json(raw)
            if not isinstance(result, list):
                current_app.logger.warning(f"[AI] discover_cases_grounded ({label}): unexpected format")
                continue
            added = 0
            for c in result:
                if not isinstance(c, dict) or not c.get('case_name'):
                    continue
                key = c['case_name'][:60].lower().strip()
                if key not in seen_keys:
                    seen_keys.add(key)
                    # Ensure lawyers key always exists
                    c.setdefault('lawyers', [])
                    all_cases.append(c)
                    added += 1
            current_app.logger.info(f"[AI] discover_cases_grounded ({label}): {added} cases")
        except Exception as e:
            current_app.logger.error(f"[AI] discover_cases_grounded ({label}): {e}")
            continue

    # Sort by trending_score descending so highest-impact cases are processed first
    all_cases.sort(key=lambda c: c.get('trending_score', 0), reverse=True)
    current_app.logger.info(f"[AI] discover_cases_grounded: {len(all_cases)} total cases across 3 searches")
    return all_cases


def discover_lawyers_grounded(case_name: str, court: str) -> list:
    """
    Aggressive Gemma 4 + Google Search call to find ALL advocates in a case.
    This is the primary recovery path when Prompt 1 returned no lawyers.
    Searches LiveLaw, Bar & Bench, IndianKanoon, SCC Online, and news sources.
    """
    prompt = f"""Search Google thoroughly for lawyers and advocates appearing in this Indian legal case:

Case: "{case_name}"
Court: {court or 'Indian court'}

Search these sources specifically:
- livelaw.in — India's leading legal news site
- barandbench.com — Bar and Bench legal news
- indiankanoon.org — Indian court judgments database
- scconline.com — SCC Online case law
- sci.gov.in — Supreme Court of India website
- Any news coverage mentioning this case

Find ALL of the following who appear in this case:
- Senior Advocates (Sr. Adv. / Senior Counsel)
- Advocates / Counsel
- Solicitor General of India (SG)
- Additional Solicitor General (ASG)
- Advocate General (AG) of any state
- Amicus Curiae
- Government Pleader / State Counsel
- Special Public Prosecutor

For EACH person found:
- Use their full name as it appears in the source
- Note which side they represent (petitioner/appellant/respondent/state/unknown)
- Note their firm if mentioned
- Note their exact role/designation

STRICT RULES:
- Do NOT include judges, justices, or judicial officers
- Do NOT include the parties/litigants themselves
- Do NOT fabricate or guess names — only include names that actually appear in search results
- If you genuinely cannot find any lawyer names, return []

Return ONLY a JSON array, no markdown, no explanation:
[{{"name": "Full Name", "firm": "Firm name or empty string", "role": "Senior Advocate / Advocate / ASG / SG / etc.", "side": "petitioner or respondent or amicus or unknown"}}]"""

    raw = _generate(prompt, grounding=True)
    if not raw:
        return []
    result = _parse_json(raw)
    if not isinstance(result, list):
        return []
    blocked = {'unknown', 'n/a', '', 'not found', 'not mentioned', 'not available',
               'not specified', 'unnamed', 'various'}
    return [
        l for l in result
        if isinstance(l, dict) and l.get('name')
        and l['name'].lower().strip() not in blocked
        and len(l['name'].strip()) > 3
    ]


def discover_contact_grounded(name: str, firm: str) -> tuple[str | None, str | None]:
    """
    Use Gemma 4 + Google Search to find a lawyer's email and LinkedIn.
    Returns (email, linkedin_url).
    """
    firm_str = f' at {firm}' if firm and firm.lower() not in ('', 'n/a', 'unknown firm') else ''
    prompt = f"""Search Google for the professional contact information of this Indian lawyer/advocate:
Name: {name}{firm_str}

Search these sources in order:
1. Their law firm's official website — look for /team, /people, /attorneys, /contact, /partners pages
2. LawRato.com lawyer profiles
3. AdvocateKhoj.com bar directory
4. JustDial lawyer listings
5. Bar Council of India or State Bar Council directories
6. LinkedIn profile (linkedin.com/in/...)
7. Any legal directory or professional profile page

Only return a real email address you actually found on one of these pages.
Do NOT guess or construct email addresses from name patterns.
Do NOT return generic firm contact emails (like info@firm.com) unless it's clearly the lawyer's direct contact.

Return ONLY this JSON, no markdown, no explanation:
{{"email": "actual.email@domain.com or null", "linkedin_url": "https://linkedin.com/in/profilename or null"}}"""

    raw = _generate(prompt, grounding=True)
    if not raw:
        return None, None
    result = _parse_json(raw)
    if not isinstance(result, dict):
        return None, None
    email = result.get('email')
    linkedin = result.get('linkedin_url')
    # Validate — reject nulls passed as strings, generic emails, bad formats
    _NULL_STRINGS = {'null', 'none', 'n/a', '', 'not found', 'not available'}
    if email and str(email).lower().strip() in _NULL_STRINGS:
        email = None
    if email and not re.match(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', str(email)):
        email = None
    if linkedin and str(linkedin).lower().strip() in _NULL_STRINGS:
        linkedin = None
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
                            email_type: str = 'primary',
                            sender_name: str = '', sender_org: str = '') -> dict | None:
    """
    Generate a personalized outreach email to a lawyer.
    Returns {subject, body} dict or None if AI unavailable.
    """
    sender_line = f"Sender: {sender_name}" if sender_name else ""
    org_line = f"Sender Organisation: {sender_org}" if sender_org else ""
    context = f"""Lawyer: {lawyer_name}
Firm: {lawyer_firm or 'Unknown Firm'}
Role: {lawyer_role}
Case: {case_title}
Court: {court or 'not specified'}
Practice Area: {practice_area or 'not specified'}
Case Summary: {case_summary}
{sender_line}
{org_line}""".strip()

    if email_type == 'followup':
        prompt = f"""Write a brief, professional follow-up email to a lawyer in India. This is a follow-up to an outreach email sent 3-5 days ago. Keep it to 3-4 sentences: acknowledge no response, briefly restate the value, politely ask if they have time for a call.

{context}

Sign the email with the sender name provided (or leave as "Best regards" if no sender name given).
Return ONLY this JSON (no markdown):
{{"subject": "Re: [original subject]", "body": "email body using \\n for newlines"}}"""
    else:
        org_ref = sender_org or 'our client'
        prompt = f"""Write a concise, personalized outreach email to a senior Indian lawyer. The email should:
- Open with a specific reference to their role in this case (show you know the details)
- In one sentence explain why you're reaching out (you represent {org_ref} with needs in this practice area)
- Ask for a 15-minute introductory call
- Stay under 120 words in the body
- Be formal but direct — Indian legal professionals value clarity
- Sign off with the sender name provided

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


def add_interview_personalization(body: str, lawyer_name: str, firm: str, role: str) -> str | None:
    """
    Ask AI to insert exactly ONE personalization sentence into the Step 1 interview invite,
    after the "Warm greetings from SuperLawyer!" line. All other text stays verbatim.
    Returns modified body or None on failure (caller keeps original).
    """
    firm_str = f' at {firm}' if firm else ''
    prompt = f"""The following is an email body. After the line "Warm greetings from SuperLawyer!" insert exactly ONE sentence that specifically references {lawyer_name}'s work as {role or 'Advocate'}{firm_str}. Keep every other word completely identical. Return only the full modified email body, no explanation, no markdown.

{body}"""
    try:
        result = _generate(prompt)
        # Sanity check: result must be close in length to original (not truncated/rewritten)
        if result and len(result.strip()) > len(body) * 0.75:
            return result.strip()
    except Exception as e:
        current_app.logger.warning(f"[AI] add_interview_personalization failed: {e}")
    return None


def generate_interview_questionnaire(lawyer_name: str, role: str, firm: str,
                                     practice_area: str, court: str) -> str:
    """
    Generate 8-10 tailored interview questions using the Cinderella Arc Method.
    Used at Step 5 to prepare the questionnaire embedded in the email body.
    Returns the questions as plain numbered text.
    """
    prompt = f"""You are preparing a written interview questionnaire for a SuperLawyer feature article.

Lawyer: {lawyer_name}
Role: {role or 'Advocate'}
Firm: {firm or 'Independent'}
Practice Area: {practice_area or 'General'}
Court/Jurisdiction: {court or 'India'}

Use the Cinderella Arc Method to structure 8-10 questions covering:
1. Humble beginnings — early inspiration and entry into law
2. Mentorship and early rise — formative experiences and influential mentors
3. Turning point — the defining moment in their career trajectory
4. Key challenges — obstacles overcome and growth achieved
5. Future outlook — vision, goals, and advice for the next generation

Guidelines:
- Each question must be crisp, precise, and preferably one line
- Tailor questions to their specific practice area and court/jurisdiction
- Maximum 10 questions
- Number each question (1. 2. 3. ...)
- No preamble, no explanations — just the numbered questions

Return ONLY the numbered list of questions:"""

    try:
        raw = _generate(prompt)
        if raw and raw.strip():
            return raw.strip()
    except Exception as e:
        current_app.logger.warning(f"[AI] generate_interview_questionnaire failed: {e}")

    # Fallback generic questionnaire
    return (
        "1. What first drew you to the legal profession, and what was your early journey like?\n"
        "2. Who were the mentors who most influenced your growth as a lawyer?\n"
        "3. What was the turning point that shaped the direction of your career?\n"
        "4. Can you describe a particularly challenging case and what you learned from it?\n"
        "5. How has the legal landscape in India evolved during your years of practice?\n"
        "6. What does a typical day look like for you, and how do you balance professional demands?\n"
        "7. What is the most important quality a lawyer must develop to succeed today?\n"
        "8. What advice would you give to young lawyers just starting their careers?\n"
        "9. What are your goals and aspirations for the next phase of your journey?\n"
        "10. What moment in your career are you most proud of, and why?"
    )


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
