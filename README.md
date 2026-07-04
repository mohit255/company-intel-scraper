# Web Scraper (worker-pool)

Async web crawler in Python. A shared URL queue feeds N concurrent workers;
each worker fetches a page, extracts title / description / text / links,
stores everything in SQLite, and queues newly discovered links.

## Architecture

```
seed URLs ──> asyncio.Queue ──> worker 1 ─┐
                    ^           worker 2 ─┼──> fetch ──> parse ──> SQLite
                    │           worker N ─┘                │
                    └────────── new links found ───────────┘
```

- **Worker pool** — `--workers N` concurrent fetchers (default 8)
- **Politeness** — per-domain delay shared across workers, robots.txt respected
- **Storage** — SQLite (`pages` + `links` tables), optional JSON export
- **Scope control** — max pages, max depth, same-domain-only by default

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
# Basic crawl: 8 workers, up to 50 pages
./.venv/bin/python scraper.py https://quotes.toscrape.com --workers 8 --max-pages 50

# Crawl deeper, export to JSON afterwards
./.venv/bin/python scraper.py https://example.com \
    --workers 16 --max-pages 500 --max-depth 5 \
    --db mysite.db --export-json pages.json
```

## Query the data

```bash
sqlite3 scraped.db "SELECT url, status, title FROM pages LIMIT 10;"
sqlite3 scraped.db "SELECT COUNT(*) FROM links;"
```

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--workers` | 8 | Number of concurrent worker tasks |
| `--max-pages` | 100 | Stop after this many pages stored |
| `--max-depth` | 3 | How many link-hops from the seed |
| `--delay` | 0.5 | Min seconds between hits to one domain |
| `--db` | scraped.db | SQLite output file |
| `--all-domains` | off | Also follow external links |
| `--no-robots` | off | Ignore robots.txt (not recommended) |
| `--export-json FILE` | — | Dump pages table to JSON at the end |

## Company scraper (news / jobs / products)

`company_scraper.py` scrapes top companies listed in `companies.json`:

- **News** — Google News RSS per company (works for any company, no API key)
- **Jobs** — public ATS APIs: Greenhouse, Lever, Ashby, Workday (per config)
- **Products & pricing** — the company's product/pricing page, price strings extracted

```bash
./.venv/bin/python company_scraper.py                       # everything → PostgreSQL
./.venv/bin/python company_scraper.py --only news           # just news
./.venv/bin/python company_scraper.py --field AI            # just AI companies
```

Add a company by appending to `companies.json` — only `name`, `field`,
`website`, and `news_query` are required; `ats` and `products_url` are optional.

Company data is stored in **PostgreSQL** (database `company_intel`, schema in
`db.py` — full-text search index on news titles, JSONB prices). Set
`DATABASE_URL` to point elsewhere (e.g. RDS/Aurora in prod):

```bash
export DATABASE_URL=postgresql://user:pass@host:5432/company_intel
```

Access the data:

```bash
psql company_intel -c "SELECT company, title, source FROM news WHERE company='Tesla';"
psql company_intel -c "SELECT company, title, location FROM jobs LIMIT 20;"
psql company_intel -c "SELECT company, prices FROM products;"
```

## Website

The `../company-intel` Next.js app is the front end for this data — see its
own README for setup. Run it with:

```bash
cd ../company-intel && npm run dev
```

## Automatic scraping

`run_scrape.sh` runs the full pipeline (`company_scraper.py` +
`enrich_locations.py`) and is scheduled via launchd
(`~/Library/LaunchAgents/com.companyintel.scraper.plist`) every 6 hours.
Logs land in `logs/scrape.log`.

## Note

Only scrape sites you're allowed to. Check the site's Terms of Service and
robots.txt, keep the delay reasonable, and identify yourself via User-Agent.
