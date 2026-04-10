import feedparser
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from flask import current_app
from app import db
from app.models import LegalCase, Lawyer
from app.ai.gemma import analyze_case


LEGAL_QUERIES = [
    'India Supreme Court landmark case',
    'India High Court ruling lawyer',
    'Indian corporate lawsuit NCLT',
    'SEBI enforcement action India',
    'India legal dispute settlement',
    'India criminal trial high profile',
    'Indian antitrust CCI ruling',
]


def fetch_news(query, days=15):
    """Fetch legal news from Bing News RSS (works from cloud servers)."""
    encoded_query = quote_plus(query)
    url = f"https://www.bing.com/news/search?q={encoded_query}&format=rss&count=10&mkt=en-IN"
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        }
        resp = requests.get(url, headers=headers, timeout=15)
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
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        }
        resp = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
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


def scan_for_cases():
    """Scan news sources for recent legal cases and store them."""
    new_cases = []
    current_app.logger.warning("Starting case scan...")

    for query in LEGAL_QUERIES:
        articles = fetch_news(query, days=15)
        current_app.logger.warning(f"Query '{query}': {len(articles)} articles")

        processed = 0
        for article in articles:
            if processed >= 3:
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

                current_app.logger.warning(f"Analyzing: {article['title'][:60]}")
                analysis = analyze_case(article['title'], article_text)

                case = LegalCase(
                    title=article['title'],
                    summary=analysis.get('summary', article_text[:300]) if analysis else article_text[:300],
                    source_url=article['url'],
                    source_name=article['source'],
                    published_date=article['published'],
                    status='active',
                )

                if analysis:
                    import json
                    case.ai_analysis = json.dumps(analysis)
                    case.trending_score = _compute_trending_score(analysis)

                    for lawyer_data in analysis.get('lawyers', []):
                        lawyer = Lawyer(
                            name=lawyer_data.get('name', 'Unknown'),
                            firm=lawyer_data.get('firm', ''),
                            role=lawyer_data.get('role', 'unknown'),
                        )
                        case.lawyers.append(lawyer)
                else:
                    case.trending_score = 5.0

                db.session.add(case)
                new_cases.append(case)
                processed += 1
            except Exception as e:
                current_app.logger.error(f"Error processing article '{article['title'][:50]}': {e}")
                continue

    db.session.commit()
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
