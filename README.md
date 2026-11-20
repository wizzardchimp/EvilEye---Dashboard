# EvilEye - Dashboard

Private ransomware-intel dashboard for the UK, broken down by ROCU (regional organised crime unit) region.

**Refresh policy:** no background scanning. The server fetches external feeds only when a signed-in user opens the
dashboard (and the stored data is older than `STALE_MINUTES`), or when Refresh is pressed.

## Data sources
ransomware.live (UK victims, recent victims, gang catalogue) · RansomLook · deepdarkCTI · NCSC · CISA KEV ·
Have I Been Pwned (recent breaches) · BleepingComputer · Krebs on Security · SANS ISC diary.

## Regions (ROCU)
ROCUs are groupings of police forces: ROCU > force > organisation. The 43 forces of England and Wales map to 10 ROCUs;
Scotland and Northern Ireland are their own buckets. A victim is placed by the first method that works:
1. a postcode in its text (postcodes.io, then data.police.uk for the force),
2. a place name in its description (a county / city / large-town list in `geo.py`; approximate),
3. Companies House by name (registered-office postcode), if `COMPANIES_HOUSE_KEY` is set.

Anything else shows as "region unknown". Lookups run in a bounded batch (`GEO_BUDGET`) after an open or Refresh and
are cached in `DATA_DIR`, so coverage grows with each refresh.

**Companies House matching** accepts a near-exact name, or a good name plus the victim's website domain agreeing with
the company name (catches trading names and "Ltd"/"Group" variants). It also retries with generic words removed.
Earlier "no match" results are retried automatically whenever the matching rules change (`MATCH_VERSION` in `geo.py`).

**Overseas / not UK:** ransomware.live sometimes tags foreign companies as UK. A victim is flagged "Overseas / not UK"
when the first "headquartered / based in ..." phrase names a foreign place, nothing points to the UK, and the website is
not `.uk`. Offices abroad do not count. These are left out of the "latest UK incidents" ticker.

**Manual placement:** an admin can set any victim's region (or Overseas / not UK) from the **Edit region** button on the
UK Victims tab, with an optional note. It overrides every automatic method, is stored in PostgreSQL, survives refreshes
and restarts, and is written to the activity log.

The **Region** selector in the header filters Overview, UK Victims, Attack Feed and Gang Catalog, and is saved as the
user's preference, which drives the "Latest in <region>" ticker. Alerts & News are national.

## Users and activity (PostgreSQL)
Accounts live in PostgreSQL (`DATABASE_URL`). The first admin is created from `EVILEYE_ADMIN_USER` (default `admin`) and
`EVILEYE_PASSWORD` when the users table is empty. After that, admins add, disable, delete and reset users on the
**Users** tab, and read the log on the **Activity** tab (filter by user and event, export CSV).
Logged: logins, failed logins, logouts, page opens, refreshes, tab views, searches, filters, region changes, copies of
gang leak-site addresses, password changes and all admin actions.
Passwords are salted scrypt hashes, 10+ characters. A username locks for 15 minutes after 6 failed logins. Changing or
resetting a password signs that user out everywhere.

Without `DATABASE_URL` the app runs in legacy mode: one shared password (`EVILEYE_PASSWORD`), no user management.

## Environment variables
| Name | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (on Railway: `${{Postgres.DATABASE_URL}}`). |
| `EVILEYE_PASSWORD` | First-admin password (and the shared password in legacy mode). |
| `EVILEYE_ADMIN_USER` | Optional, default `admin`. Name of the first admin. |
| `SECRET_KEY` | Required in production. Long random string for session cookies. |
| `DATA_DIR` | Default `./data`. Use a Railway volume (e.g. `/data`) to keep the feed snapshot and region cache. |
| `COMPANIES_HOUSE_KEY` | Optional. Region lookups by company name. REST key, used server-side only. |
| `STALE_MINUTES` | Optional, default 30. Page-open refresh only if data is older. |
| `GEO_BUDGET` | Optional, default 400. API lookups per refresh. |

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
EVILEYE_PASSWORD=choose-one-long-password .venv/bin/python app.py     # legacy mode, http://localhost:8000
```
Set `DATABASE_URL` to use real user accounts.
