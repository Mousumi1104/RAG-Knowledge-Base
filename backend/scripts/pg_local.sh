#!/usr/bin/env bash
set -euo pipefail

# ----------------------------------------------------------------------------
# pg_local.sh — self-contained userspace PostgreSQL 16 + pgvector (no sudo)
# ----------------------------------------------------------------------------

log() { echo "[pg_local] $*"; }
err() { echo "[pg_local] ERROR: $*" >&2; }

# --- Resolve script dir robustly (handle symlinks) --------------------------
resolve_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [ -h "$src" ]; do
    local dir
    dir="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd
}

SCRIPT_DIR="$(resolve_script_dir)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

# --- Paths & config ---------------------------------------------------------
# NOTE: PostgreSQL requires PGDATA to have 0700/0750 permissions, which Windows
# drvfs mounts (e.g. /mnt/d under WSL) cannot provide. The repo lives on such a
# mount, so the cluster + binaries are kept on a Linux-native path under $HOME
# (overridable via RAG_PG_HOME). This dir is disposable; it holds no source.
PG_HOME="${RAG_PG_HOME:-$HOME/.cache/rag-knowledge-base/pg}"
PG_WORK="$PG_HOME/work"
PG_ROOT="$PG_HOME/root"
PGDATA="$PG_HOME/data"
PG_LOG="$PG_HOME/server.log"
PG_RUN="$PG_HOME/run"   # writable unix socket dir (default /var/run/postgresql is unavailable)
PGBIN="$PG_ROOT/usr/lib/postgresql/16/bin"

PG_PORT=54329
PG_HOST=127.0.0.1
PG_USER=raguser
PG_DB=ragdb
PG_TEST_DB=ragdb_test

export LD_LIBRARY_PATH="$PG_ROOT/usr/lib/x86_64-linux-gnu:$PG_ROOT/usr/lib:${LD_LIBRARY_PATH:-}"

# psql/createdb helpers bound to our cluster
psql_postgres() {
  "$PGBIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres "$@"
}

# ----------------------------------------------------------------------------
# install — download & extract debs, make pgvector visible
# ----------------------------------------------------------------------------
do_install() {
  if [ -x "$PGBIN/postgres" ]; then
    log "install: postgres binary already present, skipping download"
  else
    log "install: downloading PostgreSQL 16 + pgvector debs (no sudo)"
    mkdir -p "$PG_WORK" "$PG_ROOT"
    (
      cd "$PG_WORK"
      apt-get download postgresql-16 postgresql-client-16 postgresql-16-pgvector libpq5
    )
    log "install: extracting debs into $PG_ROOT"
    for d in "$PG_WORK"/*.deb; do
      [ -e "$d" ] || continue
      dpkg-deb -x "$d" "$PG_ROOT"
    done
    if [ ! -x "$PGBIN/postgres" ]; then
      err "install: postgres binary not found at $PGBIN/postgres after extraction"
      exit 1
    fi
    log "install: extraction complete"
  fi

  # --- Make pgvector visible to this postgres build -------------------------
  # postgres uses its compiled-in pkglibdir/sharedir. Copy pgvector shared
  # objects + extension control/sql into the locations pg_config reports.
  local src_lib="$PG_ROOT/usr/lib/postgresql/16/lib"
  local src_ext="$PG_ROOT/usr/share/postgresql/16/extension"

  # Sibling lib dir of the binaries (validated fallback location).
  mkdir -p "$PGBIN/../lib" 2>/dev/null || true
  cp "$src_lib/"*.so "$PGBIN/../lib/" 2>/dev/null || true

  # Authoritative locations as reported by pg_config.
  if [ -x "$PGBIN/pg_config" ]; then
    local pkglibdir sharedir
    pkglibdir="$("$PGBIN/pg_config" --pkglibdir 2>/dev/null || true)"
    sharedir="$("$PGBIN/pg_config" --sharedir 2>/dev/null || true)"

    if [ -n "$pkglibdir" ]; then
      mkdir -p "$pkglibdir" 2>/dev/null || true
      cp "$src_lib/"*.so "$pkglibdir/" 2>/dev/null || true
    fi
    if [ -n "$sharedir" ]; then
      mkdir -p "$sharedir/extension" 2>/dev/null || true
      cp "$src_ext/"vector* "$sharedir/extension/" 2>/dev/null || true
    fi
    log "install: pgvector staged (pkglibdir=$pkglibdir, sharedir=$sharedir)"
  else
    log "install: pg_config not found; relied on sibling lib copy for pgvector"
  fi
}

# ----------------------------------------------------------------------------
# initdb — create the data cluster
# ----------------------------------------------------------------------------
do_initdb() {
  if [ -f "$PGDATA/PG_VERSION" ]; then
    log "initdb: cluster already initialized at $PGDATA, skipping"
    return 0
  fi
  log "initdb: creating cluster at $PGDATA (owner=$PG_USER)"
  mkdir -p "$PGDATA"
  "$PGBIN/initdb" -D "$PGDATA" -U "$PG_USER" --auth-local=trust --auth-host=trust -E UTF8
  log "initdb: done"
}

# ----------------------------------------------------------------------------
# start / running helpers
# ----------------------------------------------------------------------------
is_running() {
  "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1
}

do_start() {
  if is_running; then
    log "start: server already running"
    return 0
  fi
  log "start: launching server on $PG_HOST:$PG_PORT"
  mkdir -p "$PG_RUN"
  # NOTE: listen_addresses=* binds all interfaces; this is gated by pg_hba.conf,
  # which keeps loopback as 'trust' (app, no password) and requires scram for
  # external clients. ALTER SYSTEM (postgresql.auto.conf) overrides this -c flag
  # when 'expose'/'unexpose' have been run; this is just a sane default.
  if ! "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PG_LOG" \
      -o "-c listen_addresses=* -p $PG_PORT -c unix_socket_directories=$PG_RUN" -w start; then
    err "start: failed to start server; last 40 lines of $PG_LOG:"
    tail -n 40 "$PG_LOG" >&2 || true
    exit 1
  fi
  log "start: server is ready"
}

do_stop() {
  if is_running; then
    log "stop: stopping server"
    "$PGBIN/pg_ctl" -D "$PGDATA" -w stop
    log "stop: stopped"
  else
    log "stop: server not running, nothing to do"
  fi
}

do_restart() {
  do_stop
  do_start
}

# ----------------------------------------------------------------------------
# databases + extension (idempotent)
# ----------------------------------------------------------------------------
db_exists() {
  local name="$1" out
  out="$(psql_postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$name'" 2>/dev/null || true)"
  [ "$out" = "1" ]
}

create_db_if_absent() {
  local name="$1"
  if db_exists "$name"; then
    log "db: '$name' already exists"
  else
    log "db: creating '$name'"
    "$PGBIN/createdb" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$name"
  fi
}

enable_vector() {
  local name="$1"
  log "vector: ensuring extension on '$name'"
  "$PGBIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$name" \
    -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;"
}

do_create_dbs() {
  create_db_if_absent "$PG_DB"
  create_db_if_absent "$PG_TEST_DB"
  enable_vector "$PG_DB"
  enable_vector "$PG_TEST_DB"
}

# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------
cmd_up() {
  do_install
  do_initdb
  do_start
  do_create_dbs
  log "up: ready on $PG_HOST:$PG_PORT ($PG_DB, $PG_TEST_DB, vector enabled)"
}

cmd_stop() {
  do_stop
}

cmd_status() {
  if is_running; then
    log "status: running on $PG_HOST:$PG_PORT"
    "$PGBIN/pg_ctl" -D "$PGDATA" status || true
  else
    log "status: stopped (configured port $PG_PORT)"
  fi
}

cmd_psql() {
  exec "$PGBIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" "$@"
}

cmd_url() {
  echo "postgresql+psycopg://$PG_USER@$PG_HOST:$PG_PORT/$PG_DB"
}

cmd_reset() {
  log "reset: dropping and recreating $PG_DB and $PG_TEST_DB"
  do_stop
  do_start
  for name in "$PG_DB" "$PG_TEST_DB"; do
    if db_exists "$name"; then
      log "reset: dropping '$name'"
      "$PGBIN/dropdb" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$name"
    fi
    log "reset: creating '$name'"
    "$PGBIN/createdb" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$name"
    enable_vector "$name"
  done
  log "reset: done ($PG_DB, $PG_TEST_DB recreated, vector enabled)"
}

# ----------------------------------------------------------------------------
# expose / unexpose — allow (or revoke) external pgAdmin access (idempotent)
# ----------------------------------------------------------------------------
HBA_EXTERNAL_RULE="host all all 0.0.0.0/0 scram-sha-256"

cmd_expose() {
  # Ensure the server is up (do_start is a no-op if already running).
  do_start

  # Set a password on the role via loopback trust (no password needed locally).
  log "expose: setting password on role '$PG_USER' (via loopback trust)"
  "$PGBIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres \
    -v ON_ERROR_STOP=1 -c "ALTER ROLE $PG_USER WITH PASSWORD 'ragpass';"

  # Ensure the external scram host rule exists (append only if absent).
  # Do NOT touch the loopback trust lines — the app depends on them.
  if grep -qF "$HBA_EXTERNAL_RULE" "$PGDATA/pg_hba.conf"; then
    log "expose: scram host rule already present in pg_hba.conf"
  else
    log "expose: appending scram host rule to pg_hba.conf"
    printf '%s\n' "$HBA_EXTERNAL_RULE" >> "$PGDATA/pg_hba.conf"
  fi

  # Persist listen_addresses=* via ALTER SYSTEM (survives restarts, overrides -c).
  log "expose: setting listen_addresses='*' via ALTER SYSTEM"
  "$PGBIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres \
    -v ON_ERROR_STOP=1 -c "ALTER SYSTEM SET listen_addresses = '*';"

  # listen_addresses change requires a restart; restart once (also picks up hba).
  do_restart

  log "expose: listening on all interfaces; role has password; scram host rule present"
}

cmd_unexpose() {
  # Ensure the server is up so we can run ALTER SYSTEM.
  do_start

  # Remove the external scram host rule if present (keep loopback trust lines).
  if grep -qF "$HBA_EXTERNAL_RULE" "$PGDATA/pg_hba.conf"; then
    log "unexpose: removing scram host rule from pg_hba.conf"
    local tmp
    tmp="$(mktemp)"
    grep -vF "$HBA_EXTERNAL_RULE" "$PGDATA/pg_hba.conf" > "$tmp"
    cat "$tmp" > "$PGDATA/pg_hba.conf"
    rm -f "$tmp"
  else
    log "unexpose: scram host rule not present, nothing to remove"
  fi

  # Revert listen_addresses to loopback only via ALTER SYSTEM (survives restarts).
  log "unexpose: setting listen_addresses='127.0.0.1' via ALTER SYSTEM"
  "$PGBIN/psql" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres \
    -v ON_ERROR_STOP=1 -c "ALTER SYSTEM SET listen_addresses = '127.0.0.1';"

  # listen_addresses change requires a restart.
  do_restart

  log "unexpose: listening on loopback only; external scram host rule removed"
}

# ----------------------------------------------------------------------------
# ip — print this WSL host's eth0 IPv4 (for the Windows pgAdmin connection)
# ----------------------------------------------------------------------------
cmd_ip() {
  local addr
  addr="$(ip -4 addr show eth0 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1 || true)"
  if [ -z "$addr" ]; then
    addr="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  echo "$addr"
}

usage() {
  cat >&2 <<EOF
[pg_local] usage: $(basename "$0") <command>

Commands:
  up       install (if needed), initdb, start, create dbs, enable vector
  stop     stop the server if running
  status   show running/stopped state and port
  psql     open psql against $PG_DB (extra args passed through)
  url      print the SQLAlchemy/psycopg connection URL
  reset    drop+recreate $PG_DB and $PG_TEST_DB, re-enable vector
  expose   allow external clients (e.g. Windows pgAdmin): listen on all
           interfaces, set role password, add scram host rule, restart
  unexpose revert to loopback-only: remove scram host rule, restart
  ip       print this WSL host's eth0 IPv4 (for the pgAdmin connection)

Cluster: $PG_HOST:$PG_PORT  user=$PG_USER  dbs=$PG_DB,$PG_TEST_DB
Loopback (127.0.0.1) stays 'trust', so the app connects without a password.
External clients (after 'expose') authenticate with scram (password 'ragpass').
EOF
}

main() {
  local cmd="${1:-}"
  [ $# -gt 0 ] && shift || true
  case "$cmd" in
    up)     cmd_up "$@" ;;
    stop)   cmd_stop "$@" ;;
    status) cmd_status "$@" ;;
    psql)   cmd_psql "$@" ;;
    url)    cmd_url "$@" ;;
    reset)    cmd_reset "$@" ;;
    expose)   cmd_expose "$@" ;;
    unexpose) cmd_unexpose "$@" ;;
    ip)       cmd_ip "$@" ;;
    *)        usage; exit 2 ;;
  esac
}

main "$@"
