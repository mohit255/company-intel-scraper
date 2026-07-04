#!/bin/zsh
# Full automatic scrape: news + jobs + products for every company,
# then rebuild the country/city mappings. Run by launchd (see
# ~/Library/LaunchAgents/com.companyintel.scraper.plist) every 6 hours.
set -e
cd /Users/mohitchack/Desktop/Work/code/web-scraper

echo "=== scrape started $(date) ==="
./.venv/bin/python company_scraper.py
./.venv/bin/python enrich_locations.py
echo "=== scrape finished $(date) ==="
