#!/usr/bin/env bash
set -euo pipefail

systemctl enable redis-server postgresql >/dev/null

# Expose Redis only through the WSL private interface with password auth. Windows
# localhost forwarding is intermittent in WSL NAT mode, so the launcher uses the
# current WSL address instead.
REDIS_CONF="/etc/redis/redis.conf"
sed -ri 's/^bind .*/bind 0.0.0.0 ::1/' "$REDIS_CONF"
if grep -Eq '^# *requirepass ' "$REDIS_CONF"; then
  sed -ri 's/^# *requirepass .*/requirepass dramaflow/' "$REDIS_CONF"
elif grep -Eq '^requirepass ' "$REDIS_CONF"; then
  sed -ri 's/^requirepass .*/requirepass dramaflow/' "$REDIS_CONF"
else
  printf '\nrequirepass dramaflow\n' >> "$REDIS_CONF"
fi
systemctl restart redis-server postgresql

runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
  -c "ALTER USER postgres WITH PASSWORD 'postgres';"

if ! runuser -u postgres -- psql -tAc \
  "SELECT 1 FROM pg_database WHERE datname='dramaflow'" | grep -q 1; then
  runuser -u postgres -- createdb dramaflow
fi

# WSL2 NAT does not always forward PostgreSQL's loopback listener to Windows.
# Listen on the WSL private interface and allow password auth only from its RFC1918 NAT range.
runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
  -c "ALTER SYSTEM SET listen_addresses = '*';"
PG_HBA="/etc/postgresql/16/main/pg_hba.conf"
HBA_RULE="host all all 172.16.0.0/12 scram-sha-256 # dramaflow-wsl"
grep -Fq "# dramaflow-wsl" "$PG_HBA" || printf '%s\n' "$HBA_RULE" >> "$PG_HBA"
systemctl restart postgresql

redis-cli -a dramaflow --no-auth-warning ping
runuser -u postgres -- psql -d dramaflow -tAc "SELECT current_database();"
pg_isready -h 127.0.0.1 -p 5432 -U postgres -d dramaflow
