#!/bin/bash
# Inside the container "localhost" is the container itself, so point a
# localhost DATABASE_URL at the Docker host's Postgres instead.
if [ -n "${DB_HOST_OVERRIDE:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
    export DATABASE_URL="$(printf '%s' "$DATABASE_URL" | sed -E "s#@(localhost|127\.0\.0\.1)([:/])#@${DB_HOST_OVERRIDE}\2#")"
fi
exec "$@"
