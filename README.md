# Scanline — Live SEO Audit Tool

A working SaaS-style tool inspired by [JeffLi1993/seo-audit-skill](https://github.com/JeffLi1993/seo-audit-skill).
User pastes a URL → server fetches the real page live → runs 19+ deterministic checks → renders a scored report. No mock data, no API keys required for the core checks.

## What it checks (mirrors the skill's "Layer 1: Script" checks)

**Site-level:** HTTP status, HTTPS, HTTP→HTTPS redirect, response time, robots.txt, XML sitemap, 404 handling, E-E-A-T trust pages (About/Contact/Privacy/Terms).

**Page-level:** Title tag, meta description, H1, heading structure, canonical tag, robots meta (noindex), word count, image alt text, internal/external links, Open Graph/Twitter tags, JSON-LD structured data, URL slug hygiene.

Each check returns `pass` / `warn` / `fail` with a plain-English reason and evidence pulled straight from the live page. The score is a weighted pass/warn/fail percentage, and the top 3 fails/warnings are surfaced as Priority Actions — same idea as the original skill's report.

## Run it locally

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5050`, paste any public URL, click **Run audit**.

## Deploy it as a real SaaS

This is a plain Flask app — deploy it anywhere that runs Python:
- **Render / Railway / Fly.io**: connect the repo, it'll detect `app.py`, set the start command to `gunicorn app:app`.
- **A VPS**: `pip install gunicorn`, then `gunicorn -w 4 -b 0.0.0.0:8000 app:app` behind nginx.

For production, swap Flask's dev server for gunicorn (`pip install gunicorn`) — the dev server used here is fine for local testing only.

## Extending it further

- **PageSpeed / Core Web Vitals**: add a `check-pagespeed.py`-style call to the PageSpeed Insights API (needs a free Google API key) and merge results in.
- **Multi-page crawl**: currently audits one URL per request, same as the base `seo-audit` skill tier — extend `audit_engine.py` to walk the sitemap for a full-site crawl (like the `seo-audit-full` tier).
- **Accounts + saved history**: add a database (SQLite/Postgres) and store each `run_audit()` result per user.
- **LLM semantic layer**: the original skill's real edge is Layer 2 — an LLM judging keyword intent, content depth, and E-E-A-T quality on top of these deterministic checks. You can wire this app to call the Claude API (see `audit_engine.py` output — pass any check with `status: "warn"` to an LLM prompt for a semantic verdict) to fully replicate the two-layer architecture.

## Files

```
app.py              Flask routes + API
audit_engine.py      All the real audit logic (the actual "work")
templates/index.html Frontend — form, results rendering, styling
requirements.txt
```
