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

## Contents

- [Setup with Docker (recommended)](#setup-with-docker-recommended): Ubuntu server, step by step
- [Docker cron jobs](#docker-cron-jobs): what runs when
- [Daily operations](#daily-operations): health checks, updates, stopping a run
- [Troubleshooting](#troubleshooting): every issue seen during setup, with the fix
- [Local development on macOS](#local-development-on-macos-docker-desktop)
- [Bare-metal setup (without Docker)](#bare-metal-setup-without-docker)

---

## Setup with Docker (recommended)

The scraper runs in Docker on an Ubuntu server against a **Postgres installed
on that same server**, and the server's crontab schedules the jobs. Postgres
is **not** run in Docker.

### How it works

| Piece | Role |
|---|---|
| `Dockerfile` | Python 3.12 image with the scraper, proxy tools and `run_scrape.sh` baked in |
| `docker-compose.yml` | `scraper` service, compose project **`company-intel-scraper`** (unique, so it never clashes with other projects on the server such as `company-intel`). Mounts `logs/`, `companies.json` and `proxies*.txt` from the repo. |
| `docker-compose.linux.yml` | `network_mode: host`. `localhost` in the container **is the server**, so Postgres sees a normal local connection. No `pg_hba.conf` changes are needed. |
| `docker-entrypoint.sh` | macOS only: rewrites `localhost` to `host.docker.internal`. Switched off by the Linux file. |
| `docker/cron.sh <job>` | Entry point for every job. On Linux it **always** adds `docker-compose.linux.yml`. It writes to `logs/cron-<job>.log` and uses a fixed container name as an overlap lock. |
| `run_scrape.sh` | Checks the DB connection **and** permissions first, and aborts in seconds if either is wrong. Then it refreshes proxies if there are fewer than 50, scrapes, and prints a DB summary. |
| `setup_cron.sh` | Installs the 5 cron jobs. Safe to run again. |

```
cron ──> docker/cron.sh scrape ──> docker compose run (host network) ──> run_scrape.sh
                                                                           │
                                                          localhost:5432 ──┘──> Postgres on the server
```

### Requirements

| Need | Version | Why |
|---|---|---|
| Ubuntu | 20.04+ | tested on Ubuntu server |
| Docker Engine + compose plugin | compose **v2.24+** | `docker-compose.linux.yml` uses `!reset` |
| PostgreSQL server + client | client major version ≥ server | the backup/vacuum jobs use the host's `pg_dump`/`psql` |
| A normal Linux user (e.g. `mohit`) | — | owns the crons; **not root** |

### Step 1 — Install Docker and allow your user to use it

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh     # Docker Engine + compose plugin
sudo systemctl enable --now docker              # start now and on every boot
sudo usermod -aG docker $USER                   # use docker without sudo
```

**Log out and SSH back in**, because group changes only apply to new logins.
Then check:

```bash
docker compose version       # v2.24 or newer
docker ps                    # must work WITHOUT sudo (no "permission denied")
id -nG | grep -w docker      # prints "docker"
systemctl is-enabled docker  # "enabled"
```

> **Issue seen:** `Cannot use Docker as user mohit ... permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`.
> The user wasn't in the `docker` group yet, or hadn't logged in again. Cron
> runs as your user, so `sudo docker` isn't an option.

### Step 2 — PostgreSQL: database, user and permissions

Install Postgres if it isn't there already:

```bash
sudo apt install -y postgresql postgresql-contrib   # includes psql and pg_dump
systemctl is-active postgresql                      # "active"
psql --version && pg_dump --version
```

Create the database and user, skipping any that already exist:

```bash
sudo -u postgres psql -c "CREATE USER company_intel_rw WITH PASSWORD 'CHANGE_ME';"
sudo -u postgres psql -c "CREATE DATABASE company_intel OWNER company_intel_rw;"
```

Grant the permissions the scraper needs. **This is required on PostgreSQL 15+.**

```bash
sudo -u postgres psql -d company_intel <<'SQL'
-- let the scraper create its tables/indexes in schema public
GRANT USAGE, CREATE ON SCHEMA public TO company_intel_rw;

-- if the scraper tables already exist (restored or created by another user),
-- hand over ownership: db.py runs ALTER TABLE on every start, which only the owner may do
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['companies','news','jobs','products','job_locations'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE public.%I OWNER TO company_intel_rw', t);
    END IF;
  END LOOP;
END $$;
SQL
```

If the frontend (`company-intel` app) connects as a **different** DB user,
let it read the scraper's tables:

```bash
sudo -u postgres psql -d company_intel -c "GRANT SELECT ON companies, news, jobs, products, job_locations TO <frontend_user>;"
```

Check, as the scraper user:

```bash
psql "postgresql://company_intel_rw:CHANGE_ME@localhost:5432/company_intel" -c "
select has_schema_privilege('public','CREATE') as can_create,
       (select string_agg(tablename||'='||tableowner, ', ') from pg_tables
         where schemaname='public' and tablename in ('companies','news','jobs','products','job_locations')) as owners;"
```

`can_create` must be `t`, and every listed table must be `=company_intel_rw`.
On a fresh DB the owners column is empty until the first scrape.

> **Issue seen:** `psycopg.errors.InsufficientPrivilege: permission denied for schema public`.
> PostgreSQL 15+ doesn't let ordinary users create objects in `public`.
> Rollback: `REVOKE CREATE ON SCHEMA public FROM company_intel_rw;`, then
> `ALTER TABLE <t> OWNER TO <previous owner>;`.

### Step 3 — Get the code and create the state files

```bash
cd /var/www/html/pythonProjects          # or any folder you like
git clone -b dockerSetup https://github.com/mohit255/company-intel-scraper.git
cd company-intel-scraper
mkdir -p logs backups
touch proxies.txt proxies_working.txt proxies_failed.txt
```

The `proxies*.txt` files are gitignored but get mounted into the container.
If they don't exist, Docker creates **directories** with those names and the
proxy scripts break. `docker/cron.sh` also creates them, but create them now
for manual runs.

### Step 4 — Configure `.env`

```bash
cp .env.example .env
nano .env
chmod 600 .env        # readable only by you: it holds the DB password
```

Complete server `.env`:

```ini
# ── Database (local Postgres on this server) ─────────────────────────────────
DATABASE_URL=postgresql://company_intel_rw:CHANGE_ME@localhost:5432/company_intel

# ── Docker: Linux only (manual `docker compose ...` commands use host network) ─
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

| Variable | What to set | Notes |
|---|---|---|
| `DATABASE_URL` | host **`localhost`** | The only place credentials come from. **Don't** use `172.17.0.1` or `host.docker.internal`. |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.linux.yml` | Linux only. `cron.sh` adds the Linux file itself; this line makes manual `docker compose run ...` commands behave the same. **Leave it out on macOS.** |
| `SCRAPER_*` | optional | Defaults are in [Environment variables](#environment-variables) |
| `PROXY_*` | optional | Without `PROXY_FILE` there's no proxy refresh, so the first run is about 15 minutes faster |

`.env` formatting rules. Breaking these causes silent misconfiguration:
- No comments after a value on the same line; put them on their own line.
- No quotes, and no spaces around `=`.
- URL-encode special characters in the password: `@` → `%40`, `#` → `%23`, `/` → `%2F`, `:` → `%3A`.
- Use Unix line endings. Check with `grep -c $'\r' .env` (should print `0`) and fix with `sed -i 's/\r$//' .env`.

### Step 5 — Verify the config before the first run

```bash
docker compose config | grep -E '^name:|network_mode|DB_HOST_OVERRIDE'
```

Expected:

```
name: company-intel-scraper
      DB_HOST_OVERRIDE: ""
    network_mode: host
```

Test the DB connection from inside a container (about 5 seconds):

```bash
docker compose run --rm scraper python -c "import os,psycopg; psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5); print('DB OK')"
```

> **Issue seen:** `no pg_hba.conf entry for host "172.19.0.2"` / `"172.20.0.2"`.
> The container was on Docker's bridge network, so it reached Postgres from
> a `172.x` address that `pg_hba.conf` rejects. `docker-compose.linux.yml`
> wasn't being loaded, and the compose project name also clashed with the
> `company-intel` app. Both are fixed: the project has its own name and
> `cron.sh` always loads the Linux file.

### Step 6 — (Optional) Restore existing data

Copy a backup from another machine (e.g. `scp backups/company_intel_YYYYMMDD.sql.gz mohit@server:~/`), then:

```bash
gunzip -c ~/company_intel_YYYYMMDD.sql.gz | psql "$(grep ^DATABASE_URL= .env | cut -d= -f2-)"
```

If the tables end up owned by a different role, **re-run the ownership block
from Step 2**. Skip this step to start empty; the first scrape creates the tables.

### Step 7 — Build the image

```bash
docker compose build
```

Rebuild after any `git pull` that changes `*.py`, `run_scrape.sh`,
`requirements.txt` or `Dockerfile`, because those are baked into the image.

### Step 8 — First run

```bash
docker rm -f company-intel-scrape 2>/dev/null      # clear any old failed run
docker/cron.sh scrape &                            # the same job cron runs hourly
sleep 15; grep -E "Compose:|DB connected|DB permissions|DB connection FAILED" logs/cron-scrape.log | tail -3
```

Within 15 seconds you should see:

```
Compose: docker compose -f docker-compose.yml -f docker-compose.linux.yml
✅ DB connected: localhost:5432/company_intel as company_intel_rw (PostgreSQL 16.x)
✅ DB permissions OK (CREATE on public, owns scraper tables)
```

Follow progress without the per-proxy ✅/❌ lines:

```bash
tail -f logs/cron-scrape.log | grep --line-buffered -v "❌ http\|✅ http"
```

Press `Ctrl+C` to stop watching; the job keeps running. A first run takes
about **6–20 minutes**. When `proxies.txt` has fewer than 50 entries it first
tests about 3,000 public proxies, which alone takes about 15 minutes. Finished means:

```
Database Summary:
  Companies: 213
  ...
1721 jobs -> 2202 location rows (...)
===== [scrape] end 2026-09-25 01:35:12 rc=0 =====
```

> **Issue seen:** "`docker/cron.sh scrape` is stuck". It isn't; all output
> goes to `logs/cron-scrape.log`, not the terminal. Watch the log, or use
> `docker logs -f --tail 50 company-intel-scrape`. Note that `--tail` needs a
> number, so `docker logs --tail -f` fails.

### Step 9 — Install the cron jobs

```bash
./setup_cron.sh              # idempotent; replaces only its own block in the crontab
crontab -l | grep cron.sh    # 5 lines
```

Run it as your normal user, **not with `sudo`**. Otherwise the jobs go into
root's crontab and create root-owned files. See [Docker cron jobs](#docker-cron-jobs)
for the schedule.

### Step 10 — Test backup and vacuum once

```bash
docker/cron.sh backup && ls -lh backups/           # company_intel_YYYYMMDD.sql.gz
docker/cron.sh vacuum && tail -3 logs/cron-vacuum.log   # "VACUUM", rc=0
```

These run on the host with its own `pg_dump`/`psql`, using `DATABASE_URL` from `.env`.

### Step 11 — Log rotation

`logs/cron-*.log` grows with every run. Create `/etc/logrotate.d/company-scraper`:

```bash
sudo tee /etc/logrotate.d/company-scraper >/dev/null <<EOF
$(pwd)/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
sudo logrotate -d /etc/logrotate.d/company-scraper     # dry run: check for errors
```

`copytruncate` is needed because the logs are appended to with `>>`.

### Step 12 — Confirm cron runs it by itself

After the next full hour:

```bash
grep "\[scrape\] start" logs/cron-scrape.log | tail -3   # a start at HH:00:0x that you didn't trigger
grep CRON /var/log/syslog | tail -5                      # CMD (.../docker/cron.sh scrape)
```

---

### Docker cron jobs

Installed by `./setup_cron.sh`. Each job logs to `logs/cron-<job>.log`.

| Schedule | Job | Runs | Where |
|---|---|---|---|
| `0 * * * *` (hourly) | `scrape` | `run_scrape.sh` + `enrich_locations.py` | container `company-intel-scrape` |
| `30 */6 * * *` | `proxies` | `fetch_fresh_proxies.py` | container `company-intel-proxies` |
| `0 2 * * 0` (Sun) | `clean-proxies` | `test_and_clean_proxies.py` | container `company-intel-clean-proxies` |
| `0 1 * * *` (daily) | `backup` | `pg_dump` to `backups/*.sql.gz`, keeps 30 days | host |
| `0 5 * * 0` (Sun) | `vacuum` | `VACUUM ANALYZE` | host |

Run any job by hand with `docker/cron.sh <job>`. While a job's container is
running, starting it again fails with a name conflict. This is deliberate:
it stops runs piling up.

---

## Daily operations

### Health checks

| What | Command | Healthy |
|---|---|---|
| Recent scrapes | `grep "\[scrape\] end" logs/cron-scrape.log \| tail -5` | every line ends with `rc=0` |
| Cron firing | `grep "\[scrape\] start" logs/cron-scrape.log \| tail -3` | times at `HH:00` |
| DB status | `grep -E "DB connected\|DB permissions\|DB connection FAILED" logs/cron-scrape.log \| tail -2` | both `✅` |
| Running now | `docker ps --format '{{.Names}}  {{.Status}}' \| grep company-intel-` | `Up N minutes` during a run |
| Live output | `docker logs -f --tail 50 company-intel-scrape` | progress lines |
| Crons installed | `crontab -l \| grep cron.sh` | 5 lines |
| Backups | `ls -lh backups/` | one file per day |
| Data | `psql "$(grep ^DATABASE_URL= .env \| cut -d= -f2-)" -c "select count(*) from news;"` | growing |

### Updating the code

```bash
git pull
docker compose build     # when *.py, run_scrape.sh, requirements.txt or Dockerfile changed
./setup_cron.sh          # only when setup_cron.sh (the schedule) changed
```

| Changed file | Action |
|---|---|
| `*.py`, `run_scrape.sh`, `requirements.txt`, `Dockerfile` | `docker compose build` |
| `docker/cron.sh`, `docker-compose*.yml`, `companies.json`, `.env` | nothing; these are read from the host |
| `setup_cron.sh` | `./setup_cron.sh` |

### Stopping a run

```bash
docker rm -f company-intel-scrape      # kills the run and frees the name for the next one
```

### Removing everything

```bash
crontab -l | sed '/# BEGIN company-intel-scraper/,/# END company-intel-scraper/d' | crontab -
docker rm -f company-intel-scrape company-intel-proxies company-intel-clean-proxies 2>/dev/null
docker rmi company-intel-scraper:latest
```

This leaves the database, `logs/` and `backups/` untouched.

---

## Troubleshooting

These are all the issues seen while setting this up on a server, in the order
they came up. Start with the first `❌` or error line in `logs/cron-<job>.log`.

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `sudo docker run --env-file .env company-scraper` can't reach the DB | In a container on the default network, `localhost` is the container itself | Don't use plain `docker run`. Use `docker/cron.sh scrape` or `docker compose run --rm scraper`. |
| 2 | `Cannot use Docker as user X ... permission denied ... docker.sock` | User not in the `docker` group, or no re-login since being added | `sudo usermod -aG docker X`, log out and back in, check `docker ps` |
| 3 | `Cannot use Docker ... Is the docker daemon running?` | Docker service stopped | `sudo systemctl enable --now docker` |
| 4 | `docker/cron.sh scrape` looks stuck | Output goes to the log file, not the terminal; proxy refresh takes about 15 min | `tail -f logs/cron-scrape.log` |
| 5 | `docker logs --tail -f <id>` error | `--tail` needs a number | `docker logs -f --tail 50 <id>` |
| 6 | `❌ DB connection FAILED: ... no pg_hba.conf entry for host "172.x.x.x"` | Container on a bridge network instead of the host network | Check the `Compose:` log line includes `docker-compose.linux.yml`, `DATABASE_URL` uses `localhost`, and `docker compose config` shows `network_mode: host`. `git pull` for the `cron.sh` fix. |
| 7 | Same as #6, but the address changes (`172.19` → `172.20`) after a fix | The compose project name clashed with another project (`company-intel`) | Fixed: the project is now `company-intel-scraper`. `git pull`. |
| 8 | A fix to `.env` made no difference | The running container was created before the change; `.env` is read when a container starts | `docker rm -f company-intel-scrape`, then run again |
| 9 | Code fix made no difference after `git pull` | Image not rebuilt | `docker compose build` |
| 10 | `❌ DB permissions: ... has no CREATE on schema public` / `InsufficientPrivilege: permission denied for schema public` | PostgreSQL 15+ doesn't give `CREATE` on `public` | Step 2 `GRANT USAGE, CREATE ON SCHEMA public TO company_intel_rw;` |
| 11 | `❌ DB permissions: ... does not own: companies, news, ...` | Tables were created or restored by another role; `db.py` runs `ALTER TABLE`, which only the owner may do | Step 2 ownership `DO $$ ... $$` block |
| 12 | `❌ DB connection FAILED: ... password authentication failed` | Wrong user or password, or unencoded special characters in the password | Fix `DATABASE_URL`; URL-encode `@ # / :` |
| 13 | `❌ DB connection FAILED: ... Connection refused` | Postgres not running, or not on port 5432 | `sudo systemctl status postgresql`; `sudo -u postgres psql -c 'show port'` |
| 14 | `Conflict. The container name "/company-intel-scrape" is already in use` | The previous run is still going (overlap lock) | Expected. If it's really stuck: `docker rm -f company-intel-scrape` |
| 15 | `pg_dump: error: server version mismatch` | Host `pg_dump` older than the server | `sudo apt install -y postgresql-client-<server major>` |
| 16 | Proxy files show up as directories | `proxies*.txt` didn't exist when the container first started | `rm -rf proxies*.txt && touch proxies.txt proxies_working.txt proxies_failed.txt` |
| 17 | `.env` values ignored or wrong | Windows line endings, quotes, or comments after values | `sed -i 's/\r$//' .env`; follow the Step 4 formatting rules |

---

## Local development on macOS (Docker Desktop)

The same setup works on a Mac against the Mac's local Postgres, with these differences:

- Leave `COMPOSE_FILE` **out** of `.env`. Docker Desktop reaches the host through
  `host.docker.internal`, which `docker-entrypoint.sh` substitutes for `localhost`.
- Docker Desktop must be running: Settings → General → "Start Docker Desktop when you sign in".
- For cron: if the repo is under `~/Desktop` or `~/Documents`, grant **Full Disk Access**
  to your terminal/IDE and to `/usr/sbin/cron` (System Settings → Privacy & Security).
  Otherwise `crontab` fails with `Operation not permitted`.

```bash
cp .env.example .env                 # DATABASE_URL=postgresql://user:pass@localhost:5432/company_intel
touch proxies.txt proxies_working.txt proxies_failed.txt
docker compose build
docker compose run --rm scraper      # full run_scrape.sh, output in the terminal
./setup_cron.sh                      # optional: same cron jobs as the server
```

---

## Bare-metal setup (without Docker)

Runs the scraper directly with a Python venv. Use this only if you can't use Docker.

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
\c company_intel
GRANT USAGE, CREATE ON SCHEMA public TO scraper;   -- required on PostgreSQL 15+
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
