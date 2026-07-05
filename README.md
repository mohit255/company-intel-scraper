# Company Intel Scraper

Scrapes news, job listings, and product/pricing pages for a configurable list
of companies (`companies.json`) and stores the results in PostgreSQL.

## What's in this repo

| File | Role |
|------|------|
| `scraper.py` | **Primary scraper.** Async, proxy-aware. This is what `run_scrape.sh` and cron run. |
| `company_scraper.py` | Lighter alternate entry point (no proxy support). Reuses helpers from `scraper.py`. Useful for quick manual/filtered runs. |
| `db.py` | PostgreSQL connection + schema (`companies`, `news`, `jobs`, `products`). Reads `DATABASE_URL`. |
| `run_scrape.sh` | Wraps `scraper.py`: loads `.env`, refreshes the proxy pool if it's running low, runs the scrape, prints a DB summary. This is the script cron should call. |
| `enrich_locations.py` | Post-processing pass that normalizes `jobs.location` into a `job_locations` table. Idempotent. |
| `proxy_manager.py` | `ProxyManager` (rotation/failure tracking) + `ProxyFetcher` (pulls proxies from public sources), used by the scripts below. |
| `fetch_fresh_proxies.py` | One-shot: fetch proxies from public sources, validate, write to `proxies.txt`. |
| `daily_proxy_updater.py` / `auto_update_proxies.sh` | Scheduled proxy refresh (merges with cache, keeps top N). Set up via `setup_cron.sh`. |
| `check_proxy_health.py` | Reports pool health; triggers `fetch_fresh_proxies.py` if the working count is low. |
| `test_and_clean_proxies.py` | Tests every proxy in `proxies.txt` against `httpbin.org` and rewrites the file with only the working ones. |
| `setup_cron.sh` | Installs the proxy-refresh cron jobs (see [Cron scheduling](#cron-scheduling)). |

---

## Prerequisites

- Python 3.10+
- PostgreSQL (local, or a managed instance like RDS/Aurora)
- Docker, if you're using the container path

---

## Quick start (Docker)

```bash
git clone <repo-url> && cd company-intel-scraper
cp .env.example .env          # fill in DATABASE_URL at minimum
docker build -t company-scraper .
docker run --rm --env-file .env company-scraper
```

> The Docker image only bundles `db.py`, `scraper.py`, `company_scraper.py`,
> `enrich_locations.py`, `run_scrape.sh`, and `companies.json` (see
> `Dockerfile`). `proxy_manager.py` and `fetch_fresh_proxies.py` are **not**
> copied in, so proxy rotation and auto-refresh are effectively disabled
> inside the container — it falls back to running with no proxies. Fine for
> a quick test; if you need proxy support in Docker, add those files to the
> `COPY` step yourself.

---

## Bare-metal setup (step by step)

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

### Step 2 — Create a deploy user (optional but recommended)

```bash
sudo useradd -m -s /bin/bash scraper
sudo su - scraper
```

All following commands run as this user.

### Step 3 — Clone the repo

```bash
git clone <repo-url> /home/scraper/company-intel-scraper
cd /home/scraper/company-intel-scraper
```

### Step 4 — Python virtual environment

```bash
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
```

### Step 5 — Create the database

```bash
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

### Step 6 — Configure environment

```bash
cp .env.example .env
nano .env
```

At minimum, set:

```ini
DATABASE_URL=postgresql://scraper:changeme@localhost:5432/company_intel
```

See [Environment variables](#environment-variables) for the full list. Lock
down the file so only your user can read it:

```bash
chmod 600 .env
```

### Step 7 — Create the logs directory

```bash
mkdir -p logs
```

(`logs/` is gitignored — this directory won't exist on a fresh clone.)

### Step 8 — Run once to verify

```bash
./.venv/bin/python scraper.py --companies companies.json --workers 4 --news-limit 5
```

Expected output ends with a run summary (`Total news scraped`, `Total jobs
scraped`, ... `Database totals`).

Check data landed:

```bash
psql "$DATABASE_URL" -c "SELECT company, title FROM news LIMIT 5;"
```

### Step 9 — Run the full pipeline manually

```bash
bash run_scrape.sh
```

This is the same command cron will run. Check logs:

```bash
tail -f logs/scrape.log
```

### Step 10 — Schedule with cron

See [Cron scheduling](#cron-scheduling) below — it covers the main scrape
job plus optional proxy/log/DB maintenance jobs.

### Updating the code

```bash
cd /home/scraper/company-intel-scraper
git pull
./.venv/bin/pip install -r requirements.txt   # pick up any new deps
```

The next cron run picks up the updated code automatically.

---

## Environment variables

All variables live in `.env` (copy from `.env.example`). `run_scrape.sh`
loads `.env` and translates these into `scraper.py` CLI flags — `db.py` is
the only Python module that reads an env var (`DATABASE_URL`) directly.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | — (required) | PostgreSQL connection string |
| `SCRAPER_WORKERS` | 8 | Concurrent worker tasks |
| `SCRAPER_NEWS_LIMIT` | 15 | Max news articles per company |
| `SCRAPER_JOBS_LIMIT` | 15 | Max job listings per company |
| `SCRAPER_PRODUCTS_LIMIT` | 10 | Max product/pricing pages per company |
| `SCRAPER_DELAY` | 0.5 | Min seconds between requests to the same domain |
| `SCRAPER_TIMEOUT` | 30 | HTTP request timeout (seconds) |
| `COMPANIES_FILE` | `companies.json` | Path to the companies config file |
| `PROXY_FILE` | `proxies.txt` | Proxy list file (unset/missing = run without proxies) |
| `PROXY_ROTATION` | `random` | `random` \| `round-robin` |
| `MAX_PROXY_FAILURES` | 3 | Failures before a proxy is dropped from rotation |

`--field` and `--only` (news/jobs/products/brand) are `company_scraper.py`
CLI flags, not env vars — pass them directly when running that script
manually (see below).

---

## Running the scraper

### Recommended: `run_scrape.sh`

Loads `.env`, tops up the proxy pool if it's under 50 entries, runs
`scraper.py` with all configured flags, and prints a DB summary at the end.

```bash
bash run_scrape.sh
```

### Direct: `scraper.py`

```bash
./.venv/bin/python scraper.py \
    --companies companies.json \
    --workers 8 \
    --news-limit 15 --jobs-limit 15 --products-limit 10 \
    --delay 0.5 --timeout 30 \
    --proxies proxies.txt --proxy-rotation random --max-proxy-failures 3
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--companies`, `-c` | companies.json | Path to companies config file |
| `--workers`, `-w` | 8 | Concurrent worker tasks |
| `--news-limit` | 15 | Max news articles per company |
| `--jobs-limit` | 15 | Max job listings per company |
| `--products-limit` | 10 | Max product pages per company |
| `--delay` | 1.0 | Min seconds between requests to one domain |
| `--timeout` | 30.0 | HTTP request timeout in seconds |
| `--proxies`, `-p` | — | File with one proxy per line |
| `--proxy-rotation`, `-r` | random | `random` \| `round-robin` |
| `--max-proxy-failures` | 3 | Failures before a proxy is removed |
| `--test-proxy URL` | — | Test a single proxy and exit |

### Alternate: `company_scraper.py`

No proxy support; adds `--only` and `--field` filters. Handy for ad-hoc,
scoped runs without touching the proxy pool.

```bash
./.venv/bin/python company_scraper.py                        # everything
./.venv/bin/python company_scraper.py --only news             # just news
./.venv/bin/python company_scraper.py --only jobs             # just jobs
./.venv/bin/python company_scraper.py --only products         # just products
./.venv/bin/python company_scraper.py --field AI               # only AI companies
```

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

Append to `companies.json`. Required: `name`, `field`, `website`,
`news_query`. Optional: `ats`, `products_url`.

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

---

## Proxy pool management

These are standalone scripts you can run manually or via cron. All of them
read/write `proxies.txt` (or `working_proxies_cache.json` for the daily
updater) in the repo root.

```bash
# Fetch a fresh batch from public proxy sources and validate them
./.venv/bin/python fetch_fresh_proxies.py

# Test every proxy currently in proxies.txt, rewrite with only the working ones
./.venv/bin/python test_and_clean_proxies.py

# Report pool health; auto-fetches more if working count < 20
./.venv/bin/python check_proxy_health.py

# Merge-and-refresh cycle (used by the daily cron job)
./.venv/bin/python daily_proxy_updater.py
```

`setup_cron.sh` installs the recurring jobs for you:

```bash
chmod +x setup_cron.sh
./setup_cron.sh
```

It adds two crontab entries that run `auto_update_proxies.sh` (midnight and
every 6 hours). **Edit the hardcoded path in `auto_update_proxies.sh` first**
(`cd /var/www/html/company-intel-scraper`) if your deploy path differs.

---

## Cron scheduling

Everything below assumes the repo lives at
`/home/scraper/company-intel-scraper` and runs as the `scraper` user — adjust
paths to match your deployment. Edit with:

```bash
crontab -e
```

### 1. Main scrape job (required)

`run_scrape.sh` has no built-in locking, so an overlapping run (a slow scrape
still running when the next one fires) will start a second concurrent
process against the same DB and proxy pool. Wrap it in `flock` so a new run
is skipped if the previous one hasn't finished, and pick an interval that
comfortably exceeds how long a full scrape takes for your company list —
hourly is a safe starting point for ~200 companies:

```
0 * * * * flock -n /tmp/company-scraper.lock -c "cd /home/scraper/company-intel-scraper && ./run_scrape.sh >> logs/scrape.log 2>&1"
```

If you need tighter freshness, drop to every 15 minutes — the `flock -n`
guard makes this safe to run aggressively without risking pile-up:

```
*/15 * * * * flock -n /tmp/company-scraper.lock -c "cd /home/scraper/company-intel-scraper && ./run_scrape.sh >> logs/scrape.log 2>&1"
```

Verify cron picked it up:

```bash
crontab -l
grep CRON /var/log/syslog       # Debian/Ubuntu
grep CRON /var/log/cron         # RHEL/Amazon Linux
```

### 2. Proxy maintenance (recommended if `PROXY_FILE` is set)

Handled by `./setup_cron.sh` (midnight + every 6 hours via
`auto_update_proxies.sh`). To add a weekly deep-clean of the existing list on
top of that:

```
0 2 * * 0 cd /home/scraper/company-intel-scraper && ./.venv/bin/python test_and_clean_proxies.py >> logs/proxy_cleanup.log 2>&1
```

### 3. Log rotation (recommended)

Create `/etc/logrotate.d/company-scraper`:

```
/home/scraper/company-intel-scraper/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

### 4. Database maintenance (optional)

```
# Daily backup at 1:00 AM — create the backups/ dir first: mkdir -p backups
0 1 * * * cd /home/scraper/company-intel-scraper && pg_dump "$DATABASE_URL" > backups/company_intel_$(date +\%Y\%m\%d).sql 2>&1

# Drop backups older than 30 days, at 1:30 AM
30 1 * * * find /home/scraper/company-intel-scraper/backups -name "*.sql" -mtime +30 -delete

# Weekly VACUUM ANALYZE, Sunday 5:00 AM
0 5 * * 0 psql "$DATABASE_URL" -c "VACUUM ANALYZE;" >> /home/scraper/company-intel-scraper/logs/vacuum.log 2>&1
```

`pg_dump`/`psql` need `DATABASE_URL` in the environment — either export it in
the crontab (`DATABASE_URL=...` as its own line at the top of the crontab)
or source `.env` in the command, same as `run_scrape.sh` does.

---

## PM2 service (Unix)

Alternative to cron — keeps the scraper as a managed, restartable service.

```bash
npm install -g pm2
```

`ecosystem.config.js`:

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

## Location enrichment (`enrich_locations.py`)

Parses free-text `jobs.location` strings into a normalized `job_locations`
table (`job_id`, `city`, `country`) for country/city filtering on the
website. Idempotent — rebuilds from scratch each run.

```bash
./.venv/bin/python enrich_locations.py
```

Run it after each scrape, or add it as its own cron line right after the
main scrape job.

---

## Querying the data

```bash
psql "$DATABASE_URL" -c "SELECT company, title, source FROM news WHERE company='Tesla';"
psql "$DATABASE_URL" -c "SELECT company, title, location FROM jobs LIMIT 20;"
psql "$DATABASE_URL" -c "SELECT company, prices FROM products;"
```

---

## Frontend

The `../company-intel` Next.js app is the front end for this data:

```bash
cd ../company-intel && npm run dev
```

---

## Note

Only scrape sites you're allowed to. Check the site's Terms of Service and
robots.txt, keep the delay reasonable, and identify yourself via User-Agent.
