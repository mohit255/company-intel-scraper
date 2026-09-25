#!/bin/bash
# Full automatic scrape with proxy support
set -e
cd "$(dirname "$0")"

# Load config from .env (if present)
[ -f .env ] && set -a && source .env && set +a

# Configure Python path
PYTHON="./.venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi

# Database credentials come only from .env / the environment
: "${DATABASE_URL:?DATABASE_URL is not set - add it to .env}"
export DATABASE_URL

# Proxy configuration
PROXY_FILE="${PROXY_FILE:-proxies.txt}"
PROXY_ROTATION="${PROXY_ROTATION:-random}"
MAX_PROXY_FAILURES="${MAX_PROXY_FAILURES:-3}"
PROXY_LIMIT="${PROXY_LIMIT:-200}"

# Scraper configuration
WORKERS="${SCRAPER_WORKERS:-8}"
NEWS_LIMIT="${SCRAPER_NEWS_LIMIT:-15}"
JOBS_LIMIT="${SCRAPER_JOBS_LIMIT:-15}"
PRODUCTS_LIMIT="${SCRAPER_PRODUCTS_LIMIT:-10}"
DELAY="${SCRAPER_DELAY:-0.5}"
TIMEOUT="${SCRAPER_TIMEOUT:-30}"
COMPANIES_FILE="${COMPANIES_FILE:-companies.json}"

echo "=== scrape started $(date) ==="
echo "Working directory: $(pwd)"
echo "Database: ${DATABASE_URL##*@}"
echo "Companies file: $COMPANIES_FILE"
echo ""

# Fail fast if the database is unreachable (before the slow proxy refresh)
$PYTHON - <<'PYEOF' || { echo "=== scrape aborted: database check failed ==="; exit 1; }
import os, sys, psycopg
url = os.environ["DATABASE_URL"]
try:
    with psycopg.connect(url, connect_timeout=10) as conn:
        info = conn.info
        ver = conn.execute("SHOW server_version").fetchone()[0]
        print(f"✅ DB connected: {info.host}:{info.port}/{info.dbname} as {info.user} (PostgreSQL {ver})")
        # db.py runs CREATE TABLE/INDEX and ALTER TABLE on startup: needs CREATE
        # on schema public and ownership of the scraper's tables.
        can_create = conn.execute("SELECT has_schema_privilege('public', 'CREATE')").fetchone()[0]
        not_owned = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename IN ('companies','news','jobs','products','job_locations') "
            "AND tableowner <> current_user ORDER BY 1").fetchall()]
        if not can_create or not_owned:
            if not can_create:
                print(f"❌ DB permissions: {info.user} has no CREATE on schema public")
            if not_owned:
                print(f"❌ DB permissions: {info.user} does not own: {', '.join(not_owned)}")
            print(f"   Fix (as postgres): GRANT USAGE, CREATE ON SCHEMA public TO {info.user}; "
                  f"ALTER TABLE <table> OWNER TO {info.user};  (see README Troubleshooting)")
            sys.exit(1)
        print(f"✅ DB permissions OK (CREATE on public, owns scraper tables)")
except SystemExit:
    raise
except Exception as e:
    print(f"❌ DB connection FAILED: {str(e).strip()}")
    if "pg_hba.conf" in str(e):
        print("   Hint: container is not on the host network - check COMPOSE_FILE in .env "
              "and that DATABASE_URL uses localhost")
    sys.exit(1)
PYEOF
echo ""

# Check and clean proxies if needed
if [ -f "$PROXY_FILE" ]; then
    PROXY_COUNT=$(wc -l < "$PROXY_FILE" | tr -d ' ')
    echo "Current proxy count: $PROXY_COUNT"
    
    # If proxy count is low, fetch new ones
    if [ "$PROXY_COUNT" -lt 50 ]; then
        echo "⚠️ Proxy count is low ($PROXY_COUNT). Fetching fresh proxies..."
        $PYTHON fetch_fresh_proxies.py
    fi
fi

# Build proxy arguments
PROXY_ARGS=""
if [ -f "$PROXY_FILE" ]; then
    PROXY_ARGS="--proxies $PROXY_FILE --proxy-rotation $PROXY_ROTATION --max-proxy-failures $MAX_PROXY_FAILURES"
    PROXY_COUNT=$(wc -l < "$PROXY_FILE" | tr -d ' ')
    echo "Using $PROXY_COUNT proxies from: $PROXY_FILE"
    echo "Proxy rotation: $PROXY_ROTATION"
    echo "Max failures: $MAX_PROXY_FAILURES"
else
    echo "No proxies configured - running without proxy"
fi

echo ""

# Run the scraper
echo "Starting scraper with $WORKERS workers..."
echo "News limit per company: $NEWS_LIMIT"
echo "Jobs limit per company: $JOBS_LIMIT"
echo "Products limit per company: $PRODUCTS_LIMIT"
echo "Delay between requests: ${DELAY}s"
echo "Request timeout: ${TIMEOUT}s"
echo ""

$PYTHON scraper.py \
    --companies "$COMPANIES_FILE" \
    --workers "$WORKERS" \
    --news-limit "$NEWS_LIMIT" \
    --jobs-limit "$JOBS_LIMIT" \
    --products-limit "$PRODUCTS_LIMIT" \
    --delay "$DELAY" \
    --timeout "$TIMEOUT" \
    $PROXY_ARGS

echo ""
echo "=== scrape finished $(date) ==="

# Show proxy statistics
if [ -f "$PROXY_FILE" ]; then
    PROXY_COUNT=$(wc -l < "$PROXY_FILE" | tr -d ' ')
    echo "Proxy pool size: $PROXY_COUNT proxies"
fi

# Show database summary
echo ""
echo "Database Summary:"
$PYTHON -c "
import psycopg
from db import DB_URL

try:
    conn = psycopg.connect(DB_URL)
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM news')
        news = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM jobs')
        jobs = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM products')
        products = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM companies')
        companies = cur.fetchone()[0]
        
        print(f'  Companies: {companies}')
        print(f'  News: {news}')
        print(f'  Jobs: {jobs}')
        print(f'  Products: {products}')
    conn.close()
except Exception as e:
    print(f'  Error getting stats: {e}')
"
