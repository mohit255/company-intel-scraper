#!/bin/zsh
# Full automatic scrape: news + jobs + products for every company,
# then rebuild the country/city mappings. Run by launchd (see
# ~/Library/LaunchAgents/com.companyintel.scraper.plist) every hour.
set -e
cd "$(dirname "$0")"

# Load config from .env (if present); real env vars take precedence
[ -f .env ] && set -a && source .env && set +a

echo "=== scrape started $(date) ==="
./.venv/bin/python company_scraper.py \
  ${SCRAPER_WORKERS:+--workers "$SCRAPER_WORKERS"} \
  ${SCRAPER_NEWS_LIMIT:+--news-limit "$SCRAPER_NEWS_LIMIT"} \
  ${SCRAPER_JOBS_LIMIT:+--jobs-limit "$SCRAPER_JOBS_LIMIT"} \
  ${SCRAPER_DELAY:+--delay "$SCRAPER_DELAY"} \
  ${SCRAPER_TIMEOUT:+--timeout "$SCRAPER_TIMEOUT"} \
  ${SCRAPER_FIELD:+--field "$SCRAPER_FIELD"} \
  ${SCRAPER_ONLY:+--only "$SCRAPER_ONLY"}
./.venv/bin/python enrich_locations.py
echo "=== scrape finished $(date) ==="