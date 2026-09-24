#!/bin/bash
# Cron entry point. Scraper/proxy jobs run inside Docker; backup/vacuum run
# with the host's pg tools. All jobs use the local Postgres from .env.
#   docker/cron.sh <job>   job = scrape | proxies | clean-proxies | backup | vacuum
#
# Each job runs in a container with a fixed name, so if the previous run of the
# same job is still going, the new one fails to start instead of overlapping.
set -uo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1

JOB="${1:?usage: docker/cron.sh <scrape|proxies|clean-proxies|backup|vacuum>}"
LOG="logs/cron-${JOB}.log"
mkdir -p logs backups
touch proxies.txt proxies_working.txt proxies_failed.txt

exec >>"$LOG" 2>&1
echo "===== [$JOB] start $(date '+%Y-%m-%d %H:%M:%S') ====="

if ! DOCKER_ERR="$(docker info 2>&1 >/dev/null)"; then
    echo "Cannot use Docker as user $(id -un) - skipping:"
    echo "$DOCKER_ERR" | tail -3
    echo "Hint: 'permission denied' -> sudo usermod -aG docker $(id -un), then re-login;"
    echo "      'Is the docker daemon running?' -> sudo systemctl enable --now docker"
    exit 1
fi

DC="docker compose"
# DB jobs run against the local Postgres with the host's pg_dump/psql
set -a; source .env; set +a

run_job() {
    $DC run --rm --name "company-intel-${JOB}" scraper "$@"
}

case "$JOB" in
    scrape)        run_job bash -c "bash run_scrape.sh && python enrich_locations.py" ;;
    proxies)       run_job python fetch_fresh_proxies.py ;;
    clean-proxies) run_job python test_and_clean_proxies.py ;;
    backup)
        OUT="backups/company_intel_$(date +%Y%m%d).sql.gz"
        pg_dump "$DATABASE_URL" | gzip > "$OUT" \
            && echo "wrote $OUT"
        find backups -name "*.sql.gz" -mtime +30 -delete
        ;;
    vacuum)        psql "$DATABASE_URL" -c "VACUUM ANALYZE;" ;;
    *)             echo "unknown job: $JOB"; exit 2 ;;
esac
RC=$?
echo "===== [$JOB] end $(date '+%Y-%m-%d %H:%M:%S') rc=$RC ====="
exit $RC
