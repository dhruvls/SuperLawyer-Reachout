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
from app.ai.gemma import (discover_cases_grounded, discover_lawyers_grounded,
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


def _cross_verify(ai_lawyers: list, ik_lawyers: list) -> list:
    """
    Merge lawyer lists from Google-grounded AI and IndianKanoon scraping.

    Sources: grounded_ai (Prompt 1 + Prompt 2) | indiankanoon
    Confidence: seen in both → 0.9 (verified) | one source → 0.4
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
             'grounded_ai', 'Gemma 4 + Google Search')
    for l in ik_lawyers:
        _add(l.get('name', ''), '', '', 'indiankanoon', 'IndianKanoon.org')

    for entry in master:
        n = len(set(s['type'] for s in entry['sources']))
        entry['confidence'] = 0.9 if n >= 2 else 0.4
        entry['verified'] = n >= 2

    master.sort(key=lambda x: x['confidence'], reverse=True)
    current_app.logger.info(
        f"[VERIFY] {len(master)} lawyers, "
        f"{sum(1 for l in master if l['verified'])} verified"
    )
    return master


def _multi_source_lawyers(case_title: str, ai_lawyers: list, case_name: str = '') -> list:
    """Cross-verify grounded AI lawyers against IndianKanoon."""
    ik_lawyers = _search_indiankanoon(case_name or case_title)
    current_app.logger.info(
        f"[LAWYERS] grounded_ai={len(ai_lawyers)} indiankanoon={len(ik_lawyers)}"
    )
    return _cross_verify(ai_lawyers, ik_lawyers)


# ── Stage 6: Contact discovery ────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
_EMAIL_BLOCKED = {'example.com', 'bing.com', 'microsoft.com', 'google.com',
                  'sampleemail', 'test.com', 'email.com', 'wikidata.org',
                  'wikipedia.org', 'schema.org', 'w3.org'}


def _is_valid_email(email: str) -> bool:
    if not email or len(email) < 5:
        return False
    if any(b in email.lower() for b in _EMAIL_BLOCKED):
        return False
    return bool(_EMAIL_RE.fullmatch(email))


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


def _discover_contacts(name: str, firm: str) -> tuple[str | None, str | None, str | None]:
    """
    Fallback contact discovery after discover_contact_grounded() found nothing.

    Sources (in order):
      1. LawRato.com — India lawyer directory
      2. AdvocateKhoj.com — India bar directory

    Returns (email, linkedin_url, email_source).
    """
    if not name or _normalize_name(name) in NAME_BLOCKLIST:
        return None, None, None

    found_email: str | None = None
    found_source: str | None = None

    # ── 1. LawRato ────────────────────────────────────────────────────────────
    found_email = _search_lawrato(name)
    if found_email:
        found_source = 'lawrato'

    # ── 2. AdvocateKhoj ───────────────────────────────────────────────────────
    if not found_email:
        found_email = _search_advocatekhoj(name)
        if found_email:
            found_source = 'advocatekhoj'

    return found_email, None, found_source


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

        # Stage 1b: IndianKanoon cross-verification
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
