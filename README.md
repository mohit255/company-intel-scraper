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
- [Daily operations](#daily-operations): keeping it running, run history, health checks, updates
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

### How to read these steps

Each command is listed on its own with:
- **What:** what the command does
- **Why:** why the setup needs it
- **Expected:** what you should see if it worked
- **If it fails:** what to do

Run everything as your **normal user** (e.g. `mohit`) over SSH. Commands that
need admin rights start with `sudo`. Don't switch to root.

---

### Step 1 — Install Docker and allow your user to use it

**1.1 Update the package lists**
```bash
sudo apt update
```
- **What:** refreshes Ubuntu's list of available packages.
- **Why:** so the next installs get current versions.
- **Expected:** ends with `Reading package lists... Done`.
- **If it fails:** check internet access from the server (`ping -c1 archive.ubuntu.com`).

**1.2 Install the basic tools**
```bash
sudo apt install -y ca-certificates curl git
```
- **What:** installs HTTPS certificates, `curl` (downloads files) and `git` (gets the code).
- **Why:** the Docker install script and `git clone` need them.
- **Expected:** `... is already the newest version` or `Setting up ...` lines.

**1.3 Install Docker Engine and the compose plugin**
```bash
curl -fsSL https://get.docker.com | sudo sh
```
- **What:** runs Docker's official install script.
- **Why:** it installs `docker` and `docker compose` (v2) from Docker's repository. Ubuntu's own `docker.io` package can be too old.
- **Expected:** ends with Docker version info. Skip this step if `docker compose version` already shows v2.24 or newer.

**1.4 Start Docker now and on every boot**
```bash
sudo systemctl enable --now docker
```
- **What:** `enable` makes Docker start on boot; `--now` also starts it right away.
- **Why:** the cron jobs need Docker running, including after a reboot.
- **Expected:** no output, or `Created symlink ...`.

**1.5 Let your user run Docker without sudo**
```bash
sudo usermod -aG docker $USER
```
- **What:** adds your user to the `docker` group.
- **Why:** cron runs jobs as your user, and it can't type a `sudo` password.
  Without this, every job logs `permission denied ... docker.sock`.
- **Expected:** no output.
- **Note:** being in the `docker` group is effectively root access on this server. Add only trusted users.

**1.6 Log out and back in**
```bash
exit
ssh mohit@<server>
```
- **What:** starts a new login session.
- **Why:** group membership only applies to new logins.
- **Tip:** `newgrp docker` applies it to the current shell only, as a quick alternative.

**1.7 Check Docker works**
```bash
docker compose version
```
- **Expected:** `Docker Compose version v2.24.0` or newer.
- **If it fails:** older than v2.24 → re-run 1.3 (`docker-compose.linux.yml` uses `!reset`, which needs v2.24).

```bash
docker ps
```
- **Expected:** a header line `CONTAINER ID   IMAGE   COMMAND ...`, and **no** `permission denied`.
- **If it fails:** `permission denied` → you haven't logged in again after 1.5.

```bash
id -nG
```
- **Expected:** the list includes `docker`.

```bash
systemctl is-enabled docker
```
- **Expected:** `enabled`.

---

### Step 2 — PostgreSQL: database, user and permissions

Postgres runs **directly on the server**, not in Docker.

**2.1 Install Postgres (skip if it's already installed)**
```bash
sudo apt install -y postgresql postgresql-contrib
```
- **What:** installs the Postgres server and client tools (`psql`, `pg_dump`).
- **Why:** the scraper stores its data here, and the backup/vacuum jobs use `pg_dump`/`psql`.

**2.2 Check Postgres is running and starts on boot**
```bash
sudo systemctl enable --now postgresql
systemctl is-active postgresql
```
- **Expected:** `active`.

**2.3 Check the client tools**
```bash
psql --version
pg_dump --version
```
- **Expected:** `psql (PostgreSQL) 16.x` (any version). The `pg_dump` major version must be **the same as the server or newer**.
- **If it fails:** `sudo apt install -y postgresql-client`.

**2.4 Create the database user**
```bash
sudo -u postgres psql -c "CREATE USER company_intel_rw WITH PASSWORD 'CHANGE_ME';"
```
- **What:** creates the login the scraper uses. `sudo -u postgres` runs `psql` as the Postgres admin.
- **Why:** the scraper connects as this user through `DATABASE_URL`.
- **Expected:** `CREATE ROLE`.
- **If it fails:** `role "company_intel_rw" already exists` → fine, continue.
- Use a strong password. If it contains `@ # / :`, you'll URL-encode it in Step 4.

**2.5 Create the database**
```bash
sudo -u postgres psql -c "CREATE DATABASE company_intel OWNER company_intel_rw;"
```
- **Expected:** `CREATE DATABASE`.
- **If it fails:** `already exists` → fine, continue.

**2.6 Allow the user to create tables in schema `public`**
```bash
sudo -u postgres psql -d company_intel -c "GRANT USAGE, CREATE ON SCHEMA public TO company_intel_rw;"
```
- **What:** lets `company_intel_rw` create tables and indexes in `public`.
- **Why:** **required on PostgreSQL 15+**, which no longer allows this by default.
  The scraper runs `CREATE TABLE IF NOT EXISTS ...` every time it starts.
- **Expected:** `GRANT`.
- **Issue seen without it:** `psycopg.errors.InsufficientPrivilege: permission denied for schema public`.

**2.7 Make the user the owner of the scraper tables (only if they already exist)**
```bash
sudo -u postgres psql -d company_intel <<'SQL'
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
- **What:** for each of the 5 scraper tables that exists, makes `company_intel_rw` its owner.
- **Why:** `db.py` runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on every start, and only the table owner can do that.
  This matters when the tables were created or restored by another user.
- **Expected:** `DO`. It's safe on an empty DB, where it does nothing, and safe to run again.
- Other tables (e.g. the frontend's `analytics_events`) are **not** touched.

**2.8 (Only if the frontend uses a different DB user) Let it read the scraper tables**
```bash
sudo -u postgres psql -d company_intel -c "GRANT SELECT ON companies, news, jobs, products, job_locations TO <frontend_user>;"
```
- **Why:** after 2.7 the tables belong to `company_intel_rw`, and other users need an explicit read grant.
- On a fresh DB, run this after the first scrape (Step 8), once the tables exist.

**2.9 Check the permissions as the scraper user**
```bash
psql "postgresql://company_intel_rw:CHANGE_ME@localhost:5432/company_intel" -c "
select has_schema_privilege('public','CREATE') as can_create,
       (select string_agg(tablename||'='||tableowner, ', ') from pg_tables
         where schemaname='public' and tablename in ('companies','news','jobs','products','job_locations')) as owners;"
```
- **Expected:** `can_create = t`. `owners` is empty on a fresh DB, or lists every table as `=company_intel_rw`.
- **If it fails:** `password authentication failed` → wrong password. `f` → re-run 2.6. A table owned by someone else → re-run 2.7.

**Rollback for Step 2:** `REVOKE CREATE ON SCHEMA public FROM company_intel_rw;` and `ALTER TABLE <t> OWNER TO <previous owner>;`.

---

### Step 3 — Get the code and create the state files

**3.1 Create the parent folder and make it yours**
```bash
sudo mkdir -p /var/www/html/pythonProjects
sudo chown $USER:$USER /var/www/html/pythonProjects
```
- **Why:** `/var/www` belongs to root, and your user must be able to write the repo, logs and backups there.

**3.2 Clone the repo**
```bash
cd /var/www/html/pythonProjects
git clone -b dockerSetup https://github.com/mohit255/company-intel-scraper.git
```
- **What:** downloads the code on the `dockerSetup` branch.
- **Expected:** `Cloning into 'company-intel-scraper'...`.

**3.3 Go into the repo**
```bash
cd company-intel-scraper
git branch --show-current
```
- **Expected:** `dockerSetup`. **Every later command runs from this folder.**

**3.4 Create the logs and backups folders**
```bash
mkdir -p logs backups
```
- **Why:** they're gitignored, so a fresh clone doesn't have them. Jobs write logs and backups here.

**3.5 Create the proxy state files**
```bash
touch proxies.txt proxies_working.txt proxies_failed.txt
ls -l proxies*.txt
```
- **What:** creates the three empty files.
- **Why:** they're mounted into the container. If a file doesn't exist, Docker creates a **directory** with that name instead, and the proxy scripts break.
- **Expected:** three lines starting with `-rw` (files), **not** `drw` (directories).
- **If it fails:** they're directories → `rm -rf proxies*.txt` and run `touch` again.

---

### Step 4 — Configure `.env`

**4.1 Copy the template**
```bash
cp .env.example .env
```

**4.2 Edit it**
```bash
nano .env
```
Replace the contents with this. Set your real password, then save with `Ctrl+O`, `Enter`, `Ctrl+X`:

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

What each line is for:

| Variable | What to set | Why |
|---|---|---|
| `DATABASE_URL` | `postgresql://USER:PASSWORD@localhost:5432/DBNAME` | The only place credentials come from. Keep the host **`localhost`**. Don't use `172.17.0.1` or `host.docker.internal`. |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.linux.yml` | Puts manual `docker compose run ...` commands on the host network, so `localhost` reaches the server's Postgres. (`docker/cron.sh` adds this by itself.) **Linux only; leave it out on a Mac.** |
| `SCRAPER_WORKERS` | `8` | Companies scraped in parallel |
| `SCRAPER_NEWS_LIMIT` / `SCRAPER_JOBS_LIMIT` / `SCRAPER_PRODUCTS_LIMIT` | `15` / `50` / `10` | Maximum items per company per run |
| `SCRAPER_DELAY` | `0.5` | Minimum seconds between requests to the same site (politeness) |
| `SCRAPER_TIMEOUT` | `20` | Seconds before an HTTP request gives up |
| `COMPANIES_FILE` | `companies.json` | Companies to scrape. It's mounted, so edits need no rebuild. |
| `PROXY_FILE` | `proxies.txt` | Proxy pool. Delete the line to run without proxies, which makes the first run about 15 minutes faster. |
| `PROXY_ROTATION` | `random` | `random` or `round-robin` |
| `MAX_PROXY_FAILURES` | `3` | Failures before a proxy is dropped |

Formatting rules. Breaking one gives silently wrong settings:
- No comments after a value on the same line (`SCRAPER_WORKERS=8  # x` is wrong); put comments on their own line.
- No quotes, and no spaces around `=`.
- URL-encode special characters in the password: `@` → `%40`, `#` → `%23`, `/` → `%2F`, `:` → `%3A`.

**4.3 Make the file readable only by you**
```bash
chmod 600 .env
ls -l .env
```
- **Why:** it holds the DB password.
- **Expected:** `-rw------- 1 mohit mohit ... .env`.

**4.4 Check for Windows line endings**
```bash
grep -c $'\r' .env
```
- **Expected:** `0`.
- **If it fails:** any other number → `sed -i 's/\r$//' .env`. This happens if the file was edited on Windows.

**4.5 Check the two important lines (password hidden)**
```bash
grep -E '^(DATABASE_URL|COMPOSE_FILE)=' .env | sed -E 's#://([^:]+):[^@]+@#://\1:****@#'
```
- **Expected:**
  ```
  DATABASE_URL=postgresql://company_intel_rw:****@localhost:5432/company_intel
  COMPOSE_FILE=docker-compose.yml:docker-compose.linux.yml
  ```

---

### Step 5 — Verify the config before the first run

**5.1 Check compose uses the host network**
```bash
docker compose config | grep -E '^name:|network_mode|DB_HOST_OVERRIDE'
```
- **What:** prints the final merged compose config and keeps only the key lines.
- **Why:** it confirms the Linux override is loaded, **before** you wait 15 minutes for a run.
- **Expected:**
  ```
  name: company-intel-scraper
        DB_HOST_OVERRIDE: ""
      network_mode: host
  ```
- **If it fails:** no `network_mode: host` → the `COMPOSE_FILE` line is missing or mistyped (see 4.5).

**5.2 Test the DB connection from inside a container**
```bash
docker compose run --rm scraper python -c "import os,psycopg; psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5); print('DB OK')"
```
- **What:** starts a one-off container, connects to the DB, prints the result and removes the container.
- **Why:** it's a 5-second check of the whole path (container → host network → Postgres → login).
- **Expected:** `DB OK`. The first time, it builds the image first, which takes about 1 minute.
- **If it fails:**
  - `no pg_hba.conf entry for host "172.x.x.x"` → not on the host network; fix 5.1.
  - `password authentication failed` → wrong password or unencoded special characters (Step 4).
  - `Connection refused` → Postgres isn't running (2.2).

---

### Step 6 — (Optional) Restore existing data

Skip this to start with an empty database; the first scrape creates the tables.

**6.1 Copy a backup to the server (run on the machine that has it)**
```bash
scp backups/company_intel_YYYYMMDD.sql.gz mohit@<server>:~/
```

**6.2 Load it into the database**
```bash
gunzip -c ~/company_intel_YYYYMMDD.sql.gz | psql "$(grep ^DATABASE_URL= .env | cut -d= -f2-)"
```
- **What:** unpacks the dump and runs it against the DB from `.env`.
- **Expected:** a stream of `CREATE TABLE`, `COPY n`, `ALTER TABLE` lines.

**6.3 Fix table ownership**

Re-run **2.7**, then **2.9**.
- **Why:** restored tables can end up owned by another role, which makes the startup permission check fail.

---

### Step 7 — Build the image

**7.1 Build**
```bash
docker compose build
```
- **What:** builds `company-intel-scraper:latest` from the `Dockerfile`: Python 3.12, the dependencies, and all the scripts.
- **Why:** every job runs from this image.
- **Expected:** ends with `Image company-intel-scraper:latest Built`.

**7.2 Confirm the image exists**
```bash
docker images company-intel-scraper
```
- **Expected:** one row with the `latest` tag.

**When to rebuild:** after any `git pull` that changes `*.py`, `run_scrape.sh`,
`requirements.txt` or `Dockerfile`, because those are copied into the image.

---

### Step 8 — First run

**8.1 Clear any old failed run**
```bash
docker rm -f company-intel-scrape 2>/dev/null
```
- **Why:** the container name is the overlap lock. A leftover container would block the new run with "name already in use".
- **Expected:** no output.

**8.2 Start the scrape in the background**
```bash
docker/cron.sh scrape &
```
- **What:** runs exactly what cron runs every hour: `run_scrape.sh`, then `enrich_locations.py`. `&` gives you the prompt back.
- **Why:** it tests the real job end to end.
- **Expected:** a job number such as `[1] 12345`. **The terminal shows nothing else**, because all output goes to `logs/cron-scrape.log`.

**8.3 Check the startup lines after 15 seconds**
```bash
sleep 15; grep -E "Compose:|DB connected|DB permissions|DB connection FAILED" logs/cron-scrape.log | tail -3
```
- **Expected:**
  ```
  Compose: docker compose -f docker-compose.yml -f docker-compose.linux.yml
  ✅ DB connected: localhost:5432/company_intel as company_intel_rw (PostgreSQL 16.x)
  ✅ DB permissions OK (CREATE on public, owns scraper tables)
  ```
- **If it fails:** a `❌` line → see [Troubleshooting](#troubleshooting). The run has already stopped, so fix the cause and repeat 8.1–8.3.

**8.4 Follow the progress**
```bash
tail -f logs/cron-scrape.log | grep --line-buffered -v "❌ http\|✅ http"
```
- **What:** follows the log live, without the thousands of per-proxy test lines.
- **Expected stages:**
  1. `Testing proxies...`: only when `proxies.txt` has fewer than 50 entries. Takes about 15 minutes.
  2. `Starting scraper with 8 workers...`
  3. `Total news scraped` / `Database totals`
  4. `... jobs -> ... location rows`
  5. `===== [scrape] end ... rc=0 =====`
- Press `Ctrl+C` to stop **watching**; the job keeps running.
- Alternative: `docker logs -f --tail 50 company-intel-scrape`. `--tail` needs a number.

**8.5 Confirm it finished successfully**
```bash
grep "\[scrape\] end" logs/cron-scrape.log | tail -1
```
- **Expected:** `===== [scrape] end <date> rc=0 =====`.

**8.6 Confirm the data landed**
```bash
psql "$(grep ^DATABASE_URL= .env | cut -d= -f2-)" -c "select (select count(*) from companies) companies, (select count(*) from news) news, (select count(*) from jobs) jobs, (select count(*) from job_locations) locations;"
```
- **Expected:** non-zero counts (e.g. `213 | 8263 | 1721 | 2202`).
- If the frontend uses a different DB user, run **2.8** now.

---

### Step 9 — Install the cron jobs

**9.1 Install**
```bash
./setup_cron.sh
```
- **What:** adds the 5 jobs (scrape, proxies, clean-proxies, backup, vacuum) to **your** crontab, between `# BEGIN/END company-intel-scraper` markers.
- **Why:** this schedules everything. It's safe to run again, because it replaces only its own block.
- **Important:** **no `sudo`**. With `sudo` the jobs go into root's crontab and create root-owned files.

**9.2 Check**
```bash
crontab -l | grep cron.sh
```
- **Expected:** 5 lines:
  ```
  0 * * * * /var/www/html/pythonProjects/company-intel-scraper/docker/cron.sh scrape
  30 */6 * * * .../docker/cron.sh proxies
  0 2 * * 0 .../docker/cron.sh clean-proxies
  0 1 * * * .../docker/cron.sh backup
  0 5 * * 0 .../docker/cron.sh vacuum
  ```

---

### Step 10 — Test backup and vacuum once

**10.1 Backup**
```bash
docker/cron.sh backup && ls -lh backups/
```
- **What:** runs `pg_dump` on the host into `backups/company_intel_YYYYMMDD.sql.gz` and deletes backups older than 30 days.
- **Expected:** a new `.sql.gz` file, a few MB in size.
- **If it fails:** check `tail logs/cron-backup.log`. `server version mismatch` → install `postgresql-client-<server major>`.

**10.2 Vacuum**
```bash
docker/cron.sh vacuum && tail -3 logs/cron-vacuum.log
```
- **What:** runs `VACUUM ANALYZE` to reclaim space and refresh query statistics.
- **Expected:** `VACUUM`, then `[vacuum] end ... rc=0`.

---

### Step 11 — Log rotation

**11.1 Create the logrotate config**
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
```
- **What:** tells the system's daily logrotate run to rotate this repo's logs, keep 7 days and compress the old ones.
- **Why:** `logs/cron-*.log` grows with every run. `copytruncate` is needed because the files are appended to with `>>`.
- Run it from the repo folder: `$(pwd)` fills in the path.

**11.2 Dry-run it**
```bash
sudo logrotate -d /etc/logrotate.d/company-scraper
```
- **Expected:** `considering log .../logs/cron-scrape.log` lines and no `error:` lines.

---

### Step 12 — Keep it running and confirm cron fires

There is **no long-running scraper container**. Cron starts one per job,
and it's removed when the job ends. See [Keeping it running](#keeping-it-running).

**12.1 Make sure all three services start on boot**
```bash
sudo systemctl enable --now docker cron postgresql
systemctl is-enabled docker cron postgresql
```
- **Expected:** `enabled` three times.

**12.2 After the next full hour, confirm cron started the job by itself**
```bash
grep "\[scrape\] start" logs/cron-scrape.log | tail -3
```
- **Expected:** a `start` at `HH:00:0x` that you didn't trigger.

```bash
grep CRON /var/log/syslog | tail -5
```
- **Expected:** `(mohit) CMD (/var/www/html/.../docker/cron.sh scrape)`.

**12.3 (Recommended) Reboot test at a quiet time**
```bash
sudo reboot
```
After logging back in, repeat **12.1** and **8.1–8.3**.

---

### Setup checklist

| # | Check | Command | Pass |
|---|---|---|---|
| 1 | Docker without sudo | `docker ps` | no `permission denied` |
| 2 | Compose version | `docker compose version` | v2.24+ |
| 3 | Services on boot | `systemctl is-enabled docker cron postgresql` | `enabled` ×3 |
| 4 | DB permissions | query in 2.9 | `can_create = t` |
| 5 | `.env` | 4.4 / 4.5 | `0` and `localhost` |
| 6 | Host network | `docker compose config \| grep network_mode` | `host` |
| 7 | DB from container | 5.2 | `DB OK` |
| 8 | First run | 8.3 / 8.5 | ✅ ✅ and `rc=0` |
| 9 | Crons | `crontab -l \| grep cron.sh` | 5 lines |
| 10 | Backup | `ls backups/` | today's `.sql.gz` |
| 11 | Cron fired | 12.2 | `start` at `HH:00` |

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

### Keeping it running

There is **no long-running scraper container**. Cron starts a fresh
container for each job, and `--rm` deletes it when the job ends. Keeping it
running means keeping three host services up, all started on boot:

```
server boots → systemd starts docker, cron, postgresql
            → cron fires on schedule → docker/cron.sh <job> → short-lived container → removed
```

```bash
sudo systemctl enable --now docker cron postgresql   # start now and on every boot
systemctl is-enabled docker cron postgresql          # each: enabled
systemctl is-active  docker cron postgresql          # each: active
crontab -l | grep cron.sh                            # 5 jobs in YOUR crontab (not root's)
```

| If this is down | What happens |
|---|---|
| `docker` | `cron.sh` logs `Cannot use Docker ...` and skips the job |
| `cron` | nothing runs, and no new `[scrape] start` lines appear |
| `postgresql` | `❌ DB connection FAILED ... Connection refused` |

**Reboot test** (once, at a quiet time):

```bash
sudo reboot
# after logging back in:
systemctl is-active docker cron postgresql
docker/cron.sh scrape &
sleep 15; grep -E "DB connected|DB permissions" logs/cron-scrape.log | tail -2
```

**Disk space.** A full disk is the most common way this stops:

```bash
du -sh logs backups          # logs rotate after 7 days (Step 11), backups are deleted after 30
docker image prune -f        # remove old images left behind by each `docker compose build`
```

### Where's the container? Seeing past runs

`docker ps` only shows the scraper **while a job is running**: about 6–20
minutes after each `HH:00`. At other times it's normal for `docker ps` to
list only other apps (e.g. `company-intel-app-1`, `n8n`). The container
is removed after each run, so `docker ps -a` doesn't show it either.
**Nothing is lost**, because all its output is in `logs/cron-<job>.log`.

```bash
date
grep -n "\[scrape\] \(start\|end\)" logs/cron-scrape.log | tail -6    # run history
```

| You see | Meaning |
|---|---|
| a `start` at the last `HH:00`, followed by `end ... rc=0` | Working. The container ran and was removed. |
| a `start` at the last `HH:00` with no `end` yet | Running now; `docker ps` shows `company-intel-scrape` |
| the last `start` is more than 1 hour old | Cron isn't firing: `crontab -l \| grep cron.sh`, `systemctl is-active cron` |
| `end ... rc=1` | Failed; read the `❌` line just above it |

Full output of the last run (what `docker logs` would have shown):

```bash
sed -n "$(grep -n '\[scrape\] start' logs/cron-scrape.log | tail -1 | cut -d: -f1),\$p" logs/cron-scrape.log | grep -v "❌ http\|✅ http"
```

Docker's own record of the containers it started and removed:

```bash
docker events --since 24h --until now --filter container=company-intel-scrape --filter event=start --filter event=die
```

Watch a run while it's happening (just after `HH:00`):

```bash
docker ps --format '{{.Names}}  {{.Status}}' | grep company-intel-
docker logs -f --tail 50 company-intel-scrape
```

Containers aren't kept on purpose. The fixed name `company-intel-scrape` is
the overlap lock, so a leftover stopped container would block every later run
with "name already in use".

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
| 18 | `docker ps` shows no scraper container / "the container is removed after execution" | Normal: each job's container exists only while it runs and is removed by `--rm` | Check run history in `logs/cron-scrape.log`, see [Where's the container?](#wheres-the-container-seeing-past-runs) |

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
