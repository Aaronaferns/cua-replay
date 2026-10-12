#!/usr/bin/env bash
# Reset the target to its known post-seed state (spike S10).
#   reset.sh dump      save the current database as the post-seed dump (run once, right after seed.py)
#   reset.sh [restore] restore that dump and report how long it took (target: 15 s; slower is a warning)
# The dump lives in target/dolibarr/dumps/ (git-ignored: it holds the seeded users' password hashes).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"

now() { date +%s.%N; }

case "${1:-restore}" in
  dump)
    mkdir -p "$DUMP_DIR"
    tmp="$DUMP_FILE.partial"
    started=$(now)
    compose exec -T -e MYSQL_PWD="$DOLIBARR_DB_ROOT_PASSWORD" db \
      mariadb-dump -uroot --single-transaction --add-drop-database --databases dolidb > "$tmp"
    mv "$tmp" "$DUMP_FILE"
    elapsed=$(awk -v a="$started" -v b="$(now)" 'BEGIN { printf "%.2f", b - a }')
    echo "dumped to $DUMP_FILE ($(wc -c < "$DUMP_FILE") bytes) in ${elapsed}s"
    ;;
  restore)
    if [ ! -f "$DUMP_FILE" ]; then
      echo "no dump at $DUMP_FILE: run seed.py, then 'reset.sh dump'" >&2
      exit 3
    fi
    started=$(now)
    # The dump drops and recreates the database, so connect without a default schema.
    # Throwaway test database: skip the per-commit fsync and the constraint checks while loading (the dump is
    # consistent by construction), and create the ~400 tables inside the system tablespace instead of one file each
    # (file creation dominates the load on WSL2).
    { echo "SET GLOBAL innodb_flush_log_at_trx_commit = 2; SET GLOBAL innodb_file_per_table = 0; SET GLOBAL innodb_stats_persistent = 0;"
      echo "SET unique_checks = 0; SET foreign_key_checks = 0; SET autocommit = 0;"
      cat "$DUMP_FILE"
      echo "COMMIT;"; } |
      compose exec -T -e MYSQL_PWD="$DOLIBARR_DB_ROOT_PASSWORD" db mariadb -uroot
    elapsed=$(awk -v a="$started" -v b="$(now)" 'BEGIN { printf "%.2f", b - a }')
    echo "restored $DUMP_FILE ($(wc -c < "$DUMP_FILE") bytes) in ${elapsed}s"
    if awk -v e="$elapsed" 'BEGIN { exit !(e > 15) }'; then
      echo "WARNING: restore took longer than the 15 s target (state is restored; the target is a spike goal, not a rule)" >&2
    fi
    ;;
  *)
    echo "usage: reset.sh [dump|restore]" >&2
    exit 2
    ;;
esac
