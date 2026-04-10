import json
from datetime import datetime, timezone
from flask import current_app
from app import db
from app.models import LegalCase, Lawyer
from app.ai.gemma import search_legal_news, search_case_lawyers, search_lawyers_contact


LEGAL_QUERIES = [
    'India Supreme Court landmark case',
    'India High Court ruling lawyer',
    'Indian corporate lawsuit NCLT',
    'SEBI enforcement action India',
    'India legal dispute settlement',
    'India criminal trial high profile',
    'Indian antitrust CCI ruling',
]

MAX_NEW_CASES = 15


def scan_for_cases():
    """Scan for recent Indian legal cases using Google Search grounding."""
    new_cases = []
    total = 0
    current_app.logger.warning("Starting India-focused case scan (Google Search)...")

    for query in LEGAL_QUERIES:
        if total >= MAX_NEW_CASES:
            break

        articles = search_legal_news(query)
        current_app.logger.warning(f"Query '{query}': {len(articles)} articles from Google Search")

        for article in articles:
            if total >= MAX_NEW_CASES:
                break

            title = article.get('title', '').strip()
            source_url = article.get('source_url', '').strip()
            if not title:
                continue

            # Check duplicates by URL or title
            if source_url and LegalCase.query.filter_by(source_url=source_url).first():
                continue
            if LegalCase.query.filter_by(title=title).first():
                continue

            try:
                lawyers_data = article.get('lawyers', [])

                # If no lawyers found in initial search, do a focused search
                if not lawyers_data:
                    current_app.logger.warning(f"No lawyers in article, searching for: {title[:50]}")
                    lawyers_data = search_case_lawyers(title)

                # Build analysis
                analysis = {
                    'summary': article.get('summary', ''),
                    'lawyers': lawyers_data,
                    'practice_area': article.get('practice_area', ''),
                    'court': article.get('court', ''),
                    'status': article.get('status', 'active'),
                    'trending_reason': article.get('trending_reason', ''),
                }

                case = LegalCase(
                    title=title,
                    summary=article.get('summary', ''),
                    source_url=source_url,
                    source_name=article.get('source_name', 'News'),
                    published_date=datetime.now(timezone.utc),
                    status='active',
                    ai_analysis=json.dumps(analysis),
                    trending_score=_compute_trending_score(analysis),
                )

                # Search for contact info for all lawyers in this case
                if lawyers_data:
                    current_app.logger.warning(f"Searching contacts for {len(lawyers_data)} lawyers...")
                    contacts = search_lawyers_contact(title, lawyers_data)
                    contact_map = {c.get('name', '').lower(): c for c in contacts} if contacts else {}

                    for ld in lawyers_data:
                        name = ld.get('name', '')
                        lawyer = Lawyer(
                            name=name,
                            firm=ld.get('firm', ''),
                            role=ld.get('role', ''),
                        )

                        # Match contact info
                        contact = contact_map.get(name.lower(), {})
                        if contact.get('email'):
                            lawyer.email = contact['email']
                            lawyer.email_source = contact.get('email_source', '')
                        if contact.get('linkedin'):
                            lawyer.linkedin_url = contact['linkedin']

                        case.lawyers.append(lawyer)

                db.session.add(case)
                db.session.commit()
                new_cases.append(case)
                total += 1
                current_app.logger.warning(
                    f"Saved case #{total}: {title[:50]} ({len(lawyers_data)} lawyers)"
                )

            except Exception as e:
                current_app.logger.error(f"Error processing '{title[:50]}': {e}")
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
