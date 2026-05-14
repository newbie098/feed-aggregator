"""
Feed aggregator: pulls articles from RSS + scraped sources, summarises new ones
with Claude, and emails a digest via Resend.

State (which articles have been seen) is persisted to seen.json and committed
back to the repo by the GitHub Actions workflow.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup

# ---------- Config ----------

SOURCES = [
    {
        "name": "One Useful Thing",
        "type": "rss",
        "url": "https://www.oneusefulthing.org/feed",
    },
    {
        "name": "Microsoft WorkLab",
        "type": "worklab",
        "url": "https://www.microsoft.com/en-us/worklab",
    },
]

LOOKBACK_DAYS = 90
SEEN_FILE = "seen.json"
OUTPUT_FILE = "latest_digest.md"
USER_AGENT = "Mozilla/5.0 (compatible; feed-aggregator/1.0)"
MODEL = "claude-haiku-4-5-20251001"

# ---------- State ----------

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


def cutoff_date() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)


# ---------- Fetchers ----------

def fetch_rss(source: dict) -> list[dict]:
    """Parse an RSS/Atom feed and return articles published within the lookback window."""
    feed = feedparser.parse(source["url"])
    cutoff = cutoff_date()
    articles = []

    for entry in feed.entries:
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if not pub:
            continue
        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
        if pub_dt < cutoff:
            continue

        # Substack feeds put the full body in `content`; fall back to summary
        content = ""
        if entry.get("content"):
            content = entry.content[0].get("value", "")
        elif entry.get("summary"):
            content = entry.summary

        # Strip HTML for cleaner LLM input
        content_text = BeautifulSoup(content, "html.parser").get_text(" ", strip=True)

        articles.append({
            "title": entry.title,
            "url": entry.link,
            "date": pub_dt.isoformat(),
            "content": content_text,
            "source": source["name"],
        })

    return articles


def fetch_worklab(source: dict) -> list[dict]:
    """
    Microsoft WorkLab has no RSS feed. We scrape the listing page for article
    URLs, then visit each one to read its publish date from page metadata.
    """
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(source["url"], headers=headers, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Article URLs match: /en-us/worklab/<slug>
    # Exclude category pages and other non-article paths.
    excluded_paths = {
        "leadership", "culture", "innovation", "collaboration",
        "performance", "wellbeing", "guides", "podcast", "about",
        "newsletter", "sitemap", "work-trend-index", "frontier-firm-resources",
    }
    article_pattern = re.compile(r"^/en-us/worklab/([a-z0-9\-]+)/?$")

    candidate_urls = set()
    for a in soup.select("a[href]"):
        href = a["href"].split("?")[0].split("#")[0]
        if href.startswith("https://www.microsoft.com"):
            href = href[len("https://www.microsoft.com"):]
        match = article_pattern.match(href)
        if match and match.group(1) not in excluded_paths:
            candidate_urls.add(f"https://www.microsoft.com{href.rstrip('/')}")

    print(f"  Found {len(candidate_urls)} candidate article URLs")

    cutoff = cutoff_date()
    articles = []

    for url in candidate_urls:
        try:
            ar = requests.get(url, headers=headers, timeout=30)
            ar.raise_for_status()
        except Exception as e:
            print(f"    Skipped (fetch failed): {url} — {e}")
            continue

        asoup = BeautifulSoup(ar.text, "html.parser")

        # Try meta tags first
        date_str = None
        for selector, attr in [
            ('meta[property="article:published_time"]', "content"),
            ('meta[name="article:published_time"]', "content"),
            ('meta[itemprop="datePublished"]', "content"),
            ('meta[name="publication_date"]', "content"),
            ('time[datetime]', "datetime"),
        ]:
            el = asoup.select_one(selector)
            if el and el.get(attr):
                date_str = el[attr]
                break

        # Fall back: plain-text date in the body e.g. "April 08, 2026"
        if not date_str:
            page_text = asoup.get_text(" ", strip=True)
            m = re.search(
                r'\b(January|February|March|April|May|June|July|August|'
                r'September|October|November|December)\s+\d{1,2},\s+\d{4}\b',
                page_text,
            )
            if m:
                date_str = m.group(0)

        if not date_str:
            print(f"    Skipped (no date found): {url}")
            continue

        try:
            if "T" in date_str or "-" in date_str:
                # ISO format: 2026-04-08T00:00:00Z
                pub_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            else:
                # Plain text format: "April 08, 2026"
                pub_dt = datetime.strptime(date_str, "%B %d, %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"    Skipped (bad date format '{date_str}'): {url}")
            continue

        if pub_dt < cutoff:
            continue

        # Title: prefer h1, fall back to og:title
        title = None
        h1 = asoup.select_one("h1")
        if h1:
            title = h1.get_text(strip=True)
        if not title:
            ogt = asoup.select_one('meta[property="og:title"]')
            title = ogt.get("content", url) if ogt else url

        # Body: prefer <article>, fall back to <main>, then og:description
        body = ""
        article_el = asoup.select_one("article") or asoup.select_one("main")
        if article_el:
            body = article_el.get_text(" ", strip=True)
        if not body:
            desc = asoup.select_one('meta[property="og:description"]')
            body = desc.get("content", "") if desc else ""

        articles.append({
            "title": title,
            "url": url,
            "date": pub_dt.isoformat(),
            "content": body[:8000],  # cap to keep token use sane
            "source": source["name"],
        })

    return articles


def fetch(source: dict) -> list[dict]:
    if source["type"] == "rss":
        return fetch_rss(source)
    if source["type"] == "worklab":
        return fetch_worklab(source)
    raise ValueError(f"Unknown source type: {source['type']}")


# ---------- Summarisation ----------

SUMMARY_PROMPT = """You will summarise an article for a busy reader.

Source: {source}
Title: {title}
URL: {url}

Article content:
{content}

Output in EXACTLY this format, no preamble:

**TLDR:** <one concrete sentence>

**Key points:**
- <specific takeaway 1>
- <specific takeaway 2>
- <specific takeaway 3>

Rules:
- Be specific. Use concrete facts, numbers, names where present.
- No fluff, no marketing language, no "the article discusses..."
- Each key point should be standalone and useful by itself.
"""


def summarise(article: dict, client: Anthropic) -> str:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": SUMMARY_PROMPT.format(
                source=article["source"],
                title=article["title"],
                url=article["url"],
                content=article["content"][:8000],
            ),
        }],
    )
    return msg.content[0].text.strip()


# ---------- Email ----------

def markdown_to_html(md: str) -> str:
    """Lightweight markdown → HTML. Good enough for a digest email."""
    lines = md.split("\n")
    html_lines = []
    in_list = False

    for line in lines:
        if line.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            # Detect markdown links inside heading
            heading = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', line[3:])
            html_lines.append(f"<h2>{heading}</h2>")
        elif line.startswith("- "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = line[2:]
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            html_lines.append(f"<li>{content}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            content = line
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(r"^\*(.+?)\*$", r"<em>\1</em>", content)
            if content.strip():
                html_lines.append(f"<p>{content}</p>")

    if in_list:
        html_lines.append("</ul>")

    body = "\n".join(html_lines)
    return f"""<!DOCTYPE html>
<html><body style="font-family: -apple-system, sans-serif; max-width: 680px; margin: 0 auto; padding: 20px; line-height: 1.5;">
{body}
</body></html>"""


def send_email(digest_md: str) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    to_email = os.environ["TO_EMAIL"]
    from_email = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")

    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": from_email,
            "to": [to_email],
            "subject": f"Feed Digest (Last {LOOKBACK_DAYS} Days) — {datetime.now().strftime('%Y-%m-%d')}",
            "html": markdown_to_html(digest_md),
        },
        timeout=30,
    )
    r.raise_for_status()
    print(f"Email sent: {r.json().get('id')}")


# ---------- Main ----------

def main() -> None:
    seen = load_seen()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    all_articles = []
    for source in SOURCES:
        print(f"\nFetching {source['name']}...")
        try:
            articles = fetch(source)
        except Exception as e:
            print(f"  Error fetching {source['name']}: {e}")
            continue
        print(f"  {len(articles)} articles in last {LOOKBACK_DAYS} days")
        all_articles.extend(articles)

    if not all_articles:
        print("\nNo articles found in the lookback window. Exiting without sending email.")
        return

    print(f"\nSummarising {len(all_articles)} articles...")
    digest_parts = [
        f"# Feed Digest — Last {LOOKBACK_DAYS} Days "
        f"({datetime.now().strftime('%Y-%m-%d')})\n"
    ]

    for a in sorted(all_articles, key=lambda x: x["date"], reverse=True):
        print(f"  {a['source']}: {a['title']}")
        try:
            summary = summarise(a, client)
        except Exception as e:
            print(f"    Summarise failed: {e}")
            continue
        digest_parts.append(
            f"## [{a['title']}]({a['url']})\n"
            f"*{a['source']} — {a['date'][:10]}*\n\n"
            f"{summary}\n"
        )
        seen.add(a["url"])

    digest = "\n".join(digest_parts)

    with open(OUTPUT_FILE, "w") as f:
        f.write(digest)

    send_email(digest)
    save_seen(seen)
    print(f"\nDone. {len(all_articles)} articles processed.")


if __name__ == "__main__":
    main()
