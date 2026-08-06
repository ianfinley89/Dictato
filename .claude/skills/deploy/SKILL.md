---
name: deploy
description: Ship Dictato to the home-PC host and PROVE the new build is serving. Use after committing changes that must reach production, or whenever asked to deploy, restart, or verify what is live.
---

# Deploying Dictato

Production is this machine: a scheduled task (`Dictato`) runs uvicorn on port
8000, exposed by a Cloudflare tunnel at https://dictato.levelup-ai.com.

## Why this needs a procedure

`Stop-ScheduledTask` kills the wrapper but **not** the uvicorn process it
spawned. The new instance then fails to bind port 8000, exits, and the old build
keeps serving — while still answering HTTP 200, still serving fresh static files
(read from disk per request), and still applying DB migrations. That combination
hid a ten-day-old build in production.

So "the site is up" and "the site loads my new CSS" both prove nothing about the
Python. Only the commit does.

## The procedure

```powershell
Stop-ScheduledTask -TaskName "Dictato"; Start-Sleep -Seconds 2
$p = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($p) { Stop-Process -Id $p.OwningProcess -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }
Start-ScheduledTask -TaskName "Dictato"; Start-Sleep -Seconds 15
Invoke-RestMethod https://dictato.levelup-ai.com/api/health | ConvertTo-Json -Compress
"HEAD = $(git rev-parse --short HEAD)"
```

Clearing the port explicitly is the fix for the trap above. `deploy/run-dictato.ps1`
also claims the port before starting; if a process survives with *Access is
denied*, it needs an elevated shell (the task runs S4U).

## Verifying — both halves, not just the first

`/api/health` returns `{ok, commit, started_at}`.

1. **`commit` must equal `git rev-parse --short HEAD`.**
2. **`started_at` must be LATER than the commit's timestamp**
   (`git log -1 --format=%cI`).

Check (2). It is what catches a stale process, and for a while (1) alone could
not: `/api/health` used to shell out to `git rev-parse` *per request*, so it
reported the working tree's HEAD — the very commit you were checking for —
regardless of what Python was loaded. It agreed with you every time. It was
caught by a process started 02:38Z reporting a commit made at 03:20Z. The commit
is now frozen at import (`app/main.py:_current_commit`), but keep checking both:
a frozen value is only as good as the process holding it.

## After deploying

- **Schema or static changes**: confirm the asset actually shipped, e.g.
  `curl -s localhost:8000/static/app.js | grep -c <a symbol you just added>`,
  and check any new column exists (`PRAGMA table_info`).
- **Frontend changes**: bump `CACHE` in `static/sw.js` (`dictato-vNN`) IN THE
  SAME COMMIT, or phones keep the old bundle.
- **Reference data** (`usda_portions`, etc.) does not ship with git — `data/` is
  gitignored. A fresh host needs
  `uv run python scripts/import_usda_reference.py --download`.

## Rules

- Never deploy with a failing suite. `uv run pytest tests/ -q` first.
- Never `git push` or deploy unless asked.
- If the health check disagrees with HEAD, say the deploy FAILED. Do not report
  success and move on — that is the exact failure this file exists to prevent.
