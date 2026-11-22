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
**Dark Web News** tab opens with a visual briefing: a one-line headline, a trend chip, icon-led key points (volume, leading themes,
ransomware groups named, Dark Web Informer claims, UK mentions) and a theme donut with legend. Click
a donut segment, a legend row or a ransomware group to filter the list below (removable chips show what is applied). It is
rule-based, built from headlines, categories and group names, and does not read the articles. The week-on-week trend only compares
sources whose feed reaches back across both windows, and says "no trend yet" otherwise.

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
region filter). Click a row, or any entry in the Overview "Latest" lists, for the breach details (see below). "New" badges mark victims first seen since your previous visit (a visit starts when
you open the page more than 30 minutes after the last one).

## Breach details
Clicking a breach (a row in UK Victims, or anywhere on an entry in the Overview "Latest" lists (its name included) opens a details
panel:

- **Original site**: the company's own web address when the post gives one (otherwise "Not given in the post").
- **Published**: the date on the leak post. **First seen**: when the dashboard's feeds picked it up.
- **Gang**, **Data** and **Ransom**. The data amount is the feed's own size when it is a clean figure ("as stated by the
  gang"), otherwise a size quoted in the post text ("quoted in the post text"); about 14% of breaches have either, and the
  rest say "Not stated in the post" rather than guessing. A ransom figure is shown exactly as posted.
- **Gang responsible**: aliases, a short description, how many UK victims are on record, known leak sites (and how many are
  online), when it was first tracked, and a button to find it in the Gang Catalog.
- **Where this was sourced from**: ransomware.live (the victim tracker the breach is read from, linked to that gang's page),
  RansomLook when it also tracks the gang, the gang's own leak-site post, and any other mention in the dashboard's own alert
  and news feeds. Mentions are matched on the whole company name and must fall within 30 days of the post, and names too short
  or generic to match safely are skipped, so a listed mention is never a guess. The leak-site address is **copy only**: it is
  never opened from the dashboard. The extras load from `GET /api/victims/context?title=...`.
- **Region** (and how it was decided), the description, flags and allocation, and the admin region editor.

## Regions (ROCU)
ROCUs are groupings of police forces: ROCU > force > organisation. The 43 forces of England and Wales map to 10 ROCUs;
Scotland and Northern Ireland are their own buckets. A victim is placed by the first method that works:
1. a postcode in its text (postcodes.io, then data.police.uk for the force),
2. a place name in its description (a county / city / large-town list in `geo.py`; approximate),
3. Companies House by name (registered-office postcode), if `COMPANIES_HOUSE_KEY` is set.

Anything else shows as "region unknown". Lookups run in a bounded batch (`GEO_BUDGET`) after an open or Refresh and
are cached in `DATA_DIR`, so coverage grows with each refresh.

**Companies House matching** accepts a near-exact name, or a good name plus the victim's website domain agreeing with
the company name (catches trading names and "Ltd"/"Group" variants). Abbreviations are expanded on both sides, so
"Saddlers Court MFG" matches "SADDLERS COURT MANUFACTURING LIMITED" (and `saddlerscourtmfg.com` agrees with it); up to
three searches are made per victim: the name as written, "t/a" / bracketed trading names, abbreviations expanded, then
generic words removed. Searches are paced under the Companies House limit (600 per 5 minutes), newest victims first.
Earlier "no match" results are retried automatically whenever the matching rules change (`MATCH_VERSION` in `geo.py`);
a rejected key or a rate limit never records "no match", so those victims are simply retried.

**When a victim has no region, the page says why.** The Overview shows a warning if Companies House rejects the key
(no register lookups can run), plus how many unplaced victims are still waiting for a lookup or had no register match.
The victim drawer's Basis row gives the reason for that victim (no location in the description, not looked up yet, no
register match, key rejected, lookup off). The last run's counts are kept in `DATA_DIR/geo_status.json`.

**Overseas / not UK:** ransomware.live sometimes tags foreign companies as UK. A victim is flagged "Overseas / not UK"
when the first "headquartered / based in ..." phrase names a foreign place, nothing points to the UK, and the website is
not `.uk`. Offices abroad do not count. These are left out of the "latest UK incidents" ticker.
A description that is only a bare country ("United States") is a *weak* signal, because the source data is often wrong
about it: those victims are still marked overseas, but are looked up in Companies House under a stricter rule (the
website domain must also agree with the registered name), and a confirmed match moves them into their UK region.

**Manual placement:** an admin can set any victim's region (or Overseas / not UK) from the **Edit region** button on the
UK Victims tab, with an optional note. It overrides every automatic method, is stored in PostgreSQL, survives refreshes
and restarts, and is written to the activity log.

The **Region** selector in the header filters Overview, UK Victims and Gang Catalog, and is saved as the
user's preference, which drives the "Latest in <region>" ticker. Alerts & News are national.

**Latest UK incidents / Latest in <region>** show the five newest victims each. **View more** expands a list to 25, then
**Show 25 more** adds 25 at a time (a height-capped, scrollable list; **Show fewer** collapses it). Each entry shows the
group, sector and age on the left and the victim's area (town or district, when known) in a box on the right. Click an entry
(its name included) to open the breach details, where the company's own website is linked as "Original site". Entries carry no
outside links of their own. `/api/ticker?uk=N&reg=N` returns up to 1,500 per list.

## Flags and allocation
Every victim (in the Overview tickers, the UK Victims table and the details drawer) has a flag button. Any signed-in user can:

- **Flag a false positive**, as either **Not in <its region>** (a UK victim that is not in that region) or **Not in the UK**,
  with an optional note. The victim leaves that region (or the UK) for everyone straight away: it shows as "region unknown"
  or "Overseas / not UK" with a *false positive* badge, and the region tiles and tickers update. The automatic placement is
  kept underneath, so **Undo flag** (also offered as an Undo link right after flagging) restores it exactly. Only the person
  who flagged it, or an admin, can undo a flag. An admin's manual placement always wins: a manually placed victim cannot be
  flagged, and an existing flag stops applying while a manual placement exists.
- **Allocate** the victim by putting 2 to 4 letters of initials on it, to show they are working on it. The initials appear as
  a badge (with who set it and when in its tooltip). Initials can be remembered per user, anyone can take over, and the
  allocator, the person with those initials, or an admin can release it.

The UK Victims **Show** filter lists *Allocated*, *Allocated to me* and *False positives* (the last also finds victims flagged
out of the selected region, so they can be reviewed or undone). Flags and allocations are stored in PostgreSQL (tables
`victim_flags` and `victim_alloc`, plus `users.initials`), survive refreshes and restarts, appear in the CSV export
(`false_positive_flag`, `allocated_to`) and in the activity log (*Flagged a false positive*, *Undid a false-positive flag*,
*Allocated a victim*, *Released a victim allocation*). Without a database the buttons are hidden.

## Users and activity (PostgreSQL)
Accounts live in PostgreSQL (`DATABASE_URL`). The first admin is created from `EVILEYE_ADMIN_USER` (default `admin`) and
`EVILEYE_PASSWORD` when the users table is empty. After that, admins add, disable, delete and reset users on the
**Users** tab, and read the log on the **Activity** tab (filter by user and event, export CSV).
Logged: logins, failed logins, logouts, page opens, refreshes, tab views (including pages reached by a shared link), searches,
filters, region changes, victim details opened, **screen wake on / off / failed** (with the browser's reason), **outbound links
clicked** (where it was clicked and the destination host and path, query strings are not kept), **leak-site addresses copied**
(the gang or victim and the exact address), password changes and all admin actions.
The Activity tab and each user's activity list show friendly event names ("Copied a leak-site address", "Screen wake on") with the
raw name as a tooltip; the filter and the CSV export keep the raw names (`copy_leak_site`, `screen_wake_on`). Feed-supplied links are only ever used as
http(s) links.
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

## First sign-in: choose your own password
A password an admin has set is known to the admin, so it is temporary. Users created from the Users tab, anyone whose password an
admin resets, and a first admin created from `EVILEYE_PASSWORD` get a **Set your own password** screen before anything else
(then the welcome tour). It is enforced by the server (`users.must_change`): until the password is changed every API call except
`/api/me`, `/api/me/password` and sign-out returns 403. The new password must be at least 10 characters, differ from the temporary
one and not contain the username. Existing users are not forced. The Users page marks pending users "must set password", and the
change is logged as "first sign-in: chose own password". The form carries the username and `new-password` hints so password managers
can offer to save it.

## Welcome tour
The first time a user signs in they see a welcome screen (three bullets: what EvilEye is) and then **Choose your local ROCU
region** (each region lists the police forces it covers and how many victims are on record). Choosing a region saves it as
their preference and applies it; Skip leaves it unchanged. It is shown once per user (`users.onboarded_at`) and can be
reopened from the Account button. Finishing or skipping is written to the activity log. Without a database it is remembered
for the session only.

## Look and feel
The default theme is a slightly lightened dark. The contrast button in the header (and on the sign-in page) inverts it to a
light theme; the choice is remembered per browser. All colours come from the tokens at the top of `static/style.css`.
The header logo is split in two: the shield (`static/mark-off.png` red eye, `static/mark-on.png` green eye) and the
"EvilEye" text. **Click the shield** to keep the screen awake while the tab is visible: the eye turns green (and so does
the browser-tab icon) while it is on, and the choice is remembered per browser. **Click the EvilEye text** to return to the
Overview. The sign-in page shows the same mark.

## Backup
`main` is mirrored to a second GitHub repository, <https://github.com/wizzardchimp/Evil-Eye-Dashboard>, as a backup of the
code. Railway deploys from `wizzardchimp/EvilEye---Dashboard` only. The backup is a plain copy of the same history, so after
each change push to both:

```bash
git remote add backup https://github.com/wizzardchimp/Evil-Eye-Dashboard   # once per clone
git push origin main && git push backup main
```

It holds code only: secrets live in Railway variables and the database and data volume are not in git.
