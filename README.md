# EvilEye - Dashboard

Private ransomware-intel dashboard for the UK / West Midlands. Sources: ransomware.live, RansomLook,
deepdarkCTI, NCSC, CISA KEV.

**Refresh policy:** no background scanning. Feeds are fetched only when a signed-in user opens the
dashboard (and the stored data is older than `STALE_MINUTES`), or when Refresh is pressed.

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
EVILEYE_PASSWORD=choose-one .venv/bin/python app.py   # http://localhost:8000
```

## Environment variables
| Name | Purpose |
|---|---|
| `EVILEYE_PASSWORD` | Required. Sign-in password. |
| `SECRET_KEY` | Required in production. Long random string for session cookies. |
| `STALE_MINUTES` | Optional, default 30. Page-open refresh only if data is older than this. |
| `DATA_DIR` | Optional, default `./data`. Point at a Railway volume (e.g. `/data`) to keep data across deploys. |
| `COMPANIES_HOUSE_KEY` | Needed for the regional (ROCU) breakdown of ICO breaches; also enriches West Midlands matches. REST key, used server-side only. |
| `GEO_BUDGET` | Optional, default 400. New organisations placed per refresh (Companies House allows 600 requests / 5 min). Coverage grows with each refresh and is cached in `DATA_DIR`. |

## Deploy on Railway
New Project > Deploy from GitHub repo > this repo. Set the variables above. `railway.json` sets the start
command and the `/healthz` health check. Add a volume mounted at `/data` and set `DATA_DIR=/data`.

## Breaches tab (ICO)
Self-reported personal data breach cases from the ICO's quarterly CSVs (Open Government Licence v3.0).
Toggle **Cyber incidents** (the organisation's own `ISCyberIncident = Yes` answer) or **All breaches**.
Regions follow ROCU > police force > organisations. The ICO publishes no locations, so organisations are
placed by Companies House registered office -> postcodes.io -> data.police.uk. Unmatched organisations
(many GP surgeries, schools, charities) show as **Unplaced**; the coverage percentage is shown on the tab.
