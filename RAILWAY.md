# Railway deployment — Toing Dashboard

## Files to upload to GitHub

Only these (everything else is gitignored or local-only):

```
Procfile
requirements.txt
toing_dashboard.py
templates/dashboard.html
templates/logs.html
templates/hits.html
templates/firebases.html
templates/settings.html
README.md (optional)
```

Do **not** upload `settings.json`, `firebase_links.json`, `stats.json`, `hits/`, `claimed_session_*.json`, `pending_session.json`, `.env`, `toing.apk`, or `tools/`. The `.gitignore` already excludes them.

## Deploy

1. Create a private GitHub repo with the files above.
2. Railway → **New Project → Deploy from GitHub repo**.
3. Railway installs `requirements.txt` and starts with the `Procfile` command `python -u toing_dashboard.py`.
4. Do not add a `runtime.txt` — pinning Python on the current Railway builder can fail with a `mise` attestation error. The platform default works.

## Volume and variables

1. Service → **Settings → Volumes → Add Volume**, mount path `/data` (1 GB is plenty).
2. Service → **Variables**:

| Variable | Value | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `/data` | All runtime state (settings, links, stats, sessions) in the volume |
| `PORT` | leave to Railway | The app binds to the injected port; local fallback is 5004 |

No other variables are required. Proxy, Telegram, referral code, target and instances are all set from the dashboard and saved into the volume.

3. **Settings → Health Check** → path `/health`, timeout 30 s. Keep **Replicas = 1** — multiple replicas would run duplicate workers.

## First run

1. Open the Railway domain.
2. **Firebase** page → paste all Firebase URLs (plain or `URL:` format), Save.
3. **Settings** page → set proxy (if used), TLS verify toggle, Telegram, referral code/campaign, then **Save All**.
4. **Dashboard** → enter how many accounts you need and how many instances → **Start All**.
5. Firebase order is shuffled each run. Inactive databases are skipped. When the cash target is reached, all workers stop and the instance list is cleared.

## Data that persists

In `/data`: `settings.json`, `firebase_links.json`, `stats.json` (daily counters), `hits/` (session JSONs). Nothing is committed to GitHub. Stats reset by IST date at midnight automatically.

## Graceful shutdown

On redeploy/restart the app catches SIGTERM/SIGINT and stops workers before exiting.
