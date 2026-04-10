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


LEGAL_QUERIES = [
    'India Supreme Court landmark case',
    'India High Court ruling lawyer',
    'Indian corporate lawsuit NCLT',
    'SEBI enforcement action India',
    'India legal dispute settlement',
    'India criminal trial high profile',
    'Indian antitrust CCI ruling',
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36'
}

MAX_NEW_CASES = 15
MAX_PER_QUERY = 3


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


def scan_for_cases():
    """Scan Bing News for Indian legal cases, analyze with AI, find lawyer emails."""
    new_cases = []
    total = 0
    current_app.logger.warning("=== SCAN START ===")

    for query in LEGAL_QUERIES:
        if total >= MAX_NEW_CASES:
            break

        articles = fetch_news(query, days=15)
        per_query = 0

        for article in articles:
            if per_query >= MAX_PER_QUERY or total >= MAX_NEW_CASES:
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

                # Step 3: If no lawyers found, search web for them
                if analysis and not analysis.get('lawyers'):
                    current_app.logger.warning(f"[AI] No lawyers in article, searching web...")
                    search_text = search_case_lawyers(title)
                    if search_text:
                        extra = identify_lawyers_from_search(title, search_text)
                        if extra:
                            analysis['lawyers'] = extra
                            current_app.logger.warning(f"[AI] Found {len(extra)} lawyers via web")

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

                    # Step 4: Create lawyers and find their emails
                    for ld in analysis.get('lawyers', []):
                        name = ld.get('name', '')
                        firm = ld.get('firm', '')
                        lawyer = Lawyer(name=name, firm=firm, role=ld.get('role', ''))

                        current_app.logger.warning(f"[CONTACT] Searching for: {name}")
                        email, linkedin, source = find_lawyer_email(name, firm)
                        if email:
                            lawyer.email = email
                            lawyer.email_source = source
                        if linkedin:
                            lawyer.linkedin_url = linkedin

                        case.lawyers.append(lawyer)
                else:
                    case.trending_score = 5.0

                db.session.add(case)
                db.session.commit()
                new_cases.append(case)
                total += 1
                per_query += 1
                current_app.logger.warning(f"[SAVED] #{total}: {title[:50]} ({len(case.lawyers)} lawyers)")

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
