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

MAX_NEW_CASES = 7


def fetch_news(query, days=15):
    """Fetch legal news from Bing News RSS (works from cloud servers)."""
    encoded_query = quote_plus(query)
    url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss&count=10&mkt=en-IN"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        current_app.logger.warning(f"News fetch [{query}]: status={resp.status_code}, len={len(resp.text)}")
        feed = feedparser.parse(resp.text)
        current_app.logger.warning(f"News parsed [{query}]: {len(feed.entries)} entries")

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
        current_app.logger.error(f"News fetch error: {e}")
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
        current_app.logger.error(f"Article scrape error for {url}: {e}")
        return ''


def search_case_lawyers(case_title):
    """Search Bing for lawyers/advocates involved in an Indian legal case."""
    query = f"{case_title} lawyer advocate senior counsel India"
    encoded = quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&count=5&mkt=en-IN"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, 'html.parser')

        snippets = []
        for result in soup.select('.b_algo'):
            snippets.append(result.get_text(strip=True))

        search_text = ' '.join(snippets)[:3000]
        current_app.logger.warning(f"Lawyer search for '{case_title[:40]}': {len(snippets)} results, {len(search_text)} chars")
        return search_text
    except Exception as e:
        current_app.logger.error(f"Lawyer search error: {e}")
        return ''


def find_lawyer_email(name, firm):
    """Search Bing for a lawyer's email, LinkedIn, and source URL."""
    if not name or name.lower() in ('unknown', 'n/a', 'not mentioned'):
        return None, None, None

    # Build search query
    search_query = f'"{name}" lawyer email'
    if firm and firm.lower() not in ('unknown', 'n/a', '', 'not mentioned'):
        search_query += f' "{firm}"'
    search_query += ' India'

    encoded = quote_plus(search_query)
    url = f"https://www.bing.com/search?q={encoded}&count=10"

    blocked_domains = [
        'example.com', 'bing.com', 'microsoft.com', 'google.com',
        'sampleemail', 'test.com', 'email.com', 'wikidata', 'wikipedia',
    ]
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    linkedin_pattern = r'https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9_%/-]+'

    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        html = resp.text

        # Check search results page for emails
        found_emails = re.findall(email_pattern, html)
        valid_emails = [e for e in found_emails if not any(b in e.lower() for b in blocked_domains)]

        # Check for LinkedIn
        linkedin_urls = re.findall(linkedin_pattern, html)

        if valid_emails:
            current_app.logger.warning(f"Found email for {name} on search page: {valid_emails[0]}")
            return valid_emails[0], linkedin_urls[0] if linkedin_urls else None, 'Bing search results'

        # Scrape top 2 result pages for emails
        soup = BeautifulSoup(html, 'html.parser')
        result_links = []
        for a in soup.select('.b_algo h2 a'):
            href = a.get('href', '')
            if href and 'bing.com' not in href and 'microsoft.com' not in href:
                result_links.append(href)

        for link in result_links[:3]:
            try:
                page = requests.get(link, headers=HEADERS, timeout=8, allow_redirects=True)
                page_emails = re.findall(email_pattern, page.text)
                page_valid = [e for e in page_emails if not any(b in e.lower() for b in blocked_domains)]
                page_linkedin = re.findall(linkedin_pattern, page.text)

                if page_valid:
                    current_app.logger.warning(f"Found email for {name} on {link[:60]}: {page_valid[0]}")
                    return (
                        page_valid[0],
                        page_linkedin[0] if page_linkedin else (linkedin_urls[0] if linkedin_urls else None),
                        link,
                    )
            except Exception:
                continue

        return None, linkedin_urls[0] if linkedin_urls else None, None
    except Exception as e:
        current_app.logger.error(f"Email search error for {name}: {e}")
        return None, None, None


def scan_for_cases():
    """Scan news for recent Indian legal cases, identify lawyers, and find their emails."""
    new_cases = []
    total_processed = 0
    current_app.logger.warning("Starting India-focused case scan...")

    for query in LEGAL_QUERIES:
        if total_processed >= MAX_NEW_CASES:
            break

        articles = fetch_news(query, days=15)
        current_app.logger.warning(f"Query '{query}': {len(articles)} articles")

        per_query = 0
        for article in articles:
            if per_query >= 2 or total_processed >= MAX_NEW_CASES:
                break

            existing = LegalCase.query.filter_by(source_url=article['url']).first()
            if existing:
                continue

            try:
                article_text = fetch_article_text(article['url'])
                if len(article_text) < 100:
                    current_app.logger.warning(
                        f"Skipping (short: {len(article_text)}): {article['title'][:60]}"
                    )
                    continue

                # Step 1: AI analysis of the article
                current_app.logger.warning(f"Analyzing: {article['title'][:60]}")
                analysis = analyze_case(article['title'], article_text)

                # Step 2: If no named lawyers found, do a secondary web search
                if analysis and not analysis.get('lawyers'):
                    current_app.logger.warning(f"No lawyers in article, searching web...")
                    search_text = search_case_lawyers(article['title'])
                    if search_text:
                        extra_lawyers = identify_lawyers_from_search(article['title'], search_text)
                        if extra_lawyers:
                            analysis['lawyers'] = extra_lawyers
                            current_app.logger.warning(f"Found {len(extra_lawyers)} lawyers via web search")

                case = LegalCase(
                    title=article['title'],
                    summary=analysis.get('summary', article_text[:300]) if analysis else article_text[:300],
                    source_url=article['url'],
                    source_name=article['source'],
                    published_date=article['published'],
                    status='active',
                )

                if analysis:
                    case.ai_analysis = json.dumps(analysis)
                    case.trending_score = _compute_trending_score(analysis)

                    # Step 3: Create lawyers and find their emails
                    for lawyer_data in analysis.get('lawyers', []):
                        name = lawyer_data.get('name', '')
                        firm = lawyer_data.get('firm', '')
                        role = lawyer_data.get('role', 'unknown')

                        lawyer = Lawyer(name=name, firm=firm, role=role)

                        # Search for email and LinkedIn
                        current_app.logger.warning(f"Searching contact for: {name}")
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
                total_processed += 1
                per_query += 1

            except Exception as e:
                current_app.logger.error(f"Error processing article '{article['title'][:50]}': {e}")
                db.session.rollback()
                continue

    current_app.logger.warning(f"Scan complete: {len(new_cases)} new cases")
    return new_cases


def _compute_trending_score(analysis):
    """Simple scoring heuristic based on AI analysis."""
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
