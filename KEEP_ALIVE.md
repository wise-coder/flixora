# Keep Render Backend Awake

Use an external ping service to hit the backend before Render's free idle timeout.

Backend health URL:

```text
https://feemx.onrender.com/api/health
```

Expected success response:

```json
{"ok": true, "service": "flixora", "timestamp": 1234567890}
```

Recommended settings:

- Method: `GET`
- Interval: every `5 minutes`
- Timeout: `20-30 seconds`
- Follow redirects: `on`

## Option 1: UptimeRobot

1. Sign in at `https://uptimerobot.com/`
2. Create a new `HTTP(s)` monitor
3. Friendly name: `FeemX Render Backend`
4. URL: `https://feemx.onrender.com/api/health`
5. Monitoring interval: `5 minutes`
6. Save the monitor

## Option 2: Cron-job.org

1. Sign in at `https://cron-job.org/`
2. Create a new cronjob
3. URL: `https://feemx.onrender.com/api/health`
4. Request method: `GET`
5. Schedule: every `5 minutes`
6. Save and enable the cronjob

## Option 3: GitHub Actions

This repo now includes:

`/.github/workflows/keep-render-awake.yml`

It pings:

```text
https://feemx.onrender.com/api/health
```

every `5 minutes`.

To use it:

1. Push the latest repo changes to GitHub
2. Open the repo `Actions` tab
3. Enable workflows if GitHub asks
4. Confirm the workflow named `Keep Render Awake` is present

## Do Not Auto-Redeploy Every 5 Minutes

That is not a safe fix.

- Redeploying every 5 minutes restarts the backend repeatedly
- active users can be interrupted during restart
- build/deploy cycles add more instability, not less
- it does not solve slow upstream movie-detail requests

## Notes

- Render free web services can still restart occasionally even if they do not go idle.
- This keeps the backend awake by sending inbound traffic regularly.
- Do not use `/robots.txt` for this. Use `/api/health`.
- The backend now also warms the home-page cache in the background after startup and refreshes it every 10 minutes while the service stays awake.
- To disable that behavior, set `FLIXORA_DISABLE_BACKGROUND_WARM_CACHE=1` in the backend environment.
