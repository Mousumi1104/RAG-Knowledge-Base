# Keep the backend awake (free-tier 502 fix)

## Why you see `502` at the backend URL

The backend runs on a **Render free web service**. Free services **spin down after ~15 minutes of inactivity**. The next request triggers a spin-up that takes ~30–60s, and **during that window Render's router returns fast `502`s** (it does not hold the request). Once the container is up and passes its health check, requests return `200` again.

This is normal free-tier behavior — not a crash. The database and app are healthy; the instance was just asleep.

> In-app document uploads are already hardened against this: the frontend pings `/health` to wake the backend and retries the upload, so end users rarely notice. This guide stops the 502s entirely by never letting the service sleep.

## Fix: ping the health endpoint every 5 minutes

Point a free uptime monitor at the health endpoint. A request every 5 min is well under Render's 15-min idle timeout, so the service stays awake 24/7.

**Health URL to monitor:**

```
https://rag-knowledge-base-86tb.onrender.com/health
```

Expected response: `200` with body `{"status":"ok"}`.

### Option A — UptimeRobot (recommended)

1. Sign up free at <https://uptimerobot.com>.
2. **Dashboard → Add New Monitor**.
3. Set:
   - **Monitor Type:** `HTTP(s)`
   - **Friendly Name:** `RAG backend keep-warm`
   - **URL (or IP):** `https://rag-knowledge-base-86tb.onrender.com/health`
   - **Monitoring Interval:** `5 minutes` (the free-plan minimum — that's fine)
4. **Create Monitor.**

That's it. UptimeRobot will hit `/health` every 5 minutes and keep the service warm. You'll also get downtime alerts as a bonus.

### Option B — cron-job.org (alternative)

1. Sign up free at <https://cron-job.org>.
2. **Create cronjob.**
3. Set:
   - **Title:** `RAG backend keep-warm`
   - **URL:** `https://rag-knowledge-base-86tb.onrender.com/health`
   - **Schedule:** every 5 minutes (`*/5 * * * *`)
4. **Save.**

## Notes

- **Monitor `/health`, not `/`.** The API root `/` has no route (it would 404 even when healthy); `/health` is the intended liveness probe.
- This is a **workaround for the free tier**. The permanent fix is upgrading the Render service to a paid plan (e.g. Starter), which never spins down.
- ⚠️ Independent of keep-warm, the **free Postgres expires ~2026-07-01** — upgrade or migrate the database before then to avoid data loss.
