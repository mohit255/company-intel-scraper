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

# Set database URL
export DATABASE_URL="${DATABASE_URL:-postgresql://companyinteluser:Mohit8787@localhost:5432/company_intel}"

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
echo "Database: $DATABASE_URL"
echo "Companies file: $COMPANIES_FILE"
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
