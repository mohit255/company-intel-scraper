# Web Scraper / Company Intel

Two scrapers in one repo:

1. **`scraper.py`** — general-purpose async web crawler
2. **`company_scraper.py`** — company intelligence scraper (news, jobs, products)

---

## Quick start (Docker — recommended)

The fastest way to run on any machine with Docker installed.

```bash
git clone <repo-url> && cd web-scraper
cp .env.example .env          # fill in DATABASE_URL
docker compose up --build     # starts Postgres + scraper
```

That's it. See [Docker](#docker) for full details.

---

## Unix server setup (bare metal / VM)

Full walkthrough for running directly on a Linux server without Docker.

---

### Step 1 — System dependencies

```bash
# Debian / Ubuntu
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip \
    postgresql postgresql-contrib

# RHEL / Amazon Linux 2023
sudo dnf install -y git python3 python3-pip postgresql postgresql-server
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql
```

Verify:

```bash
python3 --version    # 3.10+
psql --version
```

---

### Step 2 — Create a deploy user (optional but recommended)

```bash
sudo useradd -m -s /bin/bash scraper
sudo su - scraper
```

All following commands run as this user.

---

### Step 3 — Clone the repo

```bash
git clone <repo-url> /home/scraper/web-scraper
cd /home/scraper/web-scraper
```

---

### Step 4 — Python virtual environment

```bash
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
```

---

### Step 5 — Create the database

```bash
# Switch to postgres superuser
sudo -u postgres psql
```

Inside `psql`:

```sql
CREATE USER scraper WITH PASSWORD 'changeme';
CREATE DATABASE company_intel OWNER scraper;
\q
```

Test the connection:

```bash
psql postgresql://scraper:changeme@localhost:5432/company_intel -c '\dt'
```

---

### Step 6 — Configure environment

```bash
cp .env.example .env
nano .env
```

```ini
DATABASE_URL=postgresql://scraper:changeme@localhost:5432/company_intel
```

Lock down the file so only your user can read it:

```bash
chmod 600 .env
```

---

### Step 7 — Create logs directory

```bash
mkdir -p logs
```

---

### Step 8 — Run once to verify

```bash
set -a && source .env && set +a
./.venv/bin/python company_scraper.py --only news
```

Expected output: `[w0] news <CompanyName>: N articles` for each company, ending with a totals line.

Check data landed:

```bash
psql "$DATABASE_URL" -c "SELECT company, title FROM news LIMIT 5;"
```

---

### Step 9 — Run the full pipeline manually

```bash
bash run_scrape.sh
```

Check logs:

```bash
tail -f logs/scrape.log
```

---

### Step 10 — Schedule with cron (every hour)

```bash
crontab -e
```

Add:

```
0 * * * * cd /home/scraper/web-scraper && set -a && source .env && set +a && bash run_scrape.sh >> logs/scrape.log 2>&1
```

Verify cron picked it up:

```bash
crontab -l
grep CRON /var/log/syslog       # Debian/Ubuntu
grep CRON /var/log/cron         # RHEL/Amazon Linux
```

---

### Step 11 — Keep logs from growing unbounded

Create `/etc/logrotate.d/company-scraper`:

```
/home/scraper/web-scraper/logs/scrape.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

---

### Updating the code

```bash
cd /home/scraper/web-scraper
git pull
./.venv/bin/pip install -r requirements.txt   # pick up any new deps
```

The next cron run will use the updated code automatically.

---

## Docker

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose v2

### Build and run (one-shot)

```bash
cp .env.example .env   # fill in DATABASE_URL pointing to your Postgres
docker build -t company-scraper .
docker run --rm --env-file .env company-scraper
```

### Compose (scraper + Postgres together)

```bash
docker compose up --build
```

`docker-compose.yml`:

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: company_intel
      POSTGRES_USER: scraper
      POSTGRES_PASSWORD: secret
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U scraper -d company_intel"]
      interval: 5s
      retries: 5

  scraper:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://scraper:secret@db:5432/company_intel
    depends_on:
      db:
        condition: service_healthy

volumes:
  pgdata:
```

### Scheduled runs via Docker + cron

```bash
# crontab -e — run every hour
0 * * * * docker run --rm --env-file /opt/web-scraper/.env company-scraper >> /var/log/company-scraper.log 2>&1
```

---

## PM2 service (Unix)

Keeps the scraper running as a managed service with cron-style scheduling.

### Install

```bash
npm install -g pm2
```

### `ecosystem.config.js`

```js
module.exports = {
  apps: [
    {
      name: "company-scraper",
      script: "./run_scrape.sh",
      interpreter: "/bin/bash",
      cron_restart: "0 * * * *",   // every hour
      autorestart: false,
      watch: false,
      env_file: ".env",
    },
  ],
};
```

### Commands

```bash
pm2 start ecosystem.config.js   # start
pm2 save                        # persist across reboots
pm2 startup                     # generate + run the startup hook

pm2 status                      # service health
pm2 logs company-scraper        # tail logs
pm2 restart company-scraper     # trigger a manual run now
pm2 delete company-scraper      # remove the service
```

---

## Company intel scraper (`company_scraper.py`)

Scrapes news, job listings, and product/pricing pages for companies listed in `companies.json`.

**Data sources per company:**

| Type | Source |
|------|--------|
| News | Google News RSS (no API key needed) |
| Jobs | Public ATS APIs: Greenhouse, Lever, Ashby, Workday |
| Products | Company's product/pricing page — price strings extracted |
| Brand | `og:image` / `twitter:image` from company homepage |

### Usage

```bash
./.venv/bin/python company_scraper.py                        # everything
./.venv/bin/python company_scraper.py --only news            # just news
./.venv/bin/python company_scraper.py --only jobs            # just jobs
./.venv/bin/python company_scraper.py --only products        # just products
./.venv/bin/python company_scraper.py --field AI             # only AI companies
```

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--companies` | companies.json | Path to companies config file |
| `--workers` | 8 | Concurrent worker tasks |
| `--news-limit` | 15 | Max news articles per company |
| `--jobs-limit` | 50 | Max job listings per company |
| `--only` | — | Scrape only: `news`, `jobs`, `products`, or `brand` |
| `--field` | — | Filter by field (e.g. `AI`, `Finance`) |
| `--delay` | 0.5 | Min seconds between requests to one domain |
| `--timeout` | 20.0 | HTTP request timeout in seconds |

### Adding a company

Append to `companies.json`. Required: `name`, `field`, `website`, `news_query`. Optional: `ats`, `products_url`.

```json
{
  "name": "Acme Corp",
  "field": "Technology",
  "website": "https://acme.com",
  "news_query": "Acme Corp",
  "products_url": "https://acme.com/pricing",
  "ats": { "type": "greenhouse", "board": "acmecorp" }
}
```

Supported ATS types: `greenhouse`, `lever`, `ashby`, `workday`.

### Query the data

```bash
psql company_intel -c "SELECT company, title, source FROM news WHERE company='Tesla';"
psql company_intel -c "SELECT company, title, location FROM jobs LIMIT 20;"
psql company_intel -c "SELECT company, prices FROM products;"
```

---

## General web crawler (`scraper.py`)

### Architecture

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

### Usage

```bash
./.venv/bin/python scraper.py https://quotes.toscrape.com --workers 8 --max-pages 50

./.venv/bin/python scraper.py https://example.com \
    --workers 16 --max-pages 500 --max-depth 5 \
    --db mysite.db --export-json pages.json
```

### Options

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

### Query the data

```bash
sqlite3 scraped.db "SELECT url, status, title FROM pages LIMIT 10;"
sqlite3 scraped.db "SELECT COUNT(*) FROM links;"
```

---

## Location enrichment (`enrich_locations.py`)

Parses free-text `jobs.location` strings into a normalized `job_locations` table (`job_id`, `city`, `country`) for country/city filtering on the website. Idempotent — rebuilds from scratch each run.

```bash
./.venv/bin/python enrich_locations.py
```

---

## Frontend

The `../company-intel` Next.js app is the front end for this data:

```bash
cd ../company-intel && npm run dev
```

---

## Note

Only scrape sites you're allowed to. Check the site's Terms of Service and robots.txt, keep the delay reasonable, and identify yourself via User-Agent.
