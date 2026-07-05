#!/bin/bash
# Setup daily proxy updates via cron

echo "Setting up daily proxy updates..."

# Make scripts executable
chmod +x daily_proxy_updater.py auto_update_proxies.sh

# Add to crontab (runs daily at midnight)
(crontab -l 2>/dev/null; echo "0 0 * * * /var/www/html/company-intel-scraper/auto_update_proxies.sh") | crontab -

# Also run every 6 hours for fresh proxies
(crontab -l 2>/dev/null; echo "0 */6 * * * /var/www/html/company-intel-scraper/auto_update_proxies.sh") | crontab -

echo "✅ Daily proxy updates configured!"
echo "   - Runs at midnight every day"
echo "   - Also runs every 6 hours"
echo "   - Logs to: logs/proxy_updater.log"
