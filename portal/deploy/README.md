# Provisioner Portal — dev deployment

One-time setup for `portal-dev.solprovision.com` on the OCI VPS.

**Ports on this box:** 5000 tools prod · 5001 tools dev · **5002 portal dev** · 5003 reserved for portal prod.

The portal shares the `/var/www/sol-provision-tools-dev` checkout with the tools app but runs as its
own systemd service, from its own venv, behind its own nginx server block. Nothing here modifies the
tools app.

## Order matters

Do **Part A on the VPS first**, then merge the PR. That way the first automated deploy comes up green
instead of failing on a missing systemd unit. Part A works before the code exists on the box because
the venv is populated by package name rather than from `portal/requirements.txt`.

---

## Part A — VPS setup (before merging)

### 1. Log directory

```bash
sudo mkdir -p /var/log/solprovision-portal-dev
sudo chown solprovision:solprovision /var/log/solprovision-portal-dev
```

### 2. Session secret

Unit files are world-readable, so the key goes in a root-only env file. Anyone holding this value can
forge a portal session.

```bash
sudo mkdir -p /etc/sol-provision
printf 'PORTAL_SECRET_KEY=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  | sudo tee /etc/sol-provision/portal-dev.env > /dev/null
sudo chmod 600 /etc/sol-provision/portal-dev.env
sudo chown root:root /etc/sol-provision/portal-dev.env
```

### 3. Portal venv

Separate from the tools venv on purpose: the deploy runs `pip install` on every push, and a shared
venv would let a portal dependency bump Flask or Werkzeug under the tools app on its next restart.

```bash
sudo -u solprovision python3 -m venv /var/www/sol-provision-tools-dev/portal/venv
sudo -u solprovision /var/www/sol-provision-tools-dev/portal/venv/bin/pip install --quiet \
  --upgrade pip
sudo -u solprovision /var/www/sol-provision-tools-dev/portal/venv/bin/pip install --quiet \
  'Flask>=3.0' 'firebase-admin>=6.5' 'gunicorn>=21.2'
```

> If `/var/www/sol-provision-tools-dev/portal/` doesn't exist yet, create it first:
> `sudo -u solprovision mkdir -p /var/www/sol-provision-tools-dev/portal`
> The merge will fill it in. `venv/` is gitignored, so `git reset --hard` won't remove it.

### 4. systemd unit

```bash
sudo cp /var/www/sol-provision-tools-dev/portal/deploy/solprovision-portal-dev.service \
        /etc/systemd/system/solprovision-portal-dev.service
sudo systemctl daemon-reload
sudo systemctl enable solprovision-portal-dev      # enable, but do NOT start yet
```

If the repo file isn't on the box yet, paste the unit from
`portal/deploy/solprovision-portal-dev.service` in this repo.

### 5. Let the deploy user restart it

The GitHub Actions deploy runs `sudo systemctl restart` non-interactively, so it needs a NOPASSWD
rule — the same one that already exists for the tools services. Check what's there:

```bash
sudo grep -r systemctl /etc/sudoers.d/ /etc/sudoers 2>/dev/null
```

Add the portal service alongside it. Use `visudo` so a syntax error can't lock you out:

```bash
sudo visudo -f /etc/sudoers.d/solprovision-deploy
```

```
marauder ALL=(ALL) NOPASSWD: /bin/systemctl restart solprovision-portal-dev
```

Restart is the only privileged call the deploy makes — the health checks that follow it
(`systemctl is-active`, `curl /api/health`) need no privileges.

Replace `marauder` with whatever `VPS_USER` is, and check whether systemctl is at `/bin/systemctl` or
`/usr/bin/systemctl` on this box (`command -v systemctl`) — the path in the rule must match exactly.

### 6. nginx

```bash
sudo cp /var/www/sol-provision-tools-dev/portal/deploy/nginx-portal-dev.conf \
        /etc/nginx/sites-available/solprovision-portal-dev
sudo ln -s /etc/nginx/sites-available/solprovision-portal-dev \
           /etc/nginx/sites-enabled/solprovision-portal-dev
sudo nginx -t          # MUST pass before reloading
sudo systemctl reload nginx
```

`nginx -t` before every reload — a bad config plus a reload takes the tools sites down too, since
they share the same nginx.

### 7. TLS

```bash
sudo certbot --nginx -d portal-dev.solprovision.com
```

Certbot rewrites the server block with the `listen 443 ssl` lines and an http→https redirect, exactly
as it did for the tools blocks. It serves the HTTP-01 challenge itself, so the backend does not need
to be running yet.

---

## Part B — ship it

1. Merge the PR into `dev`.
2. `deploy-portal-dev.yml` fires: pulls, installs `portal/requirements.txt` into the portal venv,
   restarts the service, and fails the run if the service doesn't come back or `/api/health`
   doesn't answer.

## Part C — verify

```bash
# On the VPS
sudo systemctl status solprovision-portal-dev --no-pager
curl -s http://127.0.0.1:5002/api/health
```

Expect:

```json
{"ok":true,"env":"dev","firebase_project":"sp-ledger","token_verification":true,
 "client_config":true,"auth_enabled":true,"roster_readable":true}
```

Any `false` in there tells you which half is missing:

| Field | false means |
|---|---|
| `token_verification` | service account missing/unreadable, or its `project_id` doesn't match — check the error at the top of `journalctl -u solprovision-portal-dev` |
| `client_config` | `FIREBASE_API_KEY` was overridden to empty |
| `roster_readable` | can't open `/var/www/sparqy/data/mee6_snapshots.db` — check `MEE6_DB` and that `solprovision` can read it |

Then from a browser: `https://portal-dev.solprovision.com` → login overlay → **Login with Discord**.
Sign-in requires `portal-dev.solprovision.com` in Firebase Console → Authentication → Settings →
Authorized domains.

Remember the dev gate: only **rank 4+** members can sign in to portal-dev, matching tools-dev.

## Logs

```bash
sudo journalctl -u solprovision-portal-dev -f       # startup + Firebase init
sudo tail -f /var/log/solprovision-portal-dev/error.log
```

## Rollback

```bash
sudo systemctl stop solprovision-portal-dev
sudo rm /etc/nginx/sites-enabled/solprovision-portal-dev
sudo nginx -t && sudo systemctl reload nginx
```

The tools sites are untouched by any of this — separate service, separate venv, separate server block.
