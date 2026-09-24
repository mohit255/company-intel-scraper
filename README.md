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
| `daily_proxy_updater.py` / `auto_update_proxies.sh` | Legacy proxy refresh (merges with cache, keeps top N). Not used by the Docker cron jobs. |
| `check_proxy_health.py` | Reports pool health; triggers `fetch_fresh_proxies.py` if the working count is low. |
| `test_and_clean_proxies.py` | Tests every proxy in `proxies.txt` against `httpbin.org` and rewrites the file with only the working ones. |
| `setup_cron.sh` | Installs the Docker cron jobs (see [Docker cron jobs](#docker-cron-jobs)). |
| `docker-compose.yml` | Scraper service (host Postgres via `DATABASE_URL`). |
| `docker-compose.linux.yml` | Linux override: host networking so `localhost` reaches the server's Postgres. |
| `docker-entrypoint.sh` | Rewrites `localhost` in `DATABASE_URL` to `host.docker.internal` (macOS Docker Desktop). |
| `docker/cron.sh` | Cron entry point: `scrape`, `proxies`, `clean-proxies`, `backup`, `vacuum`. |

---

## Prerequisites

- Python 3.10+
- PostgreSQL (local, or a managed instance like RDS/Aurora)
- Docker, if you're using the container path

---

## Quick start (Docker)

The scraper runs in Docker against the **local (host) Postgres** from
`DATABASE_URL` in `.env`. Inside the container, `localhost` is rewritten to
`host.docker.internal` by `docker-entrypoint.sh`, so the same `.env` works
both on the host and in Docker.

```bash
git clone -b dockerSetup https://github.com/mohit255/company-intel-scraper.git && cd company-intel-scraper
cp .env.example .env                     # DATABASE_URL=postgresql://user:pass@localhost:5432/company_intel
touch proxies.txt proxies_working.txt proxies_failed.txt
docker compose build
docker compose run --rm scraper          # full run_scrape.sh
docker compose run --rm scraper python enrich_locations.py
```

`logs/`, `companies.json` and the `proxies*.txt` files are bind-mounted, so
they persist across runs and `companies.json` edits need no rebuild.

### Docker cron jobs

`docker/cron.sh <job>` runs one job and appends to `logs/cron-<job>.log`.
Scrape/proxy jobs run in a fixed-name container, so an overlapping run of the
same job is refused instead of piling up. Backup/vacuum use the host's
`pg_dump`/`psql`. On Linux, `cron.sh` automatically adds
`docker-compose.linux.yml` (host networking). Each run logs a `Compose:` line
and a `✅ DB connected` / `❌ DB connection FAILED` line at the start.

| Schedule | Job | What it does |
|---|---|---|
| `0 * * * *` | `scrape` | `run_scrape.sh` + `enrich_locations.py` |
| `30 */6 * * *` | `proxies` | `fetch_fresh_proxies.py` |
| `0 2 * * 0` | `clean-proxies` | `test_and_clean_proxies.py` |
| `0 1 * * *` | `backup` | `pg_dump` to `backups/*.sql.gz`, keeps 30 days |
| `0 5 * * 0` | `vacuum` | `VACUUM ANALYZE` |

Install / refresh them (idempotent):

```bash
./setup_cron.sh
```

macOS notes: Docker Desktop must be running (Settings -> "Start Docker
Desktop when you sign in"), and because the repo is under `~/Desktop`,
`/usr/sbin/cron` and your terminal need **Full Disk Access** (System
Settings -> Privacy & Security), otherwise `crontab` fails with
`Operation not permitted`.

---

## Ubuntu server deployment (Docker)

Runs the scraper in Docker on an Ubuntu server, against Postgres installed
**on that same server**, scheduled with the server's crontab. Tested on a
server that also runs other compose projects (e.g. the `company-intel` app).

### How it works on Linux

| Piece | What it does |
|---|---|
| `docker/cron.sh <job>` | Entry point for every job. On Linux it **always** runs `docker compose -f docker-compose.yml -f docker-compose.linux.yml`, so the container uses the **host network**. |
| `docker-compose.linux.yml` | `network_mode: host`. `localhost` inside the container is the server itself, so Postgres sees a normal local connection. **No `pg_hba.conf` or `listen_addresses` changes needed.** |
| `run_scrape.sh` | First checks the DB (10s timeout) and logs `✅ DB connected` or `❌ DB connection FAILED`. It aborts immediately on failure instead of after the 15-minute proxy refresh. |
| Compose project `company-intel-scraper` | A unique project name, so it never shares containers or networks with other projects on the server. |
| Fixed container names (`company-intel-scrape`, ...) | Overlap lock. A new run of a job is refused while the previous one is still running. |

### Step 1 — Pre-flight checks

Run these as the user that will own the crons (e.g. `mohit`), **not root**:

```bash
docker compose version                  # needs v2.24+ (docker-compose.linux.yml uses !reset)
docker ps                               # must work WITHOUT sudo
id -nG | grep -w docker                 # user is in the docker group
sudo systemctl is-enabled docker        # "enabled" = starts on boot
psql --version && pg_dump --version     # client tools for backup/vacuum
sudo systemctl is-active postgresql     # "active"
```

| Check fails | Fix |
|---|---|
| `docker compose` missing / too old | `curl -fsSL https://get.docker.com \| sudo sh` |
| `docker ps` → `permission denied` | `sudo usermod -aG docker $USER`, then **log out and back in** |
| Docker not enabled | `sudo systemctl enable --now docker` |
| `psql` / `pg_dump` missing | `sudo apt install -y postgresql-client` (same major version as the server, or newer) |
| Postgres not installed | `sudo apt install -y postgresql postgresql-contrib` |

### Step 2 — Database and user (skip if they exist)

```bash
sudo -u postgres psql -c "CREATE USER company_intel_rw WITH PASSWORD 'CHANGE_ME';"
sudo -u postgres psql -c "CREATE DATABASE company_intel OWNER company_intel_rw;"
```

The scraper creates its tables on the first run.

### Step 3 — Get the code

```bash
git clone -b dockerSetup https://github.com/mohit255/company-intel-scraper.git
cd company-intel-scraper
mkdir -p logs backups
touch proxies.txt proxies_working.txt proxies_failed.txt
```

The `proxies*.txt` files are gitignored but are bind-mounted into the
container. If they don't exist, Docker creates **directories** with those
names and the proxy scripts break.

### Step 4 — Configure `.env`

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Complete server `.env`:

```ini
# ── Database (local Postgres on this server) ─────────────────────────────────
DATABASE_URL=postgresql://company_intel_rw:CHANGE_ME@localhost:5432/company_intel

# ── Docker: Linux only (lets plain `docker compose ...` use host networking) ─
COMPOSE_FILE=docker-compose.yml:docker-compose.linux.yml

# ── Scraper tuning ───────────────────────────────────────────────────────────
SCRAPER_WORKERS=8
SCRAPER_NEWS_LIMIT=15
SCRAPER_JOBS_LIMIT=50
SCRAPER_PRODUCTS_LIMIT=10
SCRAPER_DELAY=0.5
SCRAPER_TIMEOUT=20
COMPANIES_FILE=companies.json

# ── Proxy pool (remove PROXY_FILE to run without proxies) ────────────────────
PROXY_FILE=proxies.txt
PROXY_ROTATION=random
MAX_PROXY_FAILURES=3
```

| Variable | Server | Mac (Docker Desktop) | Notes |
|---|---|---|---|
| `DATABASE_URL` | host **`localhost`** | host **`localhost`** | The only place credentials come from. Don't use `172.17.0.1` or `host.docker.internal`. |
| `COMPOSE_FILE` | set as above | **leave out** | `cron.sh` already loads the Linux file by itself. This line makes manual `docker compose run ...` commands use it too. |
| `SCRAPER_*`, `PROXY_*` | optional | optional | Defaults are in [Environment variables](#environment-variables). |

`.env` rules:
- No comments after a value on the same line; put them on their own line.
- No quotes, and no spaces around `=`.
- URL-encode special characters in the password (`@` → `%40`, `#` → `%23`, `/` → `%2F`).
- Use Unix line endings. If the file was edited on Windows, run `sed -i 's/\r$//' .env`.

### Step 5 — Verify the config before running

```bash
docker compose config | grep -E '^name:|network_mode|DB_HOST_OVERRIDE'
```

Expected:

```
name: company-intel-scraper
      DB_HOST_OVERRIDE: ""
    network_mode: host
```

Then test the DB connection from inside the container (takes about 5s):

```bash
docker compose run --rm scraper python -c "import os,psycopg; psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5); print('DB OK')"
```

### Step 6 — (Optional) Restore existing data

```bash
gunzip -c company_intel_YYYYMMDD.sql.gz | psql "$(grep ^DATABASE_URL= .env | cut -d= -f2-)"
```

### Step 7 — Build and first run

```bash
docker compose build                 # build the scraper image
docker/cron.sh scrape &              # same job cron runs hourly; output goes to the log
sleep 15; grep -E "Compose:|DB connected|DB connection FAILED" logs/cron-scrape.log | tail -2
tail -f logs/cron-scrape.log | grep --line-buffered -v "❌\|✅"   # follow progress, Ctrl+C to stop watching
```

`docker/cron.sh` prints nothing to the terminal; everything goes to
`logs/cron-<job>.log`. A first run takes about 6–20 minutes, because it
refreshes the proxy pool when there are fewer than 50 proxies.

A healthy run logs:

```
===== [scrape] start 2026-09-24 21:00:01 =====
Compose: docker compose -f docker-compose.yml -f docker-compose.linux.yml
✅ DB connected: localhost:5432/company_intel as company_intel_rw (PostgreSQL 16.x)
...
Database Summary:
  Companies: 213
...
1721 jobs -> 2202 location rows (...)
===== [scrape] end 2026-09-24 21:20:45 rc=0 =====
```

### Step 8 — Install the cron jobs

```bash
./setup_cron.sh        # idempotent; replaces its own block in the crontab
crontab -l | grep cron.sh
```

This installs the 5 jobs from [Docker cron jobs](#docker-cron-jobs). Run it as
your normal user, **not** with `sudo`. Otherwise the jobs land in root's
crontab and create root-owned files.

### Step 9 — Test backup and vacuum once

```bash
docker/cron.sh backup && ls -lh backups/        # company_intel_YYYYMMDD.sql.gz
docker/cron.sh vacuum && tail -3 logs/cron-vacuum.log
```

### Step 10 — Log rotation (recommended)

`logs/cron-*.log` files grow forever. Create `/etc/logrotate.d/company-scraper`
(adjust the path):

```
/var/www/html/pythonProjects/company-intel-scraper/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
```

`copytruncate` is needed because the files are held open by `>>` appends.

### Health checks

| What | Command | Healthy |
|---|---|---|
| Last scrape runs | `grep "\[scrape\] end" logs/cron-scrape.log \| tail -5` | every line ends with `rc=0` |
| Cron fired on schedule | `grep "\[scrape\] start" logs/cron-scrape.log \| tail -3` | times at `HH:00` |
| DB reachable | `grep -E "DB connected\|DB connection FAILED" logs/cron-scrape.log \| tail -1` | `✅ DB connected` |
| Job running now | `docker ps --format '{{.Names}}  {{.Status}}' \| grep company-intel-` | `Up N minutes` during a run |
| Live output | `docker logs -f --tail 50 company-intel-scrape` | progress lines |
| Crons installed | `crontab -l \| grep cron.sh` | 5 lines |
| Backups | `ls -lh backups/` | a file for each day |
| Cron daemon | `grep CRON /var/log/syslog \| tail -5` | `CMD (.../docker/cron.sh ...)` |

### Updating the code

```bash
git pull
docker compose build     # needed when anything copied into the image changed (*.py, run_scrape.sh, requirements.txt)
./setup_cron.sh          # only if the schedule in setup_cron.sh changed
```

| Changed file | Rebuild needed? |
|---|---|
| `*.py`, `run_scrape.sh`, `requirements.txt`, `Dockerfile` | **Yes**: `docker compose build` |
| `docker/cron.sh`, `docker-compose*.yml`, `companies.json`, `.env` | No; used from the host or mounted |
| `setup_cron.sh` | No, but re-run `./setup_cron.sh` |

### Troubleshooting

| Symptom (in `logs/cron-scrape.log`) | Cause → Fix |
|---|---|
| `Cannot use Docker as user X ... permission denied` | User not in the `docker` group → `sudo usermod -aG docker X`, then log out and back in. Check with `docker ps`. |
| `Cannot use Docker ... Is the docker daemon running?` | Docker stopped → `sudo systemctl enable --now docker` |
| `❌ DB connection FAILED: ... no pg_hba.conf entry for host "172.x.x.x"` | Container is on a bridge network, not the host network. Check the `Compose:` log line includes `docker-compose.linux.yml`, `DATABASE_URL` uses `localhost`, and `docker compose config` shows `network_mode: host`. |
| `❌ DB connection FAILED: ... password authentication failed` | Wrong user or password in `DATABASE_URL`, or unencoded special characters in the password |
| `❌ DB connection FAILED: ... Connection refused` | Postgres not running → `sudo systemctl status postgresql` |
| `Conflict. The container name "/company-intel-scrape" is already in use` | The previous run is still going (overlap protection, expected). If it's stuck: `docker rm -f company-intel-scrape` |
| Terminal looks "stuck" after `docker/cron.sh scrape` | Normal: output goes to the log file. Watch with `tail -f logs/cron-scrape.log`. |
| Old behaviour after `git pull` | Image not rebuilt → `docker compose build` |
| `pg_dump: server version mismatch` | Install `postgresql-client-<server major>` |
| `docker logs --tail -f ...` error | `--tail` needs a number: `docker logs -f --tail 50 <container>` |

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
| `COMPOSE_FILE` | — | Linux servers only: `docker-compose.yml:docker-compose.linux.yml` |
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

For scheduled runs in Docker, see [Docker cron jobs](#docker-cron-jobs).

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

The Docker setup handles this (`./setup_cron.sh`). For bare metal, to add a weekly deep-clean of the existing list on
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

It reads the `companies`, `news`, `jobs`, `products`, and `job_locations`
tables this scraper populates. It also creates and owns one additional
table of its own, `analytics_events`, for site traffic/click tracking
(see its README) — this repo has no knowledge of that table and never
writes to it.

---

## Note

Only scrape sites you're allowed to. Check the site's Terms of Service and
robots.txt, keep the delay reasonable, and identify yourself via User-Agent.
