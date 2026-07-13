# Hosting Dictato at a permanent URL

Goal: a stable, always-on HTTPS address — **https://dictato.levelup-ai.com** —
served from the home PC, with no open router ports and the home IP hidden.

This uses a **named Cloudflare Tunnel**. Today's setup uses a *quick* tunnel,
which prints a throwaway `*.trycloudflare.com` name that changes on every
restart; a named tunnel bound to your domain is what makes the URL permanent.

> **Reversibility (read this first).** The only thing that moves is the domain's
> **nameservers** — the registration stays at Porkbun, you still own and renew it
> there. Moving nameservers is *not* a domain transfer: no 60-day lock, no fee,
> and you can point them back to Porkbun anytime. Do **not** accept Cloudflare's
> separate offer to *transfer the registration* — you don't need it.

---

## Part 0 — Before you touch anything (safety net)

1. **Export the current DNS.** In Porkbun → `levelup-ai.com` → DNS, screenshot /
   copy every record (A, CNAME, MX, TXT). This is your restore sheet if you ever
   move back.
2. **Email warning.** If `levelup-ai.com` uses **Porkbun email forwarding**, it
   **stops working** once nameservers leave Porkbun — that feature depends on
   Porkbun's nameservers. Fixes (either is free):
   - Recreate it with **Cloudflare Email Routing** after the move, or
   - Only do this on a domain you don't run email on.
   (If no email is set up on this domain, ignore this.)

## Part 1 — Move DNS to Cloudflare (one time, ~15 min + propagation)

1. Create a free account at cloudflare.com → **Add a site** (the current
   dashboard labels this **"Connect your domain"** — the *connect an existing
   domain* path, **not** "Register a new domain", which is the Registrar upsell
   you're avoiding) → enter `levelup-ai.com` → choose the **Free** plan.
2. Cloudflare scans and imports your existing records. **Verify** they all came
   across — especially any MX/TXT (email, domain verification). Add any it missed.
3. Cloudflare shows **two nameservers** (like `dana.ns.cloudflare.com`).
4. In **Porkbun** → `levelup-ai.com` → **Authoritative Nameservers** → replace
   Porkbun's with Cloudflare's two → save.
5. Wait for Cloudflare to flip the domain to **Active** (usually minutes, up to
   24–48h worst case). Registration and renewal stay at Porkbun.

## Part 2 — Create the named tunnel (one time, on the home PC)

Install cloudflared once:

```powershell
winget install --id Cloudflare.cloudflared
```

Then:

```powershell
# 1. Authorize this machine and pick the levelup-ai.com zone (opens a browser).
#    Writes cert.pem to %USERPROFILE%\.cloudflared\
cloudflared tunnel login

# 2. Create the tunnel. Prints a UUID and writes <UUID>.json credentials
#    to %USERPROFILE%\.cloudflared\
cloudflared tunnel create dictato

# 3. Create the config file (see deploy/cloudflared/config.example.yml in this
#    repo). Copy it to %USERPROFILE%\.cloudflared\config.yml and paste in your UUID.

# 4. Create the DNS record automatically (a proxied CNAME → the tunnel).
cloudflared tunnel route dns dictato dictato.levelup-ai.com
```

Smoke test (with uvicorn already running on port 8000):

```powershell
cloudflared tunnel run dictato
```

Visit **https://dictato.levelup-ai.com** — you should see Dictato over HTTPS.
`Ctrl+C` stops it; Part 3 makes it permanent.

## Part 3 — Make it survive reboots (the "always-on" half)

A permanent URL is only useful if the app **and** the tunnel come back after a
reboot — including an unattended one (a Windows Update at 3am), without anyone
logging in. Both run as **Windows scheduled tasks** that start at boot, run as
you with **no stored password** (S4U logon), and restart on crash (the wrapper
scripts loop). Running both as *you* — not as SYSTEM — also avoids the
config-path trap that `cloudflared service install` hits on Windows (the SYSTEM
service looks in the wrong `.cloudflared` folder).

1. Set `SECURE_COOKIES=true` in `.env`. (Cloudflare serves HTTPS at its edge;
   the app stays plain HTTP on `127.0.0.1:8000`, which is correct on loopback.)
2. Stop any manual smoke-test windows (`Ctrl+C` the `uvicorn` and
   `cloudflared tunnel run` ones) so they don't collide with the tasks.
3. In an **elevated** PowerShell (Run as administrator):

   ```powershell
   cd C:\Users\ianfi\workspace\Dictato
   powershell -ExecutionPolicy Bypass -File deploy\install-dictato-tasks.ps1
   ```

That registers and starts two tasks — **Dictato** (the app) and
**Dictato-Tunnel** (the tunnel) — and prints their status. A `Result` of
`267009` means "running" (good); `0` means it exited. Per-process logs are
`dictato-app.log` and `dictato-tunnel.log` in the repo root.

Then open **https://dictato.levelup-ai.com** — it now survives reboots.

Managing the tasks:

```powershell
Get-ScheduledTask Dictato,Dictato-Tunnel | Get-ScheduledTaskInfo    # status
Stop-ScheduledTask Dictato                                          # stop the app
Start-ScheduledTask Dictato                                         # start it
Unregister-ScheduledTask Dictato,Dictato-Tunnel -Confirm:$false     # remove both
```

---

## Cost

$0 — Cloudflare's Free plan covers the tunnel, DNS, and proxy.

## Locking it to your ~10 users

A permanent public URL means anyone can *reach* the login page (registration is
currently open). To restrict *signup* to your people, add invite-only
registration (a deferred item in `BUILD_PLAN.md`) — separate from hosting, but
worth doing before you share the link widely.

## Reversing the move later

1. Porkbun → `levelup-ai.com` → Authoritative Nameservers → set back to
   Porkbun's defaults (`curitiba.ns.porkbun.com`, `fortaleza.ns.porkbun.com`,
   `maceio.ns.porkbun.com`, `salvador.ns.porkbun.com`).
2. Recreate your DNS records at Porkbun from the Part 0 export.
3. (Optional) Delete the tunnel: `cloudflared tunnel delete dictato`.

The domain was never anywhere but Porkbun — only DNS answering moved.
