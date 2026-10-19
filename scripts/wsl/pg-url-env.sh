#!/bin/bash
# Resolve libpq-compatible fields from DATABASE_URL without eval or printing
# credentials. Explicit PG* / PGPASS_APP overrides are applied by callers.

resolve_pg_url_env() {
  local encoded host_b64 port_b64 user_b64 pass_b64 db_b64
  local sslmode_b64 sslrootcert_b64 sslcert_b64 sslkey_b64 timeout_b64 appname_b64

  encoded=$(python3 - "${DATABASE_URL:-}" <<'PY'
import base64
import sys
from urllib.parse import parse_qs, unquote, urlsplit

def fail():
    print("FATAL: DATABASE_URL cannot be used by the PostgreSQL backup tools; configure PG* variables.", file=sys.stderr)
    raise SystemExit(1)

try:
    url = urlsplit(sys.argv[1])
    if url.scheme.lower() not in {"postgres", "postgresql", "postgresql+asyncpg"} or not url.hostname:
        fail()
    port = url.port
    database = unquote(url.path.lstrip("/"))
    query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
    allowed = {
        "sslmode", "sslrootcert", "sslcert", "sslkey",
        "connect_timeout", "application_name",
    }
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        fail()
    fields = [
        url.hostname,
        "" if port is None else str(port),
        "" if url.username is None else unquote(url.username),
        "" if url.password is None else unquote(url.password),
        database,
        *(query.get(name, [""])[0] for name in (
            "sslmode", "sslrootcert", "sslcert", "sslkey",
            "connect_timeout", "application_name",
        )),
    ]
    if not database or any("\n" in value or "\r" in value for value in fields):
        fail()
    print(":".join(base64.b64encode(value.encode()).decode() for value in fields))
except (ValueError, UnicodeError):
    fail()
PY
  ) || return 1

  IFS=':' read -r host_b64 port_b64 user_b64 pass_b64 db_b64 \
    sslmode_b64 sslrootcert_b64 sslcert_b64 sslkey_b64 timeout_b64 appname_b64 <<< "$encoded"
  _pg_url_decode() { printf '%s' "$1" | base64 --decode; }
  PG_URL_HOST=$(_pg_url_decode "$host_b64")
  PG_URL_PORT=$(_pg_url_decode "$port_b64")
  PG_URL_USER=$(_pg_url_decode "$user_b64")
  PG_URL_PASSWORD=$(_pg_url_decode "$pass_b64")
  PG_URL_DATABASE=$(_pg_url_decode "$db_b64")
  PG_URL_SSLMODE=$(_pg_url_decode "$sslmode_b64")
  PG_URL_SSLROOTCERT=$(_pg_url_decode "$sslrootcert_b64")
  PG_URL_SSLCERT=$(_pg_url_decode "$sslcert_b64")
  PG_URL_SSLKEY=$(_pg_url_decode "$sslkey_b64")
  PG_URL_CONNECT_TIMEOUT=$(_pg_url_decode "$timeout_b64")
  PG_URL_APPLICATION_NAME=$(_pg_url_decode "$appname_b64")
  unset -f _pg_url_decode
}

apply_pg_url_options() {
  [ -z "${PG_URL_SSLMODE:-}" ] || export PGSSLMODE="${PGSSLMODE:-$PG_URL_SSLMODE}"
  [ -z "${PG_URL_SSLROOTCERT:-}" ] || export PGSSLROOTCERT="${PGSSLROOTCERT:-$PG_URL_SSLROOTCERT}"
  [ -z "${PG_URL_SSLCERT:-}" ] || export PGSSLCERT="${PGSSLCERT:-$PG_URL_SSLCERT}"
  [ -z "${PG_URL_SSLKEY:-}" ] || export PGSSLKEY="${PGSSLKEY:-$PG_URL_SSLKEY}"
  [ -z "${PG_URL_CONNECT_TIMEOUT:-}" ] || export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-$PG_URL_CONNECT_TIMEOUT}"
  [ -z "${PG_URL_APPLICATION_NAME:-}" ] || export PGAPPNAME="${PGAPPNAME:-$PG_URL_APPLICATION_NAME}"
}
