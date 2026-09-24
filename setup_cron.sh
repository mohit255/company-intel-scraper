#!/bin/bash
# Install (or refresh) the Docker-based cron jobs for this repo.
# Idempotent: replaces the block between the BEGIN/END markers.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN="$DIR/docker/cron.sh"
BEGIN="# BEGIN company-intel-scraper"
END="# END company-intel-scraper"

chmod +x "$RUN"

BLOCK="$BEGIN
# Hourly scrape + location enrichment
0 * * * * $RUN scrape
# Refresh proxy pool every 6 hours
30 */6 * * * $RUN proxies
# Weekly deep-clean of proxy list (Sun 02:00)
0 2 * * 0 $RUN clean-proxies
# Daily DB backup (01:00), keeps 30 days
0 1 * * * $RUN backup
# Weekly VACUUM ANALYZE (Sun 05:00)
0 5 * * 0 $RUN vacuum
$END"

EXISTING="$(crontab -l 2>/dev/null | sed "/^$BEGIN\$/,/^$END\$/d" || true)"
printf '%s\n%s\n' "$EXISTING" "$BLOCK" | sed '/./,$!d' | crontab -

echo "Installed cron jobs:"
crontab -l
