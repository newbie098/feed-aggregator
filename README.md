# Feed Aggregator

Pulls articles from a curated set of sources, summarises new ones with Claude,
emails a digest. Runs on GitHub Actions, no server needed.

## What it does

For each source in `aggregate.py`:
1. Fetches articles published in the last 30 days (RSS or HTML scrape)
2. Skips ones it's already summarised (state in `seen.json`)
3. Sends each new article to Claude for a TLDR + 3 key points
4. Emails the digest via Resend
5. Commits the updated `seen.json` back to the repo

## Setup (one-time)

### 1. Create a private GitHub repo and push these files

```
feed-aggregator/
├── aggregate.py
├── requirements.txt
├── seen.json
└── .github/workflows/run.yml
```

### 2. Get an Anthropic API key

https://console.anthropic.com — create an API key. Pay-as-you-go;
expect ~$0.01–0.05 per article summarised with Haiku.

### 3. Get a Resend account (free tier is fine)

https://resend.com — sign up, grab an API key from the dashboard.
For the sender, you can use `onboarding@resend.dev` to start
(it works without domain verification but may land in spam — fine for testing).

### 4. Add GitHub repository secrets

Repo → Settings → Secrets and variables → Actions → New repository secret.
Add four secrets:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from step 2 |
| `RESEND_API_KEY` | from step 3 |
| `TO_EMAIL` | the email address to send digests to |
| `FROM_EMAIL` | `onboarding@resend.dev` (or your verified domain) |

### 5. Trigger the first run manually

Repo → Actions → "Feed Aggregator" → "Run workflow" → Run.

Watch the logs. If it works, you'll get an email digest within ~1–2 minutes.

## Switching to a schedule

Once happy with output, edit `.github/workflows/run.yml` and uncomment the
`schedule:` block. The example runs every Monday at 7am UTC.

## Adding/removing sources

Edit the `SOURCES` list at the top of `aggregate.py`.
- For a Substack/blog with RSS, use `"type": "rss"` and the feed URL.
- Microsoft WorkLab uses a custom scraper (`"type": "worklab"`).
- For a new scraped source, add a new fetcher function.

## Troubleshooting

- **No articles found for Microsoft WorkLab**: the scraper relies on date
  metadata in each article page. If Microsoft changes their HTML, the date
  selectors in `fetch_worklab` may need updating.
- **Email going to spam**: verify a domain in Resend and use it as `FROM_EMAIL`.
- **State out of sync**: delete `seen.json` (or set its contents to `[]`) and
  re-run; you'll get a digest of everything in the last 30 days again.
