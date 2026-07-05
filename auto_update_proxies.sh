#!/bin/bash
# Auto-update proxies daily
# Add to crontab: 0 0 * * * /path/to/auto_update_proxies.sh

cd /var/www/html/company-intel-scraper

# Activate virtual environment if using
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Run the daily updater
python3 daily_proxy_updater.py >> logs/proxy_updater.log 2>&1

# Check if update was successful
if [ $? -eq 0 ]; then
    echo "$(date): Proxy update successful" >> logs/proxy_updater.log
else
    echo "$(date): Proxy update FAILED" >> logs/proxy_updater.log
fi
