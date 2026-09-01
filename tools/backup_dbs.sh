#!/usr/bin/env bash
#
# Snapshot every SQLite database that holds data we cannot regenerate.
#
# One entry
# per database in DATABASES below beats one crontab line per database plus a
# matching prune line: adding a database is now a one-line change that cannot
# drift out of sync with its own retention rule (which is exactly what happened
# to mee6_snapshots — it had a prune job and no backup job).
#
# Uses sqlite3's .backup, NOT cp. Every app here runs in WAL mode, where a plain
# file copy can catch a database mid-transaction and restore corrupt. .backup
# takes a read lock and folds in the -wal, so this is safe against the LIVE site
# with no service stop.
#
# Install:
#   sudo install -m 755 tools/backup_dbs.sh /usr/local/bin/sp-backup-dbs
# Cron (add alongside the existing ownership/mee6 backup lines):
#   17 */12 * * * /usr/local/bin/sp-backup-dbs >> /var/log/sp_backup.log 2>&1
#
# Exits non-zero if ANY database failed, so cron mails you / the log shows it.

# Deliberately not `set -e`: one unreadable database must not skip the rest.
set -uo pipefail

SRC_DIR="${SP_DB_DIR:-/var/www/sol-provision-tools}"
DEST_ROOT="${SP_BACKUP_DIR:-/var/www/backups}"
KEEP_DAYS="${SP_BACKUP_KEEP_DAYS:-14}"

# name : absolute source path
#
# SCOPED DELIBERATELY. blueprint_ownership, ship_ownership and mee6_snapshots are
# already backed up by their own crontab entries and pruned by their own rules —
# they are absent here on purpose, not by oversight. Listing them would write a
# second set of snapshots into the same directories alongside the existing ones.
# If those crontab lines are ever folded into this script, add them back here and
# delete the lines, so each database has exactly one owner.
#
# NOT dataforge.db — ~200MB and fully regenerable by re-running the extractor
# against Data.p4k. Disk is not the constraint it once was, but there is still no
# reason to snapshot the one database we can rebuild from scratch.
DATABASES=(
  # Portal-era. applications.db holds real recruiting submissions; opord.db and
  # org_status.db hold hand-authored operational content. No other copy exists.
  "applications:${SRC_DIR}/applications.db"
  "opord:${SRC_DIR}/opord.db"
  "org_status:${SRC_DIR}/org_status.db"

  # Member-entered, keyed by discord_id (cargo_planner holds mission_stacks).
  # Losing these means asking members to re-enter their own work.
  "cargo_planner:${SRC_DIR}/cargo_planner.db"

  # Accumulated history. UEX serves current prices only, so the trend series
  # cannot be re-fetched once lost.
  "uex_feed:${SRC_DIR}/uex_feed.db"

  # Rebuildable from the Google Sheet in normal operation — included anyway,
  # because that makes the Sheet a single point of failure. If it is deleted,
  # mangled, or access lapses, this file becomes the last good copy.
  "warehouse_inventory:${WAREHOUSE_DB:-${SRC_DIR}/warehouse_inventory.db}"
)

command -v sqlite3 >/dev/null || { echo "FATAL: sqlite3 not on PATH"; exit 2; }

stamp=$(date -u +%Y-%m-%dT%H%M%SZ)
failed=()
backed_up=0
skipped=0

for entry in "${DATABASES[@]}"; do
    name="${entry%%:*}"
    src="${entry#*:}"

    if [[ ! -f "$src" ]]; then
        # Not an error. A database is created by its writer on first use, so an
        # untouched feature simply has no file yet.
        echo "SKIP  ${name}: no file at ${src}"
        skipped=$((skipped + 1))
        continue
    fi
    if [[ ! -r "$src" ]]; then
        echo "FAIL  ${name}: ${src} is not readable by $(id -un)"
        failed+=("$name")
        continue
    fi

    dest_dir="${DEST_ROOT}/${name}"
    dest="${dest_dir}/${name}_${stamp}.db"
    if ! mkdir -p "$dest_dir"; then
        echo "FAIL  ${name}: cannot create ${dest_dir}"
        failed+=("$name")
        continue
    fi

    if ! sqlite3 "$src" ".backup '${dest}'" 2>&1; then
        echo "FAIL  ${name}: .backup failed"
        rm -f "$dest"
        failed+=("$name")
        continue
    fi

    # Verify what we just wrote, rather than discovering at restore time that
    # 14 days of snapshots are all unusable. Cheap on databases this size.
    check=$(sqlite3 "$dest" 'PRAGMA integrity_check;' 2>&1)
    if [[ "$check" != "ok" ]]; then
        echo "FAIL  ${name}: integrity_check said: ${check}"
        rm -f "$dest"          # a corrupt backup is worse than none: it hides
        failed+=("$name")      # the absence of a good one
        continue
    fi

    size=$(du -h "$dest" | cut -f1)
    echo "OK    ${name}: ${size} -> ${dest}"
    backed_up=$((backed_up + 1))

    # Prune this database's own history. Scoped to the pattern we write, so a
    # stray file in the directory is never deleted by us.
    find "$dest_dir" -maxdepth 1 -name "${name}_*.db" -type f \
         -mtime "+${KEEP_DAYS}" -delete 2>/dev/null
done

echo "---- $(date -u +%Y-%m-%dT%H:%M:%SZ)  ok=${backed_up} skipped=${skipped} failed=${#failed[@]} (keep ${KEEP_DAYS}d)"

if (( ${#failed[@]} > 0 )); then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
