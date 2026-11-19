"""ICO self-reported personal data breach cases + regional (ROCU) placement.

Placement chain (best effort, cached on disk, bounded per refresh):
  organisation name -> Companies House registered postcode -> postcodes.io (lat/lng, country)
  -> data.police.uk (police force) -> ROCU.
Organisations that cannot be matched stay "Unplaced". Nothing here runs in the background:
ingest() and geo_pass() are only called from a refresh that a user triggered.
"""
import csv
import difflib
import io
import json
import os
import re
import time
from pathlib import Path

import requests

ICO_PAGE = "https://ico.org.uk/action-weve-taken/complaints-and-concerns-data-sets/self-reported-personal-data-breach-cases/"
UA = {"User-Agent": "EvilEye-Dashboard/1.0 (research)"}
CH_KEY = os.environ.get("COMPANIES_HOUSE_KEY", "")
# When a proxy injects the Companies House credential (dev sandbox), a placeholder key is enough.
CH_ENABLED = bool(CH_KEY) or bool(os.environ.get("CH_PROXY_INJECTED"))
GEO_BUDGET = int(os.environ.get("GEO_BUDGET", "400"))   # new organisations looked up per refresh

# 43 police forces of England & Wales -> ROCU (regional organised crime unit)
ROCU_FORCES = {
    "North East": ["cleveland", "durham", "northumbria"],
    "North West": ["cheshire", "cumbria", "greater-manchester", "lancashire", "merseyside"],
    "Yorkshire and the Humber": ["humberside", "north-yorkshire", "south-yorkshire", "west-yorkshire"],
    "East Midlands": ["derbyshire", "leicestershire", "lincolnshire", "northamptonshire", "nottinghamshire"],
    "West Midlands": ["staffordshire", "warwickshire", "west-mercia", "west-midlands"],
    "Eastern": ["bedfordshire", "cambridgeshire", "essex", "hertfordshire", "norfolk", "suffolk"],
    "London": ["metropolitan", "city-of-london"],
    "South East": ["hampshire", "kent", "surrey", "sussex", "thames-valley"],
    "South West": ["avon-and-somerset", "devon-and-cornwall", "dorset", "gloucestershire", "wiltshire"],
    "Wales": ["dyfed-powys", "gwent", "north-wales", "south-wales"],
}
FORCE_TO_ROCU = {f: r for r, fs in ROCU_FORCES.items() for f in fs}
FORCE_NAMES = {f: f.replace("-", " ").title().replace(" And ", " and ") for f in FORCE_TO_ROCU}
FORCE_NAMES.update({"west-mercia": "West Mercia", "city-of-london": "City of London", "metropolitan": "Metropolitan Police"})

_STOP = re.compile(r"\b(the|limited|ltd|plc|llp|lp|cic|inc|uk|company|co)\b")


def norm(name):
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and "))
    s = _STOP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# ----------------------------------------------------------------- ICO ingest
def _date(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def ingest(data_dir: Path):
    """Return (cases, error). Quarter files are final once published, so each URL is downloaded once."""
    cache = data_dir / "ico"
    cache.mkdir(parents=True, exist_ok=True)
    page = requests.get(ICO_PAGE, headers=UA, timeout=40)
    page.raise_for_status()
    links = sorted(set(re.findall(r'href="(/media2/[^"]+\.csv)"', page.text, flags=re.I)))
    cases, seen = [], set()
    for href in links:
        f = cache / (re.sub(r"[^A-Za-z0-9.]+", "_", href.strip("/")) + "")
        if not f.exists():
            r = requests.get("https://ico.org.uk" + href, headers=UA, timeout=90)
            r.raise_for_status()
            f.write_bytes(r.content)
        text = f.read_bytes().decode("utf-8-sig", errors="ignore")
        for row in csv.DictReader(io.StringIO(text)):
            ref = row.get("ICOCaseRef")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            org = (row.get("Submitted About Account") or "").strip()
            cases.append({
                "ref": ref, "org": org, "key": norm(org), "sector": row.get("Sector") or "Unknown",
                "subsector": row.get("SubSector") or "", "received": _date(row.get("Received Date")),
                "completed": _date(row.get("Completed Date")),
                "quarter": f'{row.get("Completed FY") or "?"} {row.get("Completed Qtr") or ""}'.strip(),
                "decision": row.get("Decision") or "", "cyber": (row.get("ISCyberIncident") or "").strip() == "Yes",
                "cyber_flag": (row.get("ISCyberIncident") or "").strip() or "Not Provided",
                "legislation": row.get("Legislation") or ""})
    return cases, None


# ----------------------------------------------------------------- geo (cached)
def load_geo(data_dir: Path):
    try:
        return json.loads((data_dir / "geocache.json").read_text())
    except Exception:
        return {}


def save_geo(data_dir: Path, geo):
    (data_dir / "geocache.json").write_text(json.dumps(geo))


def _resolve(name):
    """Return a geo record for one organisation name."""
    try:
        r = requests.get("https://api.company-information.service.gov.uk/search/companies",
                         params={"q": name, "items_per_page": 3},
                         auth=(CH_KEY or "proxy-injected", ""), headers=UA, timeout=25)
        if r.status_code == 429:
            return {"status": "rate-limited"}
        if r.status_code in (401, 403):
            return {"status": "auth-error"}
        items = r.json().get("items", []) if r.ok else []
    except Exception:
        return {"status": "error"}
    target = norm(name)
    best, score = None, 0.0
    for it in items:
        s = difflib.SequenceMatcher(None, target, norm(it.get("title"))).ratio()
        if s > score:
            best, score = it, s
    if not best or score < 0.92 or not (best.get("address") or {}).get("postal_code"):
        return {"status": "no-match"}
    pc = best["address"]["postal_code"]
    try:
        p = requests.get("https://api.postcodes.io/postcodes/" + pc.replace(" ", ""), headers=UA, timeout=20)
        if not p.ok:
            return {"status": "no-postcode", "company": best.get("title"), "postcode": pc}
        pr = p.json()["result"]
    except Exception:
        return {"status": "error"}
    rec = {"status": "placed", "company": best.get("title"), "postcode": pc, "country": pr.get("country"),
           "district": pr.get("admin_district")}
    if pr.get("country") == "Scotland":
        rec.update(rocu="Scotland", force="police-scotland")
    elif pr.get("country") == "Northern Ireland":
        rec.update(rocu="Northern Ireland", force="northern-ireland")
    else:
        try:
            f = requests.get("https://data.police.uk/api/locate-neighbourhood",
                             params={"q": f'{pr["latitude"]},{pr["longitude"]}'}, headers=UA, timeout=20)
            force = f.json().get("force") if f.ok else None
        except Exception:
            force = None
        if not force or force not in FORCE_TO_ROCU:
            rec.update(status="no-force")
        else:
            rec.update(force=force, rocu=FORCE_TO_ROCU[force])
    return rec


def geo_pass(cases, geo, data_dir: Path, budget=GEO_BUDGET, stop=lambda: False):
    """Look up up to `budget` not-yet-tried organisations (cyber cases and newest first). Returns count done."""
    if not CH_ENABLED:
        return 0
    order = sorted(cases, key=lambda c: (not c["cyber"], c["completed"] or ""), reverse=False)
    order = sorted(order, key=lambda c: (c["cyber"], c["completed"] or ""), reverse=True)
    todo, seen = [], set()
    for c in order:
        k = c["key"]
        if not k or k in seen or k in geo and geo[k].get("status") in ("placed", "no-match", "no-force", "no-postcode"):
            continue
        seen.add(k)
        todo.append(c)
    done = 0
    for c in todo[:budget]:
        if stop():
            break
        rec = _resolve(c["org"])
        if rec["status"] in ("rate-limited", "auth-error"):
            break
        geo[c["key"]] = rec
        done += 1
        if done % 25 == 0:
            save_geo(data_dir, geo)
        time.sleep(0.55)   # stay under Companies House's 600 requests / 5 minutes
    save_geo(data_dir, geo)
    return done
