"""
Case discovery pipeline — AI-grounded.

Stages:
  0. discover_cases_grounded()   — Gemma 4 + Google Search finds trending cases
  1. _multi_source_lawyers()     — Bing + IndianKanoon + regex lawyer enrichment
  2. _discover_contacts()        — email + LinkedIn per lawyer
  3. _save_case()                — persist to DB

scan_for_cases() returns a summary dict for the UI.
"""

import re
import json
import requests
from datetime import datetime, timezone
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from flask import current_app
from app import db
from app.models import LegalCase, Lawyer
from app.ai.gemma import (identify_lawyers_from_search,
                           discover_cases_grounded, discover_lawyers_grounded,
                           discover_contact_grounded)


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36'
}

MAX_NEW_CASES = 15

# ── Name normalization ───────────────────────────────────────────────────────

LEGAL_TITLES = [
    'senior advocate', 'sr. advocate', 'sr advocate',
    'advocate', 'adv.', 'adv',
    'justice', "hon'ble justice", "hon'ble",
    'solicitor general', 'attorney general',
    'additional solicitor general', 'asg',
    'senior counsel', 'counsel',
    'mr.', 'mr', 'ms.', 'ms', 'smt.', 'smt', 'dr.', 'dr', 'shri',
]

NAME_BLOCKLIST = {
    'the court', 'the bench', 'the judge', 'supreme court', 'high court',
    'district court', 'sessions court', 'india', 'government', 'state',
    'union of india', 'central government', 'state government',
    'bench', 'division bench', 'court',
}

# Roles that identify parties/litigants, not lawyers — exclude from lawyer list
PARTY_ROLE_FRAGMENTS = {
    'party in case', 'party to case', 'litigant', 'petitioner',
    'respondent', 'appellant', 'plaintiff', 'defendant', 'accused',
    'complainant', 'claimant',
}


def _is_party_role(role: str) -> bool:
    """Return True if role indicates a litigant/party rather than a lawyer."""
    if not role:
        return False
    r = role.lower().strip()
    return any(p in r for p in PARTY_ROLE_FRAGMENTS)


# ── Stage 3: Deduplication ────────────────────────────────────────────────────

def _is_duplicate(title: str, source_url: str) -> bool:
    if source_url and LegalCase.query.filter_by(source_url=source_url).first():
        return True
    if LegalCase.query.filter_by(title=title).first():
        return True
    prefix = title[:50].lower()
    for c in LegalCase.query.with_entities(LegalCase.title).all():
        if c.title[:50].lower() == prefix:
            return True
    return False


# ── Stage 5: Multi-source lawyer discovery ────────────────────────────────────

def _normalize_name(name: str) -> str:
    if not name:
        return ''
    n = name.lower().strip()
    for title in sorted(LEGAL_TITLES, key=len, reverse=True):
        if n.startswith(title + ' '):
            n = n[len(title):].strip()
    n = re.sub(r'[.,;:()\[\]\'"]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _names_match(n1: str, n2: str) -> bool:
    a, b = _normalize_name(n1), _normalize_name(n2)
    if not a or not b or len(a) < 3 or len(b) < 3:
        return False
    if a == b:
        return True
    p1, p2 = a.split(), b.split()
    if len(p1) >= 2 and len(p2) >= 2:
        if p1[-1] == p2[-1] and p1[0][0] == p2[0][0]:
            return True
    if len(a) > 6 and len(b) > 6 and (a in b or b in a):
        return True
    return False


def _extract_names_regex(text: str) -> list:
    """Fast regex extraction of lawyer names from legal text."""
    patterns = [
        # "Advocate Name" / "Senior Advocate Name"
        r"(?:Senior\s+)?Advocate\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        # "Adv. Name"
        r"Adv\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        # "represented/appearing/argued by [Senior] [Advocate] Name"
        r"(?:represented|appearing|argued)\s+(?:by\s+)?(?:Senior\s+)?(?:Advocate\s+)?"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        # "Solicitor General / ASG / Attorney General Name"
        r"(?:Solicitor\s+General|Additional\s+Solicitor\s+General|ASG|Attorney\s+General)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        # "counsel for petitioner/respondent: Name"
        r"(?:counsel\s+for\s+(?:the\s+)?(?:petitioner|respondent|appellant|defendant)s?)"
        r"\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        # "For Petitioner/Respondent: [Sr.] Adv. Name" (common in IndianKanoon docs)
        r"For\s+(?:the\s+)?(?:Petitioner|Respondent|Appellant|Defendant|Plaintiff|"
        r"Prosecution|State|Union|Company)s?\s*[:\-]\s*(?:Senior\s+)?(?:Advocate|Adv\.?)?\s*"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        # "Mr./Ms./Smt. Name, [Senior] Advocate"
        r"(?:Mr\.|Ms\.|Smt\.)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*,?\s*"
        r"(?:Senior\s+)?Advocate",
        # "Name, Senior Advocate" (name before title)
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\s*,\s*(?:Senior\s+Advocate|Sr\.\s*Advocate)",
        # "amicus curiae: Name"
        r"(?:amicus\s+curiae|amicus)\s*[:\-]?\s*(?:Senior\s+)?(?:Advocate\s+)?"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        # "learned counsel Name" / "learned senior counsel Name"
        r"learned\s+(?:senior\s+)?counsel\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
    ]
    seen: set = set()
    names = []
    for pattern in patterns:
        for m in re.findall(pattern, text, re.IGNORECASE):
            name = m.strip()
            norm = _normalize_name(name)
            if len(name) > 3 and norm not in NAME_BLOCKLIST and norm not in seen:
                seen.add(norm)
                names.append(name)
    return names


def _search_bing_lawyers(case_title: str, case_name: str = '') -> list:
    """Bing web search for lawyers in a case, pass snippets to AI."""
    short_title = (case_name or case_title)[:70]
    queries = [
        f'{short_title} advocate OR "senior advocate" India',
        f'"{short_title}" lawyer OR counsel court India',
    ]

    all_snippets: list[str] = []
    for query in queries:
        encoded = quote_plus(query)
        url = f"https://www.bing.com/search?q={encoded}&count=5&mkt=en-IN"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            snippets = [r.get_text(' ', strip=True) for r in soup.select('.b_algo')]
            all_snippets.extend(snippets)
            if len(all_snippets) >= 5:
                break
        except Exception as e:
            current_app.logger.error(f"[BING_LAWYER] {query[:50]}: {e}")

    snippet_text = ' '.join(all_snippets)[:3000]
    if not snippet_text.strip():
        return []
    return identify_lawyers_from_search(case_title, snippet_text)


def _search_indiankanoon(case_title: str) -> list:
    """Search IndianKanoon and scrape the top case document for advocate names."""
    encoded = quote_plus(case_title)
    search_url = f"https://indiankanoon.org/search/?formInput={encoded}&pagenum=0"
    lawyers: list = []
    seen_names: set = set()

    def _add_names(text_block: str):
        for name in _extract_names_regex(text_block):
            norm = _normalize_name(name)
            if norm not in seen_names:
                seen_names.add(norm)
                lawyers.append({'name': name, 'source': 'IndianKanoon'})

    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=12)
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Follow the first case document link
        doc_link = None
        for a in soup.select('.result_title a, a[href]'):
            href = a.get('href', '')
            if re.match(r'^/doc/\d+', href):
                doc_link = 'https://indiankanoon.org' + href
                break

        if doc_link:
            try:
                doc_resp = requests.get(doc_link, headers=HEADERS, timeout=12)
                doc_soup = BeautifulSoup(doc_resp.text, 'html.parser')
                doc_text = doc_soup.get_text(' ', strip=True)
                _add_names(doc_text[:6000])
                current_app.logger.info(
                    f"[IK DOC] {doc_link[-30:]}: {len(lawyers)} names so far"
                )
            except Exception as e:
                current_app.logger.error(f"[IK DOC] {e}")

        snippets = soup.select('.result, .result_title, .snippet')
        snippet_text = ' '.join(s.get_text(' ', strip=True) for s in snippets[:8])
        _add_names(snippet_text)

        current_app.logger.info(f"[IK] '{case_title[:40]}': {len(lawyers)} names total")
    except Exception as e:
        current_app.logger.error(f"[IK] {case_title[:40]}: {e}")
    return lawyers


def _cross_verify(ai_lawyers: list, bing_lawyers: list, ik_lawyers: list,
                   article_lawyers: list | None = None) -> list:
    """
    Merge lawyer lists from up to 4 sources, assign confidence scores.

    Sources: ai_analysis | bing_search | indiankanoon | article_regex
    Confidence: 3+ distinct sources → 0.9 | 2 sources → 0.7 | 1 source → 0.4
    """
    master: list = []

    def _add(name, firm, role, src_type, src_detail):
        if not name:
            return
        norm = _normalize_name(name)
        if norm in NAME_BLOCKLIST or name.lower() in ('unknown', 'n/a', 'not mentioned', 'unnamed'):
            return
        if _is_party_role(role):
            current_app.logger.info(f"[VERIFY] Skipping party: {name} [{role}]")
            return
        for existing in master:
            if _names_match(existing['name'], name):
                existing['sources'].append({'type': src_type, 'detail': src_detail})
                if not existing['firm'] and firm:
                    existing['firm'] = firm
                if not existing['role'] and role:
                    existing['role'] = role
                return
        master.append({
            'name': name, 'firm': firm or '', 'role': role or '',
            'sources': [{'type': src_type, 'detail': src_detail}],
        })

    for l in ai_lawyers:
        _add(l.get('name', ''), l.get('firm', ''), l.get('role', ''),
             'ai_analysis', 'AI extraction from article')
    for l in bing_lawyers:
        _add(l.get('name', ''), l.get('firm', ''), l.get('role', ''),
             'bing_search', 'Bing + AI extraction')
    for l in ik_lawyers:
        _add(l.get('name', ''), '', '', 'indiankanoon', 'IndianKanoon.org')
    for l in (article_lawyers or []):
        _add(l.get('name', ''), l.get('firm', ''), l.get('role', ''),
             'article_regex', 'Regex from article text')

    for entry in master:
        n = len(set(s['type'] for s in entry['sources']))
        entry['confidence'] = 0.9 if n >= 3 else (0.7 if n == 2 else 0.4)
        entry['verified'] = n >= 2

    master.sort(key=lambda x: x['confidence'], reverse=True)
    current_app.logger.info(
        f"[VERIFY] {len(master)} lawyers, "
        f"{sum(1 for l in master if l['verified'])} verified"
    )
    return master


def _multi_source_lawyers(case_title: str, ai_lawyers: list, case_name: str = '') -> list:
    """Orchestrate 3-source lawyer discovery and cross-verification."""
    bing_lawyers = _search_bing_lawyers(case_title, case_name=case_name)
    ik_lawyers = _search_indiankanoon(case_title)
    current_app.logger.info(
        f"[LAWYERS] ai={len(ai_lawyers)} bing={len(bing_lawyers)} ik={len(ik_lawyers)}"
    )
    return _cross_verify(ai_lawyers, bing_lawyers, ik_lawyers)


# ── Stage 6: Contact discovery ────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
_LINKEDIN_RE = re.compile(r'https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9_%\-./]+')
_EMAIL_BLOCKED = {'example.com', 'bing.com', 'microsoft.com', 'google.com',
                  'sampleemail', 'test.com', 'email.com', 'wikidata.org',
                  'wikipedia.org', 'schema.org', 'w3.org'}


def _is_valid_email(email: str) -> bool:
    if not email or len(email) < 5:
        return False
    if any(b in email.lower() for b in _EMAIL_BLOCKED):
        return False
    return bool(_EMAIL_RE.fullmatch(email))


_GENERIC_DOMAINS = {
    'wikipedia', 'linkedin', 'google', 'bing', 'youtube', 'facebook',
    'twitter', 'x.com', 'instagram', 'justdial', 'indiakanoon',
    'barandbench', 'livelaw', 'lawrato', 'advocatekhoj',
}
_FIRM_PAGE_PATHS = ['/team', '/people', '/attorneys', '/lawyers', '/our-team',
                    '/our-lawyers', '/professionals', '/partners', '/contact']


def _extract_domain_from_firm(firm: str) -> str | None:
    """Find the law firm's website domain via Bing."""
    if not firm or firm.lower() in ('unknown firm', 'n/a', ''):
        return None
    try:
        query = quote_plus(f"{firm} India law firm official website")
        resp = requests.get(f"https://www.bing.com/search?q={query}&count=3",
                            headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for a in soup.select('.b_algo h2 a'):
            href = a.get('href', '')
            if href.startswith('http'):
                m = re.match(r'https?://(?:www\.)?([^/]+)', href)
                if m:
                    domain = m.group(1).lower()
                    if not any(g in domain for g in _GENERIC_DOMAINS):
                        return domain
    except Exception:
        pass
    return None


def _scrape_firm_pages(firm_domain: str, lawyer_name: str) -> str | None:
    """Scrape firm's team/contact pages for a specific lawyer's email."""
    name_parts = [p.lower() for p in _normalize_name(lawyer_name).split() if len(p) > 3]
    base = f"https://{firm_domain}"
    for path in _FIRM_PAGE_PATHS:
        try:
            resp = requests.get(base + path, headers=HEADERS, timeout=6, allow_redirects=True)
            if resp.status_code != 200:
                continue
            text = resp.text
            text_lower = text.lower()
            if not any(p in text_lower for p in name_parts):
                continue
            emails = [e for e in _EMAIL_RE.findall(text) if _is_valid_email(e)]
            if emails:
                current_app.logger.info(
                    f"[CONTACT] {lawyer_name}: email on {firm_domain}{path}"
                )
                return emails[0]
        except Exception:
            continue
    return None


def _search_lawrato(name: str) -> str | None:
    """Search LawRato.com India lawyer directory for an email address."""
    try:
        encoded = quote_plus(name)
        resp = requests.get(
            f"https://lawrato.com/find-lawyers?search={encoded}",
            headers=HEADERS, timeout=10
        )
        soup = BeautifulSoup(resp.text, 'html.parser')
        name_parts = [p.lower() for p in _normalize_name(name).split() if len(p) > 3]

        for a in soup.select('a[href*="/indian-lawyers/"], a[href*="/lawyer/"]'):
            link_text = a.get_text(strip=True).lower()
            if not any(p in link_text for p in name_parts):
                continue
            href = a.get('href', '')
            profile_url = href if href.startswith('http') else 'https://lawrato.com' + href
            try:
                prof_resp = requests.get(profile_url, headers=HEADERS, timeout=8)
                emails = [e for e in _EMAIL_RE.findall(prof_resp.text) if _is_valid_email(e)]
                if emails:
                    current_app.logger.info(f"[LAWRATO] {name}: {emails[0]}")
                    return emails[0]
            except Exception:
                continue
    except Exception:
        pass
    return None


def _search_advocatekhoj(name: str) -> str | None:
    """Search AdvocateKhoj.com India lawyer directory for contact info."""
    try:
        encoded = quote_plus(name)
        resp = requests.get(
            f"https://www.advocatekhoj.com/lawyers/index.php?loc=0&sname={encoded}",
            headers=HEADERS, timeout=10
        )
        soup = BeautifulSoup(resp.text, 'html.parser')
        name_parts = [p.lower() for p in _normalize_name(name).split() if len(p) > 3]

        for a in soup.select('a[href*="lawyerdetails"], a[href*="lawyer-details"]'):
            link_text = a.get_text(strip=True).lower()
            if not any(p in link_text for p in name_parts):
                continue
            href = a.get('href', '')
            profile_url = href if href.startswith('http') else 'https://www.advocatekhoj.com' + href
            try:
                prof_resp = requests.get(profile_url, headers=HEADERS, timeout=8)
                emails = [e for e in _EMAIL_RE.findall(prof_resp.text) if _is_valid_email(e)]
                if emails:
                    current_app.logger.info(f"[AKHOJ] {name}: {emails[0]}")
                    return emails[0]
            except Exception:
                continue
    except Exception:
        pass
    return None


def _hunter_find_email(name: str, domain: str) -> str | None:
    """Use Hunter.io email-finder API (if HUNTER_API_KEY is set)."""
    api_key = current_app.config.get('HUNTER_API_KEY')
    if not api_key or not domain:
        return None
    parts = _normalize_name(name).split()
    if len(parts) < 2:
        return None
    try:
        resp = requests.get(
            'https://api.hunter.io/v2/email-finder',
            params={
                'domain': domain,
                'first_name': parts[0],
                'last_name': parts[-1],
                'api_key': api_key,
            },
            timeout=10
        )
        data = resp.json().get('data', {})
        email = data.get('email')
        score = data.get('score', 0)
        if email and score >= 50:
            current_app.logger.info(f"[HUNTER] {name}: {email} (score={score})")
            return email
    except Exception as e:
        current_app.logger.error(f"[HUNTER] {name}: {e}")
    return None


def _bing_search_email(name: str, firm: str) -> tuple[str | None, str | None]:
    """
    Try multiple Bing query variants to find an email address.
    Returns (email, linkedin_url).
    """
    firm_str = f'"{firm}" ' if firm and firm.lower() not in ('unknown firm', '', 'n/a') else ''
    queries = [
        f'"{name}" {firm_str}advocate email India',
        f'"{name}" {firm_str}lawyer contact India',
        f'"{name}" senior advocate India email OR contact',
        f'{name} advocate India "@" email',
    ]

    found_email = None
    found_linkedin = None

    for query in queries:
        if found_email:
            break
        try:
            resp = requests.get(
                f"https://www.bing.com/search?q={quote_plus(query)}&count=10",
                headers=HEADERS, timeout=10
            )
            html = resp.text
            emails = [e for e in _EMAIL_RE.findall(html) if _is_valid_email(e)]
            linkedins = _LINKEDIN_RE.findall(html)

            if emails:
                found_email = emails[0]
            if linkedins and not found_linkedin:
                found_linkedin = linkedins[0]

            if not found_email:
                soup = BeautifulSoup(html, 'html.parser')
                links = [
                    a.get('href', '') for a in soup.select('.b_algo h2 a')
                    if a.get('href', '').startswith('http') and 'bing.com' not in a.get('href', '')
                ]
                for link in links[:3]:
                    try:
                        pr = requests.get(link, headers=HEADERS, timeout=7, allow_redirects=True)
                        pe = [e for e in _EMAIL_RE.findall(pr.text) if _is_valid_email(e)]
                        pl = _LINKEDIN_RE.findall(pr.text)
                        if pe:
                            found_email = pe[0]
                            break
                        if pl and not found_linkedin:
                            found_linkedin = pl[0]
                    except Exception:
                        continue
        except Exception:
            continue

    return found_email, found_linkedin


def _discover_contacts(name: str, firm: str) -> tuple[str | None, str | None, str | None]:
    """
    Find email and LinkedIn for a lawyer using 5 source tiers.

    Sources (in order):
      1. Bing — 4 query variants + page scraping
      2. LawRato.com — India lawyer directory
      3. AdvocateKhoj.com — India bar directory
      4. Firm website team/contact pages
      5. Hunter.io API (only if HUNTER_API_KEY env var is set)

    Returns (email, linkedin_url, email_source).
    """
    if not name or _normalize_name(name) in NAME_BLOCKLIST:
        return None, None, None

    found_email: str | None = None
    found_linkedin: str | None = None
    found_source: str | None = None

    # ── 1. Bing ───────────────────────────────────────────────────────────────
    found_email, found_linkedin = _bing_search_email(name, firm)
    if found_email:
        found_source = 'bing_search'

    # ── 2. LawRato ────────────────────────────────────────────────────────────
    if not found_email:
        found_email = _search_lawrato(name)
        if found_email:
            found_source = 'lawrato'

    # ── 3. AdvocateKhoj ───────────────────────────────────────────────────────
    if not found_email:
        found_email = _search_advocatekhoj(name)
        if found_email:
            found_source = 'advocatekhoj'

    # ── 4. Firm website ───────────────────────────────────────────────────────
    if not found_email and firm:
        domain = _extract_domain_from_firm(firm)
        if domain:
            found_email = _scrape_firm_pages(domain, name)
            if found_email:
                found_source = 'firm_website'
            if not found_email:
                found_email = _hunter_find_email(name, domain)
                if found_email:
                    found_source = 'hunter_io'

    # ── 5. LinkedIn (always try, independent of email search) ─────────────────
    if not found_linkedin:
        try:
            li_query = quote_plus(f'"{name}" advocate India site:linkedin.com/in/')
            li_resp = requests.get(
                f"https://www.bing.com/search?q={li_query}&count=3",
                headers=HEADERS, timeout=8
            )
            li_matches = _LINKEDIN_RE.findall(li_resp.text)
            if li_matches:
                found_linkedin = li_matches[0]
        except Exception:
            pass

    return found_email, found_linkedin, found_source


# ── Stage 7: Save to DB ───────────────────────────────────────────────────────

def _compute_trending_score(analysis: dict) -> float:
    """
    Use AI's trending_score if present, otherwise derive from analysis fields.
    Stored as 0.0–10.0 (multiply AI 0–1 score by 10).
    """
    ai_score = analysis.get('trending_score')
    if isinstance(ai_score, (int, float)) and 0 <= ai_score <= 1:
        return round(ai_score * 10, 2)

    score = 5.0
    if analysis.get('status') in ('pending', 'reserved'):
        score += 2.0
    if len(analysis.get('lawyers', [])) > 2:
        score += 1.0
    area = analysis.get('practice_area', '').lower()
    if any(a in area for a in ['corporate', 'criminal', 'constitutional', 'ip',
                                'insolvency', 'banking', 'cyber', 'sebi', 'cci']):
        score += 1.5
    court = analysis.get('court', '').lower()
    if 'supreme court' in court:
        score += 1.0
    return min(score, 10.0)


# ── Main scan entry point ─────────────────────────────────────────────────────

def scan_for_cases(progress_cb=None) -> dict:
    """
    Run the grounded discovery pipeline.

    progress_cb: optional callable(str) — receives human-readable log lines
                 in real-time (used to stream logs to the UI).

    Returns a summary dict:
      {
        'new_cases': int,
        'skipped_duplicates': int,
        'skipped_no_lawyers': int,
        'lawyers_found': int,
        'lawyers_with_email': int,
      }
    """

    def log(msg: str):
        current_app.logger.info(msg)
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    summary = {
        'new_cases': 0,
        'skipped_duplicates': 0,
        'skipped_no_lawyers': 0,
        'lawyers_found': 0,
        'lawyers_with_email': 0,
        # kept for UI compatibility
        'skipped_not_case': 0,
    }

    log("━━━ SCAN START ━━━")

    # ── Stage 0: Grounded discovery (Gemma 4 + Google Search) ────────────────
    log("Stage 0 — Gemma 4 searching Google for trending Indian cases...")
    grounded_cases = []
    try:
        grounded_cases = discover_cases_grounded()
        log(f"  Found {len(grounded_cases)} cases via grounded search")
    except Exception as e:
        log(f"  ❌ Grounded search failed: {e}")

    if not grounded_cases:
        log("  ⚠ No cases returned — scan complete")
        log("━━━ DONE: 0 cases ━━━")
        return summary

    seen_in_scan: set = set()

    def _process_case(analysis: dict, source_url: str, source_name: str,
                       published_date=None):
        """Lawyer discovery + contact + save for one validated case."""
        nonlocal summary

        case_name = analysis.get('case_name', '').strip()
        court = analysis.get('court', '')
        practice = analysis.get('practice_area', '')
        ai_lawyers = analysis.get('lawyers', [])
        score = analysis.get('trending_score', 0)

        log(f"  ✓ {case_name[:60]}")
        log(f"    Court: {court or 'unknown'}  |  Area: {practice}  |  Score: {score:.2f}")
        if ai_lawyers:
            log(f"    Prompt 1 found {len(ai_lawyers)} lawyer(s): "
                + ', '.join(l.get('name', '?') for l in ai_lawyers[:5]))
        else:
            log(f"    Prompt 1 found no lawyers — will search separately")

        # Stage 1a: grounded lawyer search (always run — primary recovery for empty Prompt 1)
        log(f"  👤 Grounded lawyer search: {case_name[:50]}...")
        try:
            grounded_lawyers = discover_lawyers_grounded(case_name, court)
            log(f"    Grounded found {len(grounded_lawyers)} lawyer(s)"
                + (': ' + ', '.join(l.get('name', '?') for l in grounded_lawyers[:5])
                   if grounded_lawyers else ''))
        except Exception as e:
            log(f"    ⚠ Grounded lawyer search failed: {e}")
            grounded_lawyers = []

        # Stage 1b: Bing + IndianKanoon enrichment
        combined = ai_lawyers + grounded_lawyers
        verified_lawyers = _multi_source_lawyers(
            case_name or source_url, combined,
            case_name=case_name
        )
        log(f"    → {len(verified_lawyers)} unique lawyers "
            f"({sum(1 for l in verified_lawyers if l['verified'])} verified)")

        if not verified_lawyers:
            log(f"  ✗ No lawyers found across all sources — skipped")
            summary['skipped_no_lawyers'] += 1
            return

        # Stage 2: Contact discovery (grounded first, then traditional)
        lawyer_objects = []
        for vl in verified_lawyers:
            name = vl.get('name', '').strip()
            firm = vl.get('firm', '').strip()
            if not name:
                continue

            log(f"    🔎 Contact: {name} (conf={vl['confidence']:.1f})")

            email, linkedin = None, None
            try:
                email, linkedin = discover_contact_grounded(name, firm)
                if email:
                    log(f"      ✉ Email via grounded: {email}")
            except Exception:
                pass

            if not email:
                email, linkedin_t, email_src = _discover_contacts(name, firm)
                if not linkedin:
                    linkedin = linkedin_t
                if email:
                    log(f"      ✉ Email via scraping: {email}")
            else:
                email_src = 'grounded_search'

            if linkedin:
                log(f"      🔗 LinkedIn found")

            lawyer_objects.append(Lawyer(
                name=name, firm=firm, role=vl.get('role', ''),
                email=email, email_source=email_src if email else None,
                linkedin_url=linkedin,
                verified=vl.get('verified', False),
                confidence_score=vl.get('confidence', 0.4),
                verification_sources=json.dumps(vl.get('sources', [])),
            ))
            summary['lawyers_found'] += 1
            if email:
                summary['lawyers_with_email'] += 1

        # Stage 3: Save
        saved_title = case_name or source_url[:80]
        case = LegalCase(
            title=saved_title,
            summary=analysis.get('summary', ''),
            source_url=source_url,
            source_name=source_name,
            published_date=published_date,
            status='active',
            ai_analysis=json.dumps(analysis),
            trending_score=_compute_trending_score(analysis),
        )
        for lo in lawyer_objects:
            case.lawyers.append(lo)
        db.session.add(case)
        db.session.commit()
        summary['new_cases'] += 1
        log(f"  ✅ Saved: {saved_title[:60]} — {len(lawyer_objects)} lawyers")

    # ── Process grounded cases ────────────────────────────────────────────────
    for gc in grounded_cases:
        if summary['new_cases'] >= MAX_NEW_CASES:
            break

        case_name = gc.get('case_name', '').strip()
        title_key = case_name[:60].lower()
        source_url = gc.get('source_url', '')

        if not case_name or title_key in seen_in_scan or _is_duplicate(case_name, source_url):
            summary['skipped_duplicates'] += 1
            log(f"  [dup] {case_name[:70]}")
            continue
        seen_in_scan.add(title_key)

        gc['is_case'] = True
        try:
            _process_case(
                analysis=gc,
                source_url=source_url,
                source_name=gc.get('source_name', 'Google Search'),
                published_date=datetime.now(timezone.utc),
            )
        except Exception as e:
            log(f"  ❌ Error processing {case_name[:50]}: {e}")
            db.session.rollback()

    log(f"━━━ DONE: {summary['new_cases']} cases · "
        f"{summary['lawyers_found']} lawyers · "
        f"{summary['lawyers_with_email']} with email · "
        f"{summary['skipped_duplicates']} duplicates skipped ━━━")
    return summary
