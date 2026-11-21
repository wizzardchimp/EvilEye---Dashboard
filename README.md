# EvilEye - Dashboard

Private ransomware-intel dashboard for the UK, broken down by ROCU (regional organised crime unit) region.

**Refresh policy:** no background scanning. The server fetches external feeds only when a signed-in user opens the
dashboard (and the stored data is older than `STALE_MINUTES`), or when Refresh is pressed.

## Data sources
- **UK victims, gangs:** ransomware.live, RansomLook, deepdarkCTI.
- **Alerts:** NCSC, CISA KEV, Have I Been Pwned, SANS ISC, RansomLook / ransomware.live press.
- **Dark web news:** Dark Web Informer (its site feed: X blocks automated access), DataBreaches.net, Hackread, The Record,
  The Hacker News, SecurityWeek, Dark Reading, BleepingComputer, Krebs on Security.

## Dark web news
Items from the news feeds are kept when they are dark-web relevant (Dark Web Informer always). Each is tagged with themes
(ransomware, data breach / leak, access & credentials, malware, forums & marketplaces, law enforcement, vulnerabilities),
ransomware groups named (matched against the gang catalogue; everyday-word names need a capital letter and ransomware context)
and UK mentions. The welcome page shows the top three (score minus a small recency penalty, at most two per source). The
**Dark Web News** tab has a rule-based briefing built from headlines, categories and group names (it does not read the
articles). The week-on-week change only compares sources whose feed reaches back across both windows.

### Thumbnails
Each news item shows a small preview of the post's own image: from the feed entry (media / enclosure / first image) or, when the
feed has none, the article page's `og:image` (up to 20 pages per refresh, remembered in `DATA_DIR/ogcache.json`). Browsers never
contact the news sites: `/api/news/thumb/<id>` fetches the image on the server, refuses anything that is not a public http(s) host
(checked again on every redirect; no localhost, private, link-local or metadata addresses), accepts only real JPEG/PNG/WEBP/GIF
under 3 MB (never SVG), shrinks it to at most 360x240 JPEG and caches it in `DATA_DIR/thumbs` (pruned as items age out). Dark Web
Informer's images are screenshots of forum posts, so there is a **Thumbnails** switch (welcome page and news tab) that hides them
everywhere and is remembered per browser. Some sites (e.g. DataBreaches.net) refuse automated page fetches, so their items have no
thumbnail.

## UK victims
The list is de-duplicated (the same company claimed again by the same group is one victim). Sector is ransomware.live's own
classification; victims it has not classified are reported separately as Unclassified, not ranked. Each row shows the
company with its town or district in brackets, sorts by any column, pages 50 at a time and exports to CSV (respecting the
region filter). Click a row for the full details drawer (description, how the region was decided, copy-only leak-site address,
and for admins the manual region editor). "New" badges mark victims first seen since your previous visit (a visit starts when
you open the page more than 30 minutes after the last one).

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

## Look and feel
The default theme is a slightly lightened dark. The contrast button in the header (and on the sign-in page) inverts it to a
light theme; the choice is remembered per browser. All colours come from the tokens at the top of `static/style.css`.
The header logo is split in two: the shield (`static/mark-off.png` red eye, `static/mark-on.png` green eye) and the
"EvilEye" text. **Click the shield** to keep the screen awake while the tab is visible: the eye turns green (and so does
the browser-tab icon) while it is on, and the choice is remembered per browser. **Click the EvilEye text** to return to the
Overview. The sign-in page shows the same mark.
