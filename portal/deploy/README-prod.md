# Provisioner Portal — production deployment

One-time setup for `portal.solprovision.com` on the OCI VPS.

**Ports on this box:** 5000 tools prod · 5001 tools dev · 5002 portal dev · **5003 portal prod**.

This mirrors `README.md` (the dev runbook) with every path pointed at the production checkout
`/var/www/sol-provision-tools` and the bind port moved to 5003. Read that file first if anything
here is unclear — the reasoning behind each step is spelled out there and not repeated.

> **Before you start**, read [Databases](#databases) at the bottom. Two of the three databases hold
> content that was authored somewhere else and will not reappear on its own.

---

## Order matters

Do **Part A on the VPS first**, then merge to `main`. The first automated deploy then comes up green
instead of failing on a missing systemd unit. Part A works before the code exists on the box because
the venv is populated by package name rather than from `portal/requirements.txt`.

---

## Part A — VPS setup (before merging to main)

### 1. Log directory

```bash
sudo mkdir -p /var/log/solprovision-portal
sudo chown solprovision:solprovision /var/log/solprovision-portal
```

### 2. Session secret

Generate a **fresh** key. Do not reuse `portal-dev.env`'s — a dev-box leak would then forge
production sessions.

```bash
sudo mkdir -p /etc/sol-provision
printf 'PORTAL_SECRET_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  | sudo tee /etc/sol-provision/portal.env > /dev/null
sudo chmod 600 /etc/sol-provision/portal.env
sudo chown root:root /etc/sol-provision/portal.env
```

### 3. Portal venv

```bash
sudo -u solprovision mkdir -p /var/www/sol-provision-tools/portal
sudo -u solprovision python3 -m venv /var/www/sol-provision-tools/portal/venv
sudo -u solprovision /var/www/sol-provision-tools/portal/venv/bin/pip install --quiet --upgrade pip
sudo -u solprovision /var/www/sol-provision-tools/portal/venv/bin/pip install --quiet \
  'Flask>=3.0' 'firebase-admin>=6.5' 'gunicorn>=21.2'
```

`venv/` is gitignored, so the deploy's `git reset --hard` won't remove it.

### 4. Firebase service account

The unit points `FIREBASE_SERVICE_ACCOUNT` at the **tools app's existing** credentials file. Confirm
it is there — without it the portal starts but every login fails with `AUTH UNAVAILABLE`:

```bash
sudo -u solprovision test -r /var/www/sol-provision-tools/app/firebase-service-account.json \
  && echo "service account OK" || echo "MISSING — copy it from the dev checkout"
```

### 5. Confirm the dataforge.db path

The header shows which game patch the org's data reflects, read from `dataforge.db`'s
`patch_history` — the same source the tools app uses. The portal opens it **read-only**; the
extractor owns it.

The unit ships with `/var/www/sol-provision-tools/dataforge.db`, confirmed against the live tools
service. Re-confirm it still matches before copying the unit:

```bash
systemctl show solprovision -p Environment | tr ' ' '\n' | grep -i dataforge
```

Expected:

```
Environment=DATAFORGE_DB=/var/www/sol-provision-tools/dataforge.db
```

If it differs, edit `Environment="DATAFORGE_DB=..."` in
`portal/deploy/solprovision-portal.service` to match before the next step. If it prints nothing, the
tools app is on its relative default — resolve that from `systemctl show solprovision -p
WorkingDirectory`.

Note this is the **checkout root**, not the app's `../../shared/data/` relative default. Getting it
wrong isn't fatal: the portal logs `patch version unavailable` and omits the line — but the header
quietly loses the patch version, which is easy to miss.

### 6. systemd unit

```bash
sudo cp /var/www/sol-provision-tools/portal/deploy/solprovision-portal.service \
        /etc/systemd/system/solprovision-portal.service
sudo systemctl daemon-reload
sudo systemctl enable solprovision-portal      # enable, but do NOT start yet
```

If the repo file isn't on the box yet, paste the unit from `portal/deploy/solprovision-portal.service`.

### 7. Let the deploy user restart it

```bash
sudo visudo -f /etc/sudoers.d/solprovision-deploy
```

Add, alongside the dev rule:

```
solprovision ALL=(ALL) NOPASSWD: /bin/systemctl restart solprovision-portal
```

**The user is `solprovision`, not your interactive login** (`marauder`). **Use `/bin/systemctl`**, not
`/usr/bin/systemctl` — `/bin` is a symlink here and the proven-working rule at `/etc/sudoers:58`
spells it `/bin`. Getting either wrong fails the deploy with `sudo: a password is required`; the pull
and pip install succeed and only the restart dies, leaving the old code running.

Verify:

```bash
sudo -l -U solprovision | grep portal
```

### 8. nginx

```bash
sudo cp /var/www/sol-provision-tools/portal/deploy/nginx-portal.conf \
        /etc/nginx/sites-available/solprovision-portal
sudo ln -s /etc/nginx/sites-available/solprovision-portal \
           /etc/nginx/sites-enabled/solprovision-portal
sudo nginx -t          # MUST pass before reloading
sudo systemctl reload nginx
```

`nginx -t` before every reload — a bad config plus a reload takes tools prod down too, since they
share one nginx.

### 9. TLS

DNS for `portal.solprovision.com` must already resolve to this box, or certbot's HTTP-01 challenge
fails.

```bash
dig +short portal.solprovision.com          # must return the VPS IP
sudo certbot --nginx -d portal.solprovision.com
```

Certbot rewrites the server block in place, adding `listen 443 ssl` and the http→https redirect.

### 10. Firebase authorized domain

In the Firebase console for **`sp-ledger`** → Authentication → Settings → Authorized domains, add
`portal.solprovision.com`. Without it the Discord popup opens and immediately closes with
`auth/unauthorized-domain`. This is the same single project dev uses; there is no separate prod
project.

### 11. New-application tripwire (VoxBot)

When `/api/join` stores a new application, the portal fires an empty POST to VoxBot, which @here-pings
the Acquisitions channel. The unit sets it:

```
Environment="JOIN_WEBHOOK_URL=http://159.89.34.140:8001/api/application"
```

**Prod only.** The dev unit deliberately leaves it unset, which disables the call — otherwise every
test submission on portal-dev pings the channel. Unsetting it is also the kill switch if VoxBot is
noisy or Jenner's box moves: no code change, just edit the unit and restart.

The body is empty by design. No applicant data leaves the VPS, which is what makes plain HTTP to a
bare IP acceptable here. If VoxBot ever wants the actual application contents, that is a different
conversation — it would need HTTPS and a shared secret.

Confirm the VPS can reach it before relying on it:

```bash
curl -sS -m 5 -o /dev/null -w 'HTTP %{http_code}\n' \
  -X POST -H 'Content-Type: application/json' -d '{}' \
  http://159.89.34.140:8001/api/application
```

A non-2xx or a timeout means outbound egress or his listener, not our code — the portal logs the
failure and carries on either way. Watch it live:

```bash
sudo journalctl -u solprovision-portal -f | grep 'join webhook'
```

---

## Part B — merge to main

Pushing to `main` fires `.github/workflows/deploy-portal.yml`, which pulls, installs
`portal/requirements.txt` into the portal venv, restarts the service, and health-checks
`http://127.0.0.1:5003/api/health`.

`deploy.yml` (tools prod) and `deploy-portal.yml` share the concurrency group `vps-deploy-prod`, so a
push touching both `app/` and `portal/` runs them one after the other instead of racing on
`.git/index.lock` in the shared checkout.

Start the service by hand for the first boot, then let the workflow own it:

```bash
sudo systemctl start solprovision-portal
systemctl is-active solprovision-portal
curl -fsS http://127.0.0.1:5003/api/health
```

---

## Part C — verify the gate

The only page reachable without a Discord session is `/join`. Check from a machine with no cookie:

```bash
curl -s -o /dev/null -w '%{http_code}  /join\n'          https://portal.solprovision.com/join
curl -s -o /dev/null -w '%{http_code}  /\n'              https://portal.solprovision.com/
curl -s -o /dev/null -w '%{http_code}  /mission-board\n' https://portal.solprovision.com/mission-board
```

Expected: `/join` 200, `/` 200, `/mission-board` **302**.

`/` returning 200 is correct — it serves the login overlay. What matters is that it carries no member
data. Confirm the readiness matrix isn't in the anonymous HTML:

```bash
curl -s https://portal.solprovision.com/ | grep -c 'div-card'      # must be 0
curl -s https://portal.solprovision.com/ | grep -c 'div-posture'   # must be 0
curl -s https://portal.solprovision.com/ | grep -c 'authOverlay'   # must be >= 1
```

---

## Databases

The portal touches three SQLite files. All three live at the checkout root
(`/var/www/sol-provision-tools/`) and all three are gitignored, so `git reset --hard` leaves them
alone. The unit file names each one explicitly rather than relying on the `REPO_ROOT` default, so a
future move of the checkout fails loudly instead of silently creating empty databases.

| File | Writer | Reader | Ships with the deploy? |
|---|---|---|---|
| `org_status.db` | tools (HQ Admin) | portal | No — created on first HQ write |
| `opord.db` | tools (OpOrd editor) | portal | No — created on first OpOrd save |
| `applications.db` | **portal** (`/join`) | tools (HQ review) | No — created on first application |

Each is created and migrated by its **writer**. The portal opens the two it doesn't own with
`mode=ro`, which cannot create a file — so before the first write exists, the portal renders an empty
readiness matrix and the "Next OpOrd — In Work" placeholder rather than erroring. That is a working
cold start, not a broken one.

### What must actually be copied

Nothing is required for the site to *run*. Two files hold content that was authored elsewhere and
will not reappear on its own:

**`applications.db` — copy from the workstation. 38 historical intake rows** imported from
`Sol Provision Membership Intake.xlsx`. That import ran locally, so prod is the only place these
records are missing. Stop the portal first so no `/join` submission is mid-write:

```powershell
# from C:\Projects\sol-provision-tools on Windows
scp applications.db solprovision:/tmp/applications.db
```

```bash
# on the VPS
sudo systemctl stop solprovision-portal
sudo -u solprovision cp /tmp/applications.db /var/www/sol-provision-tools/applications.db
rm /tmp/applications.db
sudo systemctl start solprovision-portal
```

Migration 2 put `legacy_key` under a **partial** unique index (`WHERE legacy_key IS NOT NULL`), so
re-running the import is idempotent for the historical rows while web submissions (NULL key) never
collide. Copying the file is still the simpler path — it avoids installing `openpyxl` on the VPS and
putting the spreadsheet there even temporarily.

**`opord.db` — copy dev → prod on the VPS.** The OpOrds were authored through tools-dev, so they
exist only in the dev checkout.

Two different services are involved, which is easy to get wrong: the **source** is written by
`solprovision-dev` and the **destination** by `solprovision`. Use `sqlite3 .backup` rather than `cp`
— it takes a read lock and folds in the `-wal` contents, so the source needs no stop at all. A plain
`cp` of a live WAL database silently loses whatever hasn't checkpointed yet.

**Look before overwriting.** If prod already has OpOrds, this replaces them:

```bash
sudo -u solprovision sqlite3 /var/www/sol-provision-tools/opord.db \
  'SELECT id, status, title FROM opords;' 2>/dev/null || echo 'no prod opord.db yet — safe'
```

Snapshot the source hot, no service stop:

```bash
sudo -u solprovision sqlite3 /var/www/sol-provision-tools-dev/opord.db \
  ".backup '/tmp/opord-from-dev.db'"
sqlite3 /tmp/opord-from-dev.db 'SELECT id, status, title FROM opords;'   # sanity-check
```

Then swap it in. Stop both the writer and the reader so neither holds a handle across the move, and
delete the stale `-wal`/`-shm` sidecars — leaving them next to a replaced `.db` is what corrupts it:

```bash
sudo systemctl stop solprovision solprovision-portal
sudo -u solprovision rm -f /var/www/sol-provision-tools/opord.db-wal \
                           /var/www/sol-provision-tools/opord.db-shm
sudo -u solprovision cp /tmp/opord-from-dev.db /var/www/sol-provision-tools/opord.db
sudo systemctl start solprovision solprovision-portal
rm -f /tmp/opord-from-dev.db
```

`cp` as the `solprovision` user, not root — a root-owned database is readable but not writable, so
the tools app would fail on the next OpOrd save. Confirm:

```bash
ls -l /var/www/sol-provision-tools/opord.db     # want solprovision solprovision
```

**`org_status.db` — optional.** Four division rows and four tasking slots. Copying it the same way as
`opord.db` preserves the change log; re-entering it through prod HQ takes under a minute. Either is
fine.

### After copying opord.db

A copied OpOrd keeps its `status`. If one is `posted` and its muster is less than 48h old, the portal
Mission Board shows it as the live brief the moment the portal restarts — the same rule that governs
a natively-posted one. Check what prod will display:

```bash
sudo -u solprovision sqlite3 /var/www/sol-provision-tools/opord.db \
  "SELECT id, status, mission_date, muster_at_utc, title FROM opords ORDER BY id;"
```

If a stale one is posted, demote it from HQ → Admin → Mission Board rather than editing the database
by hand: `post()` also archives the previously posted row, and doing it in SQL skips that.

### Backups

These three files are **not** covered by whatever backs up `dataforge.db` — add them. They are small
(tens of KB) and change rarely, but two of them are the only copy of hand-authored content:
`applications.db` holds real recruiting submissions that exist nowhere else, and `opord.db` holds
authored operation orders. `dataforge.db` is rebuildable from the extractor; these are not.

Back them up with SQLite's own backup API, not `cp`. In WAL mode a plain copy of a live database can
land mid-transaction. `.backup` takes a read lock rather than blocking writers, so this is safe to
run against the live site with no service stop:

```bash
sudo tee /usr/local/bin/backup-portal-dbs.sh > /dev/null <<'SH'
#!/usr/bin/env bash
set -euo pipefail
SRC=/var/www/sol-provision-tools
DEST=/var/backups/solprovision
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$DEST"
for db in org_status opord applications; do
  [ -f "$SRC/$db.db" ] || continue
  sqlite3 "$SRC/$db.db" ".backup '$DEST/$db-$STAMP.db'"
done
find "$DEST" -name '*.db' -mtime +30 -delete
SH
sudo chmod 755 /usr/local/bin/backup-portal-dbs.sh
```

Schedule it daily:

```bash
sudo crontab -e
```

```
17 4 * * *  /usr/local/bin/backup-portal-dbs.sh
```

Verify it actually produced files the next morning — a backup nobody checks is not a backup:

```bash
ls -la /var/backups/solprovision/
```
