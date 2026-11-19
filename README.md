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
| `COMPANIES_HOUSE_KEY` | Optional. Adds registered name/address to West Midlands matches. |

## Deploy on Railway
New Project > Deploy from GitHub repo > this repo. Set the variables above. `railway.json` sets the start
command and the `/healthz` health check. Add a volume mounted at `/data` and set `DATA_DIR=/data`.
