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
you open the page more than 30 minutes after the last one). The filter box matches the same way the search box does (below), so
"wine", "west midlands" or a website all work, and every word you type must be found.

## Search
The search box sits at the **right-hand end of the tab bar** on a desktop (1100px and wider; its list drops down lined up with the box's
right edge). On tablets (641 to 1099px) it is in the header on its own row under the brand, and on phones in the header between the logo
row and the region picker; it moves by itself if you resize the window, keeping what you were typing. It searches **everything the
dashboard has cached**: every breach (UK, overseas and unplaced alike, flagged ones too, whatever region is chosen), every gang, every
dark web news story and every real alert. Press `/` (on a tab with no box of its own) or `Ctrl`/`Cmd`+`K` (anywhere) to jump to it; if the
page is scrolled down they take you back to the top, where the box is. From two letters on, a list drops down under the box:

- **Breaches** (up to 8, with the true total), **Gangs** (3), **Dark web news** (4) and **Alerts** (3), each row with what you need to
  recognise it (a breach shows gang, sector, town and region, and how long ago it was first seen, with NEW, false positive and
  allocated-to badges), and the words you typed picked out in bold.
- **How it ranks:** every word must be found. A name beats a registered name, which beats the gang, website domain, town, region
  or sector, which beats the description; the start of a word beats the middle of one; an exact name, then a name that starts with your
  words, come first; ties go to the most recently seen. A match found only in a description says "in description" and only counts at the
  start of a word (so "bout" finds Boutinot, not "about"). Only the domain of a website is searched, never its path; a pasted web address
  is reduced to its domain. Punctuation and brackets are plain text.
- **Picking one:** a breach opens its details pane, a gang lands on Gang Catalog, a news story on Dark Web News and an alert on Alerts,
  each already filtered to it (old filters are cleared). "View all N breaches in UK Victims" opens the whole list of matches; it shows
  every region for that view, without changing the region saved on your account.
- **Keyboard:** Up/Down (wrapping), Home/End, Enter (with nothing highlighted, the best match), Esc closes the list (a second Esc closes
  the pane), Tab leaves it. It is a screen-reader combobox (`aria-activedescendant`, grouped options).
- Press and alert rows that merely repeat a breach (ransomware.live press, RansomLook listings) are not listed on their own: they are
  under the breach's "Where this was sourced from". The Alerts tab's own text box now matches titles with quotes or accents (it used to
  search a JSON dump, where a quote or an accent never matched).
- Each search is one line in the activity log (`all: <words>`, with `-> breach: <name>` etc. if you picked something). Results are
  never cached by the browser, and each person may make 300 searches a minute. `GET /api/search?q=...`.

## Phones
At 640px wide and below (the phone layout) all text is 15% larger: every font size in `style.css` is written as `size * var(--fs)`,
and `--fs` is 1 normally and 1.15 under 640px. Wider screens are unchanged. Grids now shrink to the screen on very narrow phones
(320 to 360px) instead of making the page scroll sideways. Other phone-layout rules:

- **Header:** logo and name on the left and the four buttons on the same row on the right; then the search box; then "REGION" with the
  sync status on its right and the region picker full width beneath. About 200px tall, and it scrolls away with the page (on larger
  screens it stays pinned). The tagline is dropped under 480px, and the smallest phones (340px and under) get slightly smaller buttons.
  Between 641 and 1099px the search sits on its own row under the brand; at 1100px and wider it moves to the right of the tab bar.
- **Region banner** ("West Midlands only" with Show all UK): one slim line the width of the page; a long region name is shortened with
  an ellipsis before the button ever is, and the word "Showing" is dropped under 480px.
- **Chosen region first:** when a region is chosen and the two Overview lists are stacked, "Latest in <region>" is above "Latest UK
  incidents". Side by side (wide screens) the order is unchanged.
- **Counts** are a 2 x 2 grid, and the **region tiles** are two columns of identical, compact tiles (the share sits beside the number,
  and "of UK" is dropped under 420px) so names of one or two lines no longer push their numbers out of line.

## Breach details
Clicking a breach (a row in UK Victims, or anywhere on an entry in the Overview "Latest" lists (its name included) opens a details
panel. It slides in from the right, 780px wide on a desktop (the page stays visible behind it; the full width of a phone), under a
large title with the gang, sector and region badges, any markers, and the Flag or allocate button. Below that, **each topic is its own bordered card**
with an icon and a 16px title on a tinted band, and the facts inside are laid out two to a row (one on a phone) with the label above the value,
so a long breach reads as a handful of short sections rather than one column of text. The panel scrolls, opening another breach starts at the
top, and flagging or allocating a breach refreshes it in place without losing your place. In order:

- **Breach details**: **Original site**, the company's own web address when the post gives one (otherwise "Not given in the post");
  **Published**, the date on the leak post; **First seen**, when the dashboard's feeds picked it up; **Data**; and **Ransom** and **Re-posted**
  when there are any. (The gang is in the badges at the top and in its own card.) The data amount is the feed's own size when it is a clean figure ("as stated by the
  gang"), otherwise a size quoted in the post text ("quoted in the post text"); about 14% of breaches have either, and the
  rest say "Not stated in the post" rather than guessing. A ransom figure is shown exactly as posted.
- **Company (Companies House)** (card): the company as the register holds it: registered name and number, status and type, date
  of incorporation, the **registered office address** (with a **Map** button after it that opens the address in the device's own maps app: Apple Maps on an iPhone, iPad or Mac, Google Maps everywhere else, which opens the Google Maps app on Android; the click is logged as an outside link without the address), the **current directors** (names and appointment dates only; resigned
  directors, secretaries, birth dates and home addresses are left out), and links to the company's gov.uk page and its officers
  page. It says how the company was matched to the breach (by name, or by name and website) and offers a Companies House search
  in case it is the wrong one. A breach already placed through the register uses its stored company number; otherwise the
  register is searched with the same matching rules as placement. It is read when a summary is opened (never in the background)
  and kept in `DATA_DIR/ch_company.json` for 7 days (a "no match" for 1 day); a rejected key or a busy register is reported and
  never kept. Needs `COMPANIES_HOUSE_KEY`; without it the section says lookup is not set up. `GET /api/victims/company?title=...`.
- **Gang responsible** (card): aliases, a short description, how many UK victims are on record, known leak sites (and how many are
  online), when it was first tracked, and a button to find it in the Gang Catalog.
- **Where this was sourced from** (card): ransomware.live (the victim tracker the breach is read from, linked to that gang's page),
  RansomLook when it also tracks the gang, the gang's own leak-site post, and any other mention in the dashboard's own alert
  and news feeds. Mentions are matched on the whole company name and must fall within 30 days of the post, and names too short
  or generic to match safely are skipped, so a listed mention is never a guess. The leak-site address is **copy only**: it is
  never opened from the dashboard. The extras load from `GET /api/victims/context?title=...`.
- **Region**: where it was placed and the basis (see Regions (ROCU)), then **Description** (from ransomware.live) and, for admins only, a
  dashed **Admin: set region** card with the manual placement form.
- **Region** (and how it was decided), the description, flags, the Task and other markers, and the admin region editor.

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

## Flags, tasks and markers
Every victim (in the Overview tickers, the UK Victims table and the details drawer) has a flag button. Any signed-in user can:

- **Flag a false positive**, as either **Not in <its region>** (a UK victim that is not in that region) or **Not in the UK**,
  with an optional note. The victim leaves that region (or the UK) for everyone straight away: it shows as "region unknown"
  or "Overseas / not UK" with a *false positive* badge, and the region tiles and tickers update. The automatic placement is
  kept underneath, so **Undo flag** (also offered as an Undo link right after flagging) restores it exactly. Only the person
  who flagged it, or an admin, can undo a flag. An admin's manual placement always wins: a manually placed victim cannot be
  flagged, and an existing flag stops applying while a manual placement exists.
- **Put a Task on it** (this is the old *Allocate*): 2 to 4 letters of initials, to show they are working on it, and an optional
  **Allocated on** date and time (left empty it is now; the picker stops at the present, so a task cannot be dated in the future). The
  dialog then shows a **Task progress** section straight under the allocation banner, with the three dates, the state and the buttons:
  **Allocated** → **Start now** → **In progress** → **Complete now** → **Completed**, plus **Reopen** (back to in progress) and **Edit dates and
  initials** for correcting any of the three dates (Started and Completed may be left empty if that has not happened). Dates are checked:
  it cannot be started before it was allocated, completed before it was started, or dated in the future, and the message says which.
  Allocating a completed task again starts a fresh cycle. Initials can be remembered per user. Anyone can **take over** a live task (its dates
  are kept and the new initials go on); only the person who set it, the person with those initials, or an admin can start, complete, edit,
  reopen or release it, and everyone else is told who can.
- **Custom markers**: any other marker an admin has created in Settings (below). Each is in the dialog's **Other markers** section as a
  collapsed row ("not set" or its state) and works exactly like the Task: initials, dates, the same buttons, plus **Remove**. A breach can
  carry the Task and any number of other markers at once.

A marker is **live** while it is allocated or in progress; once completed it stops showing in the banner and as a badge in the lists, but it is
kept (and shown in the breach pane and the dialog) as the record of who did it and when.

**The Live tasks banner.** A band across the top of the Overview, above both the national and the regional lists and as wide as the two together,
shows every live marker as a tile: the marker, the initials, "Allocated" or "In progress", the region and town, and how long ago it was allocated
and started. Work in progress comes first (the most recently started on the left), then the ones still waiting, the longest waiting first. A
tile's left edge and badge are in the marker's colour. Click a tile to open that breach's pane. If there are more than fit, the row scrolls
sideways (swipe on a phone) with a fade at the edge as the hint. With a region chosen, a switch **Only <region>** narrows the banner to that region
(your choice is remembered on that browser). It refreshes with the page, every minute, and straight after any change. `GET /api/markers/live`
(optionally `?rocu=`; at most 100 tiles, with the true total).

**In the lists.** Every live marker is a badge on its breach in the national list, the regional list and the UK Victims table: the Task as a
person-icon badge with the initials, a custom marker with its name (long names are shortened, the full wording is in the tooltip) and the
initials; an in-progress marker is drawn with a play icon and a stronger tint. The UK Victims **Show** filter lists *Allocated* (any live marker),
*Allocated to me* and *False positives* (the last also finds victims flagged out of the selected region, so they can be reviewed or undone).

**Settings tab (admins only).** Create your own markers, like the Task: type a name (1 to 24 characters, no two the same whatever the capitals),
pick one of ten colours or any colour, watch the live preview, and Add. In the list each marker shows its colour (change it with the colour
box; it saves at once), how many breaches carry it live and in total, and **Rename**, **Retire** / **Use again** and **Delete**. A retired marker
stays on the breaches that have it and can still be worked on, but cannot be put on anything new. The Task is built in: it can be recoloured
and renamed but not retired or deleted. A marker that is on any breach cannot be deleted (retire it instead). Colours are checked on the server
and again in the browser before they reach a style, names are only ever shown as text, and the badge text colour is derived from the marker's
colour at a fixed lightness per theme so that it reads at 4.5:1 or better whatever colour is chosen (even pure yellow, white or black).

Stored in PostgreSQL (`victim_flags`, `marker_types`, `victim_markers`, plus `users.initials` and `app_settings`); they survive refreshes and
restarts, appear in the CSV export (`false_positive_flag`, `allocated_to` for the Task's initials and `live_markers` for everything live) and in the
activity log (*Flagged a false positive*, *Undid a false-positive flag*, *Allocated a victim*, *Released a victim allocation*, *Put a marker on a
breach*, *Updated a marker*, *Removed a marker*, *Created / Changed / Deleted a marker type*). Without a database the buttons are hidden.
API: `GET /api/marker-types`; admin `POST /api/admin/marker-types` `{name, colour}` and `PATCH` / `DELETE /api/admin/marker-types/<id>`
(`{name, colour, active}`); `PUT /api/victims/markers` `{title, type_id?, initials, allocated_at?}` (no `type_id` means the Task; an existing live one is
taken over); `PATCH` `/api/victims/markers/<id>` `{initials?, allocated_at?, started_at?, completed_at?}` (an ISO time, `"now"`, or `null`
for the last two) and `DELETE` the same. The older `/api/victims/alloc` PUT and DELETE still work and act on the Task.

*Upgrading:* the first start with markers creates the Task and, once (recorded in `app_settings` as `markers_migrated`), turns every existing
allocation into a Task marker in the *Allocated* state, with its initials, who set it and its date unchanged. The old `victim_alloc` table is left
in place and is not read again.

## Users and activity (PostgreSQL)
Accounts live in PostgreSQL (`DATABASE_URL`). The first admin is created from `EVILEYE_ADMIN_USER` (default `admin`) and
`EVILEYE_PASSWORD` when the users table is empty. After that, admins add, disable, delete and reset users on the
**Users** tab, and read the log on the **Activity** tab (filter by user and event, export CSV).

**The Users tab** lists each person with their role, groups, region and status, and two separate times:
- **Last signed in**: when they last entered their password (how long ago, with the exact date and time underneath, or "Never signed in").
- **Last active**: the last thing they did on the dashboard (any logged event: opening it, changing page, searching...), with a green dot if
  that was in the last 5 minutes and a link to their event list. A browser stays signed in between visits, so someone can be active for weeks
  without signing in again; a failed sign-in does not count as being active. Hovering gives the exact time.
- **Show** filters the table to one group (or "People in no group"), and **Sort by** orders it by username, last active or last signed in (either
  way round; people who never have go with the longest ago).

**Groups** (admins only) are named sets of people, kept in their own table (`user_groups`, with `user_group_members` linking them to users):
- Create a group by typing a name (1 to 40 characters; extra spaces are tidied; no two groups may share a name, whatever the capitals; it needs
  a letter or number and may not contain control or invisible characters; up to 200 groups).
- A person can be in **several** groups. Put them in from the **+ Add group** menu on their row, take them out with the x on a group chip, or
  use a group card's **Members** button for a tick-list of everyone ("Tick everyone" / "Untick everyone"). **Rename** keeps the members;
  **Delete** asks first and only takes people out of the group, it never deletes them. Deleting a person removes them from their groups.
  Clicking a group's name in its card filters the table to its members.
- Every change is in the activity log (`group_create`, `group_rename`, `group_delete`, `group_members` with who was added and removed).
- Each person can see their own groups in their account dialog (the person icon in the header). Groups do not change what anyone can see or
  do yet; they are labels to build on.
- API (admin only): `GET /api/admin/groups`; `POST /api/admin/groups` `{name}`; `PATCH` / `DELETE /api/admin/groups/<id>`;
  `PUT /api/admin/groups/<id>/members` `{user_ids: [...]}` (replace the member list); `PUT` / `DELETE /api/admin/groups/<id>/members/<user id>`
  (add / remove one person, safe to repeat). `GET /api/admin/users` now also returns each person's `group_ids` and the groups.

Logged: logins, failed logins, logouts, page opens, refreshes, tab views (including pages reached by a shared link), searches,
filters, region changes, victim details opened, **screen wake on / off / failed** (with the browser's reason), **outbound links
clicked** (where it was clicked and the destination host and path, query strings are not kept), **leak-site addresses copied**
(the gang or victim and the exact address), password changes and all admin actions.
The Activity tab and each user's activity list show friendly event names ("Copied a leak-site address", "Screen wake on") with the
raw name as a tooltip; the filter and the CSV export keep the raw names (`copy_leak_site`, `screen_wake_on`). Feed-supplied links are only ever used as
http(s) links.
Passwords are salted scrypt hashes, 10+ characters. Changing or resetting a password signs that user out everywhere.

**Sign-in lockout.** Only wrong-password attempts count, over a sliding 15 minutes:

| Limit | Locks | Does not apply to |
|---|---|---|
| 6 wrong passwords for one name **from one address** | that name from that address | anyone else: the real admin, from their own address, is not locked out by someone guessing at `admin` |
| 30 wrong passwords for one name from any addresses | that name, for a new address | an address that person has signed in from in the last 30 days |
| 60 wrong passwords from one address over any names | that address, for names it has not signed in as before | a user who has signed in from that address in the last 30 days (a shared office address) |

A locked attempt is refused without checking the password (so a correct guess does not get through) and is logged as
"locked out", but those rows do not count towards the limits, so hammering a locked account does not extend the lock.

**Client address.** The activity log and the lockout use the address in `X-Real-IP`. Railway's edge overwrites that header and
`X-Forwarded-For` on every request (checked against the live site: a visitor's forged values never arrive), so the visitor cannot choose
their own address. If `X-Real-IP` is missing, the fallback is `X-Forwarded-For`, which on Railway reads "visitor, edge", so the entry
**second from the right** is used (not the last, which is Railway's own server, and not the first). Anything that is not a valid IP address is
discarded, and a local copy uses the connection's own address. `CLIENT_IP_HEADER` and `PROXY_HOPS` override this if the hosting changes.

**Dependencies** are pinned to exact tested versions in `requirements.txt` and checked with `pip-audit -r requirements.txt`
(no known vulnerabilities at the last check). Update them deliberately: change the pin, run the tests, deploy.

Without `DATABASE_URL` the app runs in legacy mode: one shared password (`EVILEYE_PASSWORD`), no user management.

## Security headers
Every response carries browser-side protections (`security_headers` in `app.py`):

| Header | What it does |
|---|---|
| `Content-Security-Policy` | Scripts, images, fonts and connections may only come from the site itself: no inline scripts, no `eval`, no other hosts. Forms post only to the site, no `<base>` tricks, no plug-ins. Styles may also be inline, the one concession, because a few `style="..."` attributes set bar widths. |
| `Strict-Transport-Security` | HTTPS only for a year (`includeSubDomains`), sent when the request arrived over HTTPS. |
| `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'` | The dashboard cannot be put in a frame on any site, including itself (clickjacking). |
| `X-Content-Type-Options: nosniff` | Browsers must trust the declared file type. |
| `Referrer-Policy: no-referrer` | Outside links never learn the dashboard address. |
| `Permissions-Policy` | Camera, microphone, location, payment, USB and motion sensors are off. The screen wake lock and the clipboard (copy buttons) stay available. |
| `Cross-Origin-Opener-Policy` / `Cross-Origin-Resource-Policy: same-origin` | Other sites cannot get a handle on the dashboard window or load its files. |
| `Cache-Control: no-store` | Pages, data and downloads are never kept by the browser or a proxy (static files and the thumbnail proxy keep their own caching). |

To keep the policy strict, the small scripts that used to be inline now live in files: `static/theme-init.js` (applies a saved
light theme before the page paints), `static/login.js` (the sign-in page's messages and theme button) and `static/gs-place.js` (puts the
search box in the tab bar's row or the header, depending on the screen width, before the page first paints). Any new script must be a
file under `/static/`; inline `<script>` blocks and `onclick="..."` attributes will be blocked.

**If the policy ever blocks something it should not**, set `CSP_MODE=report-only` in Railway and redeploy: the site then works
exactly as before while the browser console (F12) lists what the policy would have blocked. `CSP_MODE=off` removes the policy
entirely. The other headers stay on in both. The default, and any unrecognised value, is the enforcing policy.

## Environment variables
| Name | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (on Railway: `${{Postgres.DATABASE_URL}}`). |
| `EVILEYE_PASSWORD` | First-admin password (and the shared password in legacy mode). |
| `EVILEYE_ADMIN_USER` | Optional, default `admin`. Name of the first admin. |
| `SECRET_KEY` | Required in production. Long random string for session cookies. |
| `DATA_DIR` | Default `./data`. Use a Railway volume (e.g. `/data`) to keep the feed snapshot, region cache and Companies House cache. |
| `COMPANIES_HOUSE_KEY` | Optional. Region lookups by company name, and the company details (registered office, directors) in the breach summary. REST key, used server-side only. |
| `CSP_MODE` | Optional emergency switch for the Content-Security-Policy: `enforce` (default), `report-only` or `off`. See Security headers. |
| `CLIENT_IP_HEADER` | Optional, default `X-Real-IP`: the header the host's proxy sets to the visitor's address. Empty to ignore it and use `X-Forwarded-For` only. |
| `PROXY_HOPS` | Optional, default 2: which entry of `X-Forwarded-For`, counting from the right, is the visitor (Railway writes "visitor, edge"). Only used when `X-Real-IP` is absent. |
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
The first time a user signs in (after choosing their own password) they are taken through four short steps, with Back and
"Skip for now" throughout:

1. **Welcome**: three bullets on what EvilEye is.
2. **Light or dark?** Two cards with a small preview of each look. Choosing one applies it to the whole page straight away
   (arrow keys work too) and saves it. The choice is kept on the account (`users.theme`, `PUT /api/me/theme`) as well as in
   the browser, so it follows the person to other devices; the header half-filled-circle button changes it later and saves
   the same way. People who have never chosen keep this browser's last setting.
3. **What the icons mean**: every header icon (Refresh, Light or dark, Account, Sign out) and every marker on a breach
   (Flag or allocate, the initials badge, False positive, New, the outside-link arrow, Copy) with a sentence on what it is
   for. The icons are taken from the live buttons on the page, so the guide always shows exactly what is on screen.
4. **Choose your local ROCU region** (each region lists the police forces it covers and how many victims are on record).
   Choosing a region saves it as their preference and applies it.

Skipping at any step leaves the remaining choices unchanged but keeps anything already chosen (for example the look). The tour
is shown once per user (`users.onboarded_at`) and can be shown again from the Account button ("Show the welcome tour"). Finishing
or skipping is written to the activity log. Without a database it is remembered for the session only.

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
