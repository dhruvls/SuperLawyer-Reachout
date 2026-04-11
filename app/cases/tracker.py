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


# ── Primary: Direct RSS feeds from top Indian legal news sites ──
LEGAL_RSS_FEEDS = [
    ('LiveLaw', 'https://www.livelaw.in/feed'),
    ('Bar and Bench', 'https://www.barandbench.com/feed'),
]

# ── Secondary: Site-specific Bing queries for LiveLaw & Bar and Bench ──
SITE_QUERIES = [
    'site:livelaw.in Supreme Court case lawyer',
    'site:livelaw.in High Court ruling advocate',
    'site:livelaw.in NCLT SEBI case',
    'site:barandbench.com Supreme Court case lawyer',
    'site:barandbench.com High Court ruling advocate',
    'site:barandbench.com corporate NCLT case',
]

# ── Tertiary: Generic Bing queries (fallback if above yield too few) ──
FALLBACK_QUERIES = [
    'India Supreme Court landmark case',
    'India High Court ruling lawyer',
    'Indian corporate lawsuit NCLT',
    'SEBI enforcement action India',
    'India criminal trial high profile',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36'
}

MAX_NEW_CASES = 15
MAX_PER_QUERY = 3

# Indian legal title prefixes for name normalization
LEGAL_TITLES = [
    'senior advocate', 'sr. advocate', 'sr advocate',
    'advocate', 'adv.', 'adv',
    'justice', "hon'ble justice", "hon'ble",
    'solicitor general', 'attorney general',
    'additional solicitor general', 'asg',
    'senior counsel', 'counsel',
    'mr.', 'mr', 'ms.', 'ms', 'smt.', 'smt', 'dr.', 'dr',
    'shri',
]

NAME_BLOCKLIST = {
    'the court', 'the bench', 'the judge', 'supreme court', 'high court',
    'district court', 'sessions court', 'india', 'government', 'state',
    'union of india', 'central government', 'state government',
}


def fetch_news(query, days=15):
    """Fetch legal news from Bing News RSS."""
    encoded = quote_plus(query)
    url = f"https://www.bing.com/news/search?q={encoded}&format=rss&count=10&mkt=en-IN"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        current_app.logger.warning(f"[NEWS] {query}: status={resp.status_code}, len={len(resp.text)}")
        feed = feedparser.parse(resp.text)
        current_app.logger.warning(f"[NEWS] {query}: {len(feed.entries)} entries")

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        articles = []
        for entry in feed.entries[:10]:
            published = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if published < cutoff:
                    continue
            articles.append({
                'title': entry.get('title', ''),
                'url': entry.get('link', ''),
                'source': entry.get('source', {}).get('title', 'News'),
                'published': published,
            })
        return articles
    except Exception as e:
        current_app.logger.error(f"[NEWS] fetch error: {e}")
        return []


def fetch_rss_direct(feed_url, source_name, days=15):
    """Fetch articles directly from a legal news site's RSS feed."""
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=15)
        current_app.logger.warning(
            f"[RSS] {source_name}: status={resp.status_code}, len={len(resp.text)}"
        )
        feed = feedparser.parse(resp.text)
        current_app.logger.warning(f"[RSS] {source_name}: {len(feed.entries)} entries")

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        articles = []
        for entry in feed.entries[:15]:
            published = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if published < cutoff:
                    continue

            title = entry.get('title', '').strip()
            link = entry.get('link', '').strip()
            if not title or not link:
                continue

            # Filter: only keep legal/court articles
            title_lower = title.lower()
            legal_keywords = [
                'court', 'judge', 'justice', 'advocate', 'lawyer', 'bench',
                'petition', 'verdict', 'ruling', 'bail', 'case', 'tribunal',
                'nclt', 'sebi', 'cci', 'appeal', 'order', 'hearing',
                'accused', 'conviction', 'acquittal', 'writ', 'plea',
                'litigation', 'arbitration', 'dispute', 'act', 'section',
            ]
            if not any(kw in title_lower for kw in legal_keywords):
                continue

            articles.append({
                'title': title,
                'url': link,
                'source': source_name,
                'published': published,
            })
        current_app.logger.warning(
            f"[RSS] {source_name}: {len(articles)} legal articles after filter"
        )
        return articles
    except Exception as e:
        current_app.logger.error(f"[RSS] {source_name} error: {e}")
        return []


def fetch_article_text(url):
    """Scrape article text from a URL."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()
        paragraphs = soup.find_all('p')
        text = ' '.join(p.get_text(strip=True) for p in paragraphs)
        return text[:5000]
    except Exception as e:
        current_app.logger.error(f"[SCRAPE] error for {url[:60]}: {e}")
        return ''


def search_case_lawyers(case_title):
    """Search Bing for lawyers involved in a case, return snippet text."""
    query = f"{case_title} lawyer advocate senior counsel India"
    encoded = quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&count=5&mkt=en-IN"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        snippets = [r.get_text(strip=True) for r in soup.select('.b_algo')]
        text = ' '.join(snippets)[:3000]
        current_app.logger.warning(f"[LAWYER SEARCH] '{case_title[:40]}': {len(snippets)} results")
        return text
    except Exception as e:
        current_app.logger.error(f"[LAWYER SEARCH] error: {e}")
        return ''


def find_lawyer_email(name, firm):
    """Search Bing for a lawyer's email and LinkedIn."""
    if not name or name.lower() in ('unknown', 'n/a', 'not mentioned'):
        return None, None, None

    search_query = f'"{name}" lawyer email'
    if firm and firm.lower() not in ('unknown', 'n/a', ''):
        search_query += f' "{firm}"'
    search_query += ' India'

    encoded = quote_plus(search_query)
    url = f"https://www.bing.com/search?q={encoded}&count=10"

    blocked = ['example.com', 'bing.com', 'microsoft.com', 'google.com',
               'sampleemail', 'test.com', 'email.com', 'wikidata', 'wikipedia']
    email_re = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    linkedin_re = r'https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9_%/-]+'

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        html = resp.text

        # Check search results page
        emails = [e for e in re.findall(email_re, html) if not any(b in e.lower() for b in blocked)]
        linkedins = re.findall(linkedin_re, html)

        if emails:
            current_app.logger.warning(f"[EMAIL] Found for {name}: {emails[0]}")
            return emails[0], linkedins[0] if linkedins else None, 'Bing search'

        # Scrape top 3 result pages
        soup = BeautifulSoup(html, 'html.parser')
        links = [a.get('href', '') for a in soup.select('.b_algo h2 a')
                 if 'bing.com' not in a.get('href', '') and 'microsoft.com' not in a.get('href', '')]

        for link in links[:3]:
            try:
                page = requests.get(link, headers=HEADERS, timeout=8, allow_redirects=True)
                page_emails = [e for e in re.findall(email_re, page.text) if not any(b in e.lower() for b in blocked)]
                page_li = re.findall(linkedin_re, page.text)
                if page_emails:
                    current_app.logger.warning(f"[EMAIL] Found for {name} on {link[:50]}: {page_emails[0]}")
                    return page_emails[0], page_li[0] if page_li else (linkedins[0] if linkedins else None), link
            except Exception:
                continue

        return None, linkedins[0] if linkedins else None, None
    except Exception as e:
        current_app.logger.error(f"[EMAIL] search error for {name}: {e}")
        return None, None, None


# ── Multi-source verification helpers ────────────────────────────

def _normalize_name(name):
    """Normalize a lawyer name for fuzzy comparison."""
    if not name:
        return ''
    n = name.lower().strip()
    for title in sorted(LEGAL_TITLES, key=len, reverse=True):
        if n.startswith(title + ' '):
            n = n[len(title):].strip()
    n = re.sub(r'[.,;:()\[\]\'"]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _names_match(name1, name2):
    """Check if two lawyer names likely refer to the same person."""
    n1 = _normalize_name(name1)
    n2 = _normalize_name(name2)
    if not n1 or not n2 or len(n1) < 3 or len(n2) < 3:
        return False
    if n1 == n2:
        return True
    parts1 = n1.split()
    parts2 = n2.split()
    if len(parts1) >= 2 and len(parts2) >= 2:
        if parts1[-1] == parts2[-1] and parts1[0][0] == parts2[0][0]:
            return True
    if len(n1) > 6 and len(n2) > 6:
        if n1 in n2 or n2 in n1:
            return True
    return False


def _extract_names_from_text(text):
    """Extract lawyer names from legal text using regex patterns."""
    names = []
    patterns = [
        r"(?:Senior\s+)?Advocate\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        r"Adv\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        r"(?:represented|appearing|argued|counsel)\s+(?:by\s+)?(?:Senior\s+)?"
        r"(?:Advocate\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        r"(?:Solicitor\s+General|ASG|Attorney\s+General)\s+"
        r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
        r"(?:counsel\s+for\s+(?:the\s+)?(?:petitioner|respondent|appellant|"
        r"defendant)s?)\s*[:\-]?\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
    ]
    for pattern in patterns:
        for m in re.findall(pattern, text):
            name = m.strip()
            if len(name) > 3 and _normalize_name(name) not in NAME_BLOCKLIST:
                names.append(name)
    return names


def search_indiankanoon(case_title):
    """Search IndianKanoon.org for lawyer names in a case."""
    encoded = quote_plus(case_title)
    url = f"https://indiankanoon.org/search/?formInput={encoded}"
    lawyers = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')
        results = soup.select('.result, .result_title, .snippet')
        text = ' '.join(r.get_text(' ', strip=True) for r in results[:8])
        if not text:
            text = soup.get_text(' ', strip=True)[:5000]

        for name in _extract_names_from_text(text):
            lawyers.append({'name': name, 'source': 'IndianKanoon'})

        current_app.logger.warning(f"[IK] '{case_title[:40]}': {len(lawyers)} lawyers")
    except Exception as e:
        current_app.logger.error(f"[IK] error: {e}")
    return lawyers


def search_legal_news(case_title):
    """Search LiveLaw, Bar and Bench, SC Observer for lawyer names."""
    lawyers = []
    sites = [
        ('LiveLaw', f"site:livelaw.in {case_title} lawyer advocate"),
        ('BarAndBench', f"site:barandbench.com {case_title} lawyer advocate"),
        ('SCObserver', f"site:scobserver.in {case_title} lawyer advocate"),
    ]
    for site_name, query in sites:
        try:
            encoded = quote_plus(query)
            url = f"https://www.bing.com/search?q={encoded}&count=3"
            resp = requests.get(url, headers=HEADERS, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            snippets = [r.get_text(' ', strip=True) for r in soup.select('.b_algo')]
            text = ' '.join(snippets)

            # Scrape first matching result page for richer text
            for a in soup.select('.b_algo h2 a'):
                href = a.get('href', '')
                if any(s in href for s in ['livelaw', 'barandbench', 'scobserver']):
                    try:
                        page = requests.get(href, headers=HEADERS, timeout=8)
                        ps = BeautifulSoup(page.text, 'html.parser')
                        for tag in ps(['script', 'style', 'nav', 'footer']):
                            tag.decompose()
                        text += ' ' + ps.get_text(' ', strip=True)[:3000]
                    except Exception:
                        pass
                    break

            for name in _extract_names_from_text(text):
                lawyers.append({'name': name, 'source': site_name})

            current_app.logger.warning(
                f"[{site_name}] '{case_title[:40]}': "
                f"{sum(1 for l in lawyers if l['source'] == site_name)} found"
            )
        except Exception as e:
            current_app.logger.error(f"[{site_name}] error: {e}")
    return lawyers


def cross_verify_lawyers(ai_lawyers, search_lawyers, ik_lawyers, news_lawyers):
    """Cross-verify lawyer names from multiple sources, assign confidence."""
    master = []

    def _add(name, firm, role, src_type, src_detail):
        if not name or _normalize_name(name) in NAME_BLOCKLIST:
            return
        if name.lower() in ('unknown', 'n/a', 'not mentioned', 'unnamed'):
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
    for l in search_lawyers:
        _add(l.get('name', ''), l.get('firm', ''), l.get('role', ''),
             'bing_search', 'Bing web search + AI')
    for l in ik_lawyers:
        _add(l.get('name', ''), '', '', 'indiankanoon', 'IndianKanoon.org')
    for l in news_lawyers:
        _add(l.get('name', ''), '', '', 'legal_news', l.get('source', 'Legal news'))

    for entry in master:
        n_sources = len(set(s['type'] for s in entry['sources']))
        if n_sources >= 3:
            entry['confidence'] = 0.9
        elif n_sources == 2:
            entry['confidence'] = 0.6
        else:
            entry['confidence'] = 0.3
        entry['verified'] = n_sources >= 2

    master.sort(key=lambda x: x['confidence'], reverse=True)
    current_app.logger.warning(
        f"[VERIFY] {len(master)} lawyers, "
        f"{sum(1 for l in master if l['verified'])} verified"
    )
    return master


def _gather_lawyers_multi_source(case_title, article_analysis):
    """Orchestrate multi-source lawyer discovery and verification."""
    ai_lawyers = article_analysis.get('lawyers', []) if article_analysis else []

    search_lawyers = []
    search_text = search_case_lawyers(case_title)
    if search_text:
        extracted = identify_lawyers_from_search(case_title, search_text)
        if extracted:
            search_lawyers = extracted

    ik_lawyers = search_indiankanoon(case_title)
    news_lawyers = search_legal_news(case_title)

    return cross_verify_lawyers(ai_lawyers, search_lawyers, ik_lawyers, news_lawyers)


def _is_duplicate(title, source_url):
    """Check if case already exists by URL or similar title."""
    if source_url and LegalCase.query.filter_by(source_url=source_url).first():
        return True
    if LegalCase.query.filter_by(title=title).first():
        return True
    # Fuzzy: check first 50 chars of title
    prefix = title[:50].lower()
    for c in LegalCase.query.all():
        if c.title[:50].lower() == prefix:
            return True
    return False


def _collect_all_articles():
    """Gather articles from 3 tiers: direct RSS, site-specific Bing, generic Bing."""
    all_articles = []
    seen_urls = set()

    def _add_unique(articles):
        for a in articles:
            url = a['url'].strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_articles.append(a)

    # Tier 1: Direct RSS from LiveLaw & Bar and Bench
    current_app.logger.warning("--- Tier 1: Direct RSS feeds ---")
    for source_name, feed_url in LEGAL_RSS_FEEDS:
        _add_unique(fetch_rss_direct(feed_url, source_name))

    current_app.logger.warning(f"[TIER 1] {len(all_articles)} articles from RSS")

    # Tier 2: Site-specific Bing queries (LiveLaw + Bar and Bench)
    if len(all_articles) < MAX_NEW_CASES * 2:
        current_app.logger.warning("--- Tier 2: Site-specific Bing ---")
        for query in SITE_QUERIES:
            _add_unique(fetch_news(query, days=15))
        current_app.logger.warning(f"[TIER 2] {len(all_articles)} total articles")

    # Tier 3: Generic Bing queries (fallback)
    if len(all_articles) < MAX_NEW_CASES:
        current_app.logger.warning("--- Tier 3: Generic Bing fallback ---")
        for query in FALLBACK_QUERIES:
            _add_unique(fetch_news(query, days=15))
        current_app.logger.warning(f"[TIER 3] {len(all_articles)} total articles")

    return all_articles


def scan_for_cases():
    """Scan LiveLaw, Bar and Bench, and Bing for Indian legal cases."""
    new_cases = []
    total = 0
    current_app.logger.warning("=== SCAN START ===")

    articles = _collect_all_articles()
    current_app.logger.warning(f"[SCAN] {len(articles)} candidate articles")

    for article in articles:
        if total >= MAX_NEW_CASES:
            break

        title = article['title'].strip()
        source_url = article['url'].strip()
        if not title or _is_duplicate(title, source_url):
            continue

        try:
            # Step 1: Scrape article
            article_text = fetch_article_text(source_url)
            if len(article_text) < 100:
                current_app.logger.warning(f"[SKIP] Too short ({len(article_text)}): {title[:50]}")
                continue

            # Step 2: AI analysis
            current_app.logger.warning(f"[AI] Analyzing: {title[:50]}")
            analysis = analyze_case(title, article_text)

            # Step 3: Multi-source lawyer verification
            current_app.logger.warning(f"[VERIFY] Multi-source: {title[:50]}")
            verified_lawyers = _gather_lawyers_multi_source(title, analysis)

            # Build case
            case = LegalCase(
                title=title,
                summary=analysis.get('summary', article_text[:300]) if analysis else article_text[:300],
                source_url=source_url,
                source_name=article['source'],
                published_date=article['published'],
                status='active',
            )

            if analysis:
                case.ai_analysis = json.dumps(analysis)
                case.trending_score = _compute_trending_score(analysis)
            else:
                case.trending_score = 5.0

            # Step 4: Create lawyers with verification data
            for vl in verified_lawyers:
                name = vl.get('name', '')
                firm = vl.get('firm', '')
                lawyer = Lawyer(
                    name=name, firm=firm, role=vl.get('role', ''),
                    verified=vl.get('verified', False),
                    confidence_score=vl.get('confidence', 0.3),
                    verification_sources=json.dumps(vl.get('sources', [])),
                )

                current_app.logger.warning(
                    f"[CONTACT] {name} (conf: {vl['confidence']:.1f})"
                )
                email, linkedin, source = find_lawyer_email(name, firm)
                if email:
                    lawyer.email = email
                    lawyer.email_source = source
                if linkedin:
                    lawyer.linkedin_url = linkedin

                case.lawyers.append(lawyer)

            db.session.add(case)
            db.session.commit()
            new_cases.append(case)
            total += 1
            current_app.logger.warning(
                f"[SAVED] #{total}: {title[:50]} "
                f"(src: {article['source']}, {len(case.lawyers)} lawyers)"
            )

        except Exception as e:
            current_app.logger.error(f"[ERROR] Processing '{title[:40]}': {e}")
            db.session.rollback()
            continue

    current_app.logger.warning(f"=== SCAN DONE: {len(new_cases)} new cases ===")
    return new_cases


def _compute_trending_score(analysis):
    """Score based on AI analysis."""
    score = 5.0
    status = analysis.get('status', '')
    if status == 'active':
        score += 3.0
    elif status == 'developing':
        score += 2.0

    if len(analysis.get('lawyers', [])) > 2:
        score += 1.0

    area = analysis.get('practice_area', '').lower()
    high_interest = ['corporate', 'criminal', 'antitrust', 'securities', 'ip', 'constitutional',
                     'nclt', 'sebi', 'cci', 'insolvency', 'banking', 'cyber']
    if any(a in area for a in high_interest):
        score += 1.5

    return min(score, 10.0)
