#!/bin/sh

# Check the mounted files, then run cron in the foreground; the schedule is in /etc/cron.d/satview.
set -eu

for f in /app/config.yaml /app/.env; do
    if [ ! -f "$f" ]; then
        echo "$f not found: mount it into the container (see docker-compose.yaml)" >&2
        exit 1
    fi
done

echo "Cron schedule:"
grep -E '^[0-9*]' /etc/cron.d/satview

exec cron -f
