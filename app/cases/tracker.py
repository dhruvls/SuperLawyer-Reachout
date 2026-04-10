import feedparser
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from flask import current_app
from app import db
from app.models import LegalCase, Lawyer
from app.ai.gemma import analyze_case


LEGAL_QUERIES = [
    'major lawsuit filed',
    'legal case ruling',
    'court case lawyer',
    'high profile trial',
    'legal dispute settlement',
    'class action lawsuit',
    'corporate litigation',
]


def fetch_google_news(query, days=15):
    """Fetch legal news from Google News RSS."""
    url = (
        f"https://news.google.com/rss/search?"
        f"q={query}+when:{days}d&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        feed = feedparser.parse(url)
        articles = []
        for entry in feed.entries[:10]:
            published = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            articles.append({
                'title': entry.get('title', ''),
                'url': entry.get('link', ''),
                'source': entry.get('source', {}).get('title', 'Google News'),
                'published': published,
            })
        return articles
    except Exception as e:
        current_app.logger.error(f"Google News fetch error: {e}")
        return []


def fetch_article_text(url):
    """Scrape article text from a URL."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; LegalTracker/1.0)'}
        resp = requests.get(url, headers=headers, timeout=10)
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

    for query in LEGAL_QUERIES:
        articles = fetch_google_news(query, days=15)

        for article in articles:
            existing = LegalCase.query.filter_by(source_url=article['url']).first()
            if existing:
                continue

            article_text = fetch_article_text(article['url'])
            if len(article_text) < 100:
                continue

            # AI analysis
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

            db.session.add(case)
            new_cases.append(case)

    db.session.commit()
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
    high_interest = ['corporate', 'criminal', 'antitrust', 'securities', 'ip', 'constitutional']
    if any(a in area for a in high_interest):
        score += 1.5

    return min(score, 10.0)
