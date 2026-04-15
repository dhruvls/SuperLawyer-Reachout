"""
Case discovery pipeline — staged, multi-source.

Stages:
  1. _fetch_articles()         — RSS feeds + Bing queries
  2. _fast_keyword_filter()    — cheap title-based gate (no API)
  3. _deduplicate()            — skip if URL/title already in DB
  4. _ai_validate_case()       — AI gate: real case? extract metadata
  5. _multi_source_lawyers()   — 3-source lawyer enrichment + cross-verify
  6. _discover_contacts()      — email + LinkedIn per lawyer
  7. _save_case()              — persist to DB

scan_for_cases() returns a summary dict for the UI.
"""

import re
import json
import feedparser
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from flask import current_app
from app import db
from app.models import LegalCase, Lawyer
from app.ai.gemma import analyze_case, identify_lawyers_from_search


# ── Article sources ──────────────────────────────────────────────────────────

LEGAL_RSS_FEEDS = [
    ('LiveLaw', 'https://www.livelaw.in/feed'),
    ('Bar and Bench', 'https://www.barandbench.com/feed'),
]

SITE_QUERIES = [
    ('LiveLaw', 'site:livelaw.in Supreme Court judgment advocate'),
    ('LiveLaw', 'site:livelaw.in High Court verdict senior advocate'),
    ('LiveLaw', 'site:livelaw.in PIL petition hearing'),
    ('LiveLaw', 'site:livelaw.in NCLT SEBI order advocate'),
    ('Bar and Bench', 'site:barandbench.com Supreme Court judgment advocate'),
    ('Bar and Bench', 'site:barandbench.com High Court verdict petition'),
    ('Bar and Bench', 'site:barandbench.com NCLT insolvency order'),
]

FALLBACK_QUERIES = [
    'India Supreme Court judgment advocate senior counsel 2025',
    'India High Court verdict petition lawyer 2025',
    'NCLT insolvency order advocate India',
    'SEBI order penalty hearing India',
    'India PIL Supreme Court senior advocate',
]

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


# Patterns in article titles that suggest analysis/opinion pieces, not court cases
ARTICLE_TITLE_PATTERNS = [
    r'\bbalancing\b', r'\bexplainer\b', r'\banalysis\b', r'\bperspective\b',
    r'\boverview\b', r'\bguide\b', r'\bimplications of\b', r'\brealities\b',
    r'\blandscape\b', r'\bframework\b', r'\breform\b', r'\bchallenges\b',
    r'\bintersection of\b', r'\brole of\b', r'\bevolution of\b',
]
_ARTICLE_RE = re.compile('|'.join(ARTICLE_TITLE_PATTERNS), re.IGNORECASE)

# ── Fast keyword filter sets ─────────────────────────────────────────────────

# Strong proceeding indicators — any one of these strongly suggests a real case
PROCEEDING_KEYWORDS = {
    'judgment', 'judgement', 'verdict', 'order passed', 'bail granted',
    'bail denied', 'bail rejected', 'conviction', 'acquittal', 'acquitted',
    'convicted', 'writ', 'quashed', 'quash', 'dismissed', 'upheld',
    'sentence', 'sentencing', 'chargesheet', 'plea bargain',
    'injunction', 'stay granted', 'stay refused', 'suo motu', 'suo-motu',
    'judgment pronounced', 'order reserved', 'hearing concluded',
}

# Court / tribunal identifiers
COURT_KEYWORDS = {
    'supreme court', 'high court', 'nclt', 'nclat', 'district court',
    'sessions court', 'tribunal', 'sebi', 'cci', 'ngt', 'consumer court',
    'itat', 'bench', 'division bench', 'single bench', 'sat', 'drt',
    'national company law', 'appellate tribunal',
}

# Weak proceeding words — only count if a court keyword is also present
WEAK_PROCEEDING = {
    'petition', 'hearing', 'appeal', 'order', 'case', 'matter',
    'bail', 'pil', 'versus', 'v.', 'vs.', 'advocate', 'counsel',
}


# ── Stage 1: Article fetching ────────────────────────────────────────────────

def _fetch_rss(feed_url: str, source_name: str, days: int = 7) -> list:
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.text)
        current_app.logger.info(f"[RSS:{source_name}] {len(feed.entries)} entries")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        articles = []
        for entry in feed.entries[:20]:
            # Require a published date — skip undated entries (often stale/old)
            if not (hasattr(entry, 'published_parsed') and entry.published_parsed):
                continue
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if published < cutoff:
                continue
            title = entry.get('title', '').strip()
            link = entry.get('link', '').strip()
            if title and link:
                articles.append({'title': title, 'url': link,
                                 'source': source_name, 'published': published})
        return articles
    except Exception as e:
        current_app.logger.error(f"[RSS:{source_name}] {e}")
        return []


def _fetch_bing(query: str, source_name: str | None = None, days: int = 7) -> list:
    encoded = quote_plus(query)
    url = f"https://www.bing.com/news/search?q={encoded}&format=rss&count=10&mkt=en-IN"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        feed = feedparser.parse(resp.text)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        articles = []
        for entry in feed.entries[:10]:
            # Require a published date — skip undated entries
            if not (hasattr(entry, 'published_parsed') and entry.published_parsed):
                continue
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            if published < cutoff:
                continue
            entry_url = entry.get('link', '')
            if source_name:
                src = source_name
            elif 'livelaw.in' in entry_url:
                src = 'LiveLaw'
            elif 'barandbench.com' in entry_url:
                src = 'Bar and Bench'
            else:
                src = entry.get('source', {}).get('title', 'News')
            title = entry.get('title', '').strip()
            if title and entry_url:
                articles.append({'title': title, 'url': entry_url,
                                 'source': src, 'published': published})
        return articles
    except Exception as e:
        current_app.logger.error(f"[BING] {query[:40]}: {e}")
        return []


def _fetch_articles() -> list:
    """Collect candidate articles from all 3 tiers, deduplicating by URL."""
    seen_urls: set = set()
    all_articles: list = []

    def _add(articles):
        for a in articles:
            u = a['url'].strip()
            if u and u not in seen_urls:
                seen_urls.add(u)
                all_articles.append(a)

    # Tier 1: Direct RSS
    for name, feed_url in LEGAL_RSS_FEEDS:
        _add(_fetch_rss(feed_url, name))
    current_app.logger.info(f"[FETCH] Tier 1: {len(all_articles)} articles")

    # Tier 2: Site-specific Bing (only if RSS yielded few results)
    if len(all_articles) < MAX_NEW_CASES * 2:
        for src, query in SITE_QUERIES:
            _add(_fetch_bing(query, source_name=src))
        current_app.logger.info(f"[FETCH] Tier 2: {len(all_articles)} articles")

    # Tier 3: Generic fallback
    if len(all_articles) < MAX_NEW_CASES:
        for query in FALLBACK_QUERIES:
            _add(_fetch_bing(query))
        current_app.logger.info(f"[FETCH] Tier 3: {len(all_articles)} articles")

    return all_articles


# ── Stage 2: Fast keyword filter ─────────────────────────────────────────────

def _fast_keyword_filter(articles: list) -> list:
    """
    Remove obvious non-cases without any API call.
    Rules (must pass ALL):
      (a) strong proceeding keyword OR (court keyword + weak proceeding keyword)
      (b) title does NOT look like an analysis/opinion article
    """
    kept = []
    for a in articles:
        t = a['title'].lower()
        has_proceeding = any(kw in t for kw in PROCEEDING_KEYWORDS)
        has_court = any(kw in t for kw in COURT_KEYWORDS)
        has_weak = any(kw in t for kw in WEAK_PROCEEDING)

        # Rule (a): must have case-like keywords
        if not has_proceeding and not (has_court and has_weak):
            continue

        # Rule (b): reject titles that look like explainer / analysis articles
        # A title with a colon AND an analysis-word is almost always an article
        has_colon = ':' in a['title']
        has_article_word = bool(_ARTICLE_RE.search(a['title']))
        if has_colon and has_article_word:
            current_app.logger.info(f"[FILTER] Article-style title rejected: {a['title'][:80]}")
            continue

        kept.append(a)

    current_app.logger.info(
        f"[FILTER] {len(kept)}/{len(articles)} passed keyword filter"
    )
    return kept


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


# ── Stage 4: AI case validation ───────────────────────────────────────────────

def _fetch_article_text(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        text = ' '.join(p.get_text(strip=True) for p in soup.find_all('p'))
        return text[:5000]
    except Exception as e:
        current_app.logger.error(f"[SCRAPE] {url[:60]}: {e}")
        return ''


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


def _extract_from_article(article_text: str) -> list:
    """Extract lawyer names directly from article text via regex (4th source)."""
    return [
        {'name': n, 'firm': '', 'role': '', 'source': 'article_regex'}
        for n in _extract_names_regex(article_text)
    ]


def _search_bing_lawyers(case_title: str, case_name: str = '') -> list:
    """Bing web search for lawyers in a case, pass snippets to AI.

    Tries two queries:
      1. Shortened case title with advocate keywords
      2. Party v. Party format (if extracted by AI) — often more findable
    """
    # Use party names (e.g. "State v. Sharma") if available, else shortened title
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

        # Follow the first case document link — these pages have structured
        # "For Petitioner: Adv. X" sections that regex catches well
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
                # The preamble section (first ~3000 chars) holds advocate listings
                doc_text = doc_soup.get_text(' ', strip=True)
                _add_names(doc_text[:6000])
                current_app.logger.info(
                    f"[IK DOC] {doc_link[-30:]}: {len(lawyers)} names so far"
                )
            except Exception as e:
                current_app.logger.error(f"[IK DOC] {e}")

        # Also mine the search snippet text as a fallback
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


def _multi_source_lawyers(case_title: str, ai_lawyers: list,
                           article_text: str = '', case_name: str = '') -> list:
    """Orchestrate 4-source lawyer discovery and cross-verification."""
    article_lawyers = _extract_from_article(article_text) if article_text else []
    bing_lawyers = _search_bing_lawyers(case_title, case_name=case_name)
    ik_lawyers = _search_indiankanoon(case_title)
    current_app.logger.info(
        f"[LAWYERS] ai={len(ai_lawyers)} article_regex={len(article_lawyers)} "
        f"bing={len(bing_lawyers)} ik={len(ik_lawyers)}"
    )
    return _cross_verify(ai_lawyers, bing_lawyers, ik_lawyers, article_lawyers)


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


def _infer_email_patterns(first: str, last: str, domain: str) -> list:
    """Generate candidate email addresses from name + domain."""
    f, l = first.lower(), last.lower()
    return [
        f"{f}@{domain}",
        f"{f}.{l}@{domain}",
        f"{f[0]}{l}@{domain}",
        f"{f}{l[0]}@{domain}",
        f"{f}_{l}@{domain}",
    ]


def _extract_domain_from_firm(firm: str) -> str | None:
    """Try to extract a likely domain from a firm name for email pattern inference."""
    if not firm or firm.lower() in ('unknown firm', 'n/a', ''):
        return None
    # Search Bing for the firm website
    try:
        query = quote_plus(f"{firm} India law firm official website")
        url = f"https://www.bing.com/search?q={query}&count=3"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for a in soup.select('.b_algo h2 a'):
            href = a.get('href', '')
            if href.startswith('http') and 'bing.com' not in href and 'microsoft.com' not in href:
                # Extract domain from URL
                m = re.match(r'https?://(?:www\.)?([^/]+)', href)
                if m:
                    domain = m.group(1).lower()
                    # Skip generic sites
                    if not any(g in domain for g in ['wikipedia', 'linkedin', 'google', 'bing', 'youtube']):
                        return domain
    except Exception:
        pass
    return None


def _discover_contacts(name: str, firm: str) -> tuple[str | None, str | None, str | None]:
    """
    Find email and LinkedIn for a lawyer.
    Returns (email, linkedin_url, email_source).
    email_source: 'bing_search', 'firm_website', 'pattern_inferred'
    """
    if not name or _normalize_name(name) in NAME_BLOCKLIST:
        return None, None, None

    # Step 1: Bing search for email + LinkedIn
    firm_str = f' "{firm}"' if firm and firm.lower() not in ('unknown firm', '', 'n/a') else ''
    query = quote_plus(f'"{name}"{firm_str} advocate email India')
    url = f"https://www.bing.com/search?q={query}&count=10"
    found_email = None
    found_linkedin = None
    found_source = None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        html = resp.text
        emails = [e for e in _EMAIL_RE.findall(html) if _is_valid_email(e)]
        linkedins = _LINKEDIN_RE.findall(html)

        if emails:
            found_email = emails[0]
            found_source = 'bing_search'
        if linkedins:
            found_linkedin = linkedins[0]

        if not found_email:
            # Scrape top 3 result pages
            soup = BeautifulSoup(html, 'html.parser')
            page_links = [
                a.get('href', '') for a in soup.select('.b_algo h2 a')
                if 'bing.com' not in a.get('href', '') and a.get('href', '').startswith('http')
            ]
            for link in page_links[:3]:
                try:
                    page_resp = requests.get(link, headers=HEADERS, timeout=8, allow_redirects=True)
                    page_emails = [e for e in _EMAIL_RE.findall(page_resp.text) if _is_valid_email(e)]
                    page_li = _LINKEDIN_RE.findall(page_resp.text)
                    if page_emails:
                        found_email = page_emails[0]
                        found_source = 'firm_website'
                        current_app.logger.info(f"[CONTACT] {name}: email via {link[:50]}")
                        break
                    if page_li and not found_linkedin:
                        found_linkedin = page_li[0]
                except Exception:
                    continue
    except Exception as e:
        current_app.logger.error(f"[CONTACT] Bing search for {name}: {e}")

    # Step 2: LinkedIn-specific search if not found yet
    if not found_linkedin:
        try:
            li_query = quote_plus(f'"{name}" advocate India site:linkedin.com/in/')
            li_url = f"https://www.bing.com/search?q={li_query}&count=3"
            li_resp = requests.get(li_url, headers=HEADERS, timeout=8)
            li_matches = _LINKEDIN_RE.findall(li_resp.text)
            if li_matches:
                found_linkedin = li_matches[0]
        except Exception:
            pass

    # Step 3: Email pattern inference from firm domain (if still no email)
    if not found_email and firm:
        domain = _extract_domain_from_firm(firm)
        if domain:
            parts = _normalize_name(name).split()
            if len(parts) >= 2:
                candidates = _infer_email_patterns(parts[0], parts[-1], domain)
                # Use the most likely pattern; we don't verify live (no SMTP check)
                found_email = candidates[0]  # first.last@domain is most common in India
                found_source = 'pattern_inferred'
                current_app.logger.info(
                    f"[CONTACT] {name}: inferred email {found_email} via {domain}"
                )

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

    # Fallback derivation
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
    Run the full discovery pipeline.

    progress_cb: optional callable(str) — receives human-readable log lines
                 in real-time (used to stream logs to the UI).

    Returns a summary dict:
      {
        'articles_found': int,
        'passed_filter': int,
        'valid_cases': int,
        'skipped_duplicates': int,
        'skipped_not_case': int,
        'skipped_no_lawyers': int,
        'new_cases': int,
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
        'articles_found': 0,
        'passed_filter': 0,
        'valid_cases': 0,
        'skipped_duplicates': 0,
        'skipped_not_case': 0,
        'skipped_no_lawyers': 0,
        'new_cases': 0,
        'lawyers_found': 0,
        'lawyers_with_email': 0,
    }

    log("━━━ SCAN START ━━━")

    # Stage 1: Fetch
    log("Stage 1 — Fetching articles from RSS + Bing...")
    articles = _fetch_articles()
    summary['articles_found'] = len(articles)
    log(f"  Found {len(articles)} articles total")

    # Stage 2: Fast filter
    log("Stage 2 — Keyword filter (no API)...")
    articles = _fast_keyword_filter(articles)
    summary['passed_filter'] = len(articles)
    log(f"  {len(articles)} articles passed the case-keyword filter")

    if not articles:
        log("  ⚠ No articles passed filter — check RSS feeds or broaden keywords")

    seen_in_scan: set = set()   # within-batch dedup (same title from multiple sources)

    for article in articles:
        if summary['new_cases'] >= MAX_NEW_CASES:
            log(f"  Reached {MAX_NEW_CASES} cases limit — stopping.")
            break

        title = article['title'].strip()
        source_url = article['url'].strip()

        # Stage 3: Deduplicate (DB + within-batch)
        title_key = title[:60].lower()
        if not title or title_key in seen_in_scan or _is_duplicate(title, source_url):
            summary['skipped_duplicates'] += 1
            log(f"  [dup] {title[:70]}")
            continue
        seen_in_scan.add(title_key)

        try:
            # Stage 4a: Scrape article text
            article_text = _fetch_article_text(source_url)
            if len(article_text) < 80:
                log(f"  [skip — too short] {title[:70]}")
                summary['skipped_not_case'] += 1
                continue

            # Stage 4b: AI validation
            log(f"🔍 AI analyzing: {title[:65]}...")
            analysis = analyze_case(title, article_text)

            if analysis is None:
                log(f"  ✗ AI returned no result — check GOOGLE_AI_API_KEY and model name")
                summary['skipped_not_case'] += 1
                continue

            is_case = analysis.get('is_case', False)
            case_name = analysis.get('case_name', '').strip()
            court = analysis.get('court', '')
            practice = analysis.get('practice_area', '')
            ai_lawyers = analysis.get('lawyers', [])
            score = analysis.get('trending_score', 0)

            if not is_case:
                log(f"  ✗ Not a case — skipped")
                summary['skipped_not_case'] += 1
                continue

            log(f"  ✓ Valid case: {case_name or title[:55]}")
            log(f"    Court: {court or 'unknown'}  |  Area: {practice}  |  Score: {score:.2f}")
            log(f"    AI found {len(ai_lawyers)} lawyer(s): "
                + ', '.join(l.get('name','?') for l in ai_lawyers[:5]))

            if not ai_lawyers:
                log(f"  ✗ No lawyers found by AI — skipped")
                summary['skipped_no_lawyers'] += 1
                continue

            summary['valid_cases'] += 1

            # Stage 5: Multi-source lawyer enrichment
            log(f"  👤 Multi-source lawyer discovery...")
            verified_lawyers = _multi_source_lawyers(
                title, ai_lawyers, article_text=article_text, case_name=case_name
            )
            log(f"    → {len(verified_lawyers)} unique lawyers "
                f"({sum(1 for l in verified_lawyers if l['verified'])} verified)")

            if not verified_lawyers:
                log(f"  ✗ All lawyers filtered out — skipped")
                summary['skipped_no_lawyers'] += 1
                continue

            # Stage 6: Contact discovery
            lawyer_objects = []
            for vl in verified_lawyers:
                name = vl.get('name', '').strip()
                firm = vl.get('firm', '').strip()
                if not name:
                    continue

                log(f"    🔎 Contact search: {name} (conf={vl['confidence']:.1f})")
                email, linkedin, email_src = _discover_contacts(name, firm)
                if email:
                    log(f"      ✉ Email: {email} [{email_src}]")
                if linkedin:
                    log(f"      🔗 LinkedIn found")

                lawyer_objects.append(Lawyer(
                    name=name,
                    firm=firm,
                    role=vl.get('role', ''),
                    email=email,
                    email_source=email_src,
                    linkedin_url=linkedin,
                    verified=vl.get('verified', False),
                    confidence_score=vl.get('confidence', 0.4),
                    verification_sources=json.dumps(vl.get('sources', [])),
                ))
                summary['lawyers_found'] += 1
                if email:
                    summary['lawyers_with_email'] += 1

            # Stage 7: Save to DB
            saved_title = case_name or title
            case = LegalCase(
                title=saved_title,
                summary=analysis.get('summary', article_text[:300]),
                source_url=source_url,
                source_name=article['source'],
                published_date=article.get('published'),
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

        except Exception as e:
            current_app.logger.error(f"[ERROR] '{title[:50]}': {e}")
            log(f"  ❌ Error: {e}")
            db.session.rollback()
            continue

    log(f"━━━ DONE: {summary['new_cases']} cases · "
        f"{summary['lawyers_found']} lawyers · "
        f"{summary['lawyers_with_email']} with email · "
        f"{summary['skipped_not_case']} filtered ━━━")
    return summary


# ── Legacy aliases (for imports elsewhere) ────────────────────────────────────

def fetch_news(query, days=15, source_name=None):
    return _fetch_bing(query, source_name=source_name, days=days)


def fetch_rss_direct(feed_url, source_name, days=15):
    return _fetch_rss(feed_url, source_name, days=days)


def fetch_article_text(url):
    return _fetch_article_text(url)


def find_lawyer_email(name, firm):
    email, linkedin, source = _discover_contacts(name, firm)
    return email, linkedin, source
