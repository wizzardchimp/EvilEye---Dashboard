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

## Sectors
- **Ransomware victims:** the sector is ransomware.live's own `activity` category (14 values). Victims it has not
  classified ("Not Found") show as **Unclassified** and are counted, so shares add up to 100%.
- **ICO cases:** the ICO's `Sector` (22 values) and `SubSector` (about 138). The Breaches tab drills sector > sub-sector.
- **Comparison card:** the two lists differ, so both are mapped to one approximate common set (below). It is a
  judgement call; edit `RL_TO_COMMON` / `ICO_TO_COMMON` in `breaches.py` to change it.

| Common sector | ransomware.live | ICO |
|---|---|---|
| Agriculture and food | Agriculture and Food Production | - |
| Charity, religious and membership | - | Charitable and voluntary, Membership association, Religious |
| Education | Education | Education and childcare |
| Energy and utilities | Energy & Utilities | Utilities |
| Finance and insurance | Financial Services | Finance, insurance and credit |
| Government and public sector | Government & Defense | Local government, Central Government, Justice, Regulators, Political |
| Health and social care | Healthcare | Health, Social care |
| Legal and professional services | Professional Services | Legal |
| Media and marketing | - | Marketing, Media |
| Other | Other | General business |
| Property and construction | - | Land or property services |
| Retail, manufacturing and trade | Manufacturing, Retail & E-Commerce | Retail and manufacture |
| Technology and telecoms | Technology | Online Technology and Telecoms |
| Transport, travel and hospitality | Transportation, Hospitality | Transport and leisure |
| Unclassified | Unclassified | Unknown |
