"""Place UK ransomware victims into ROCU (regional organised crime unit) regions.

ROCUs are groupings of police forces, so the chain is  victim -> police force -> ROCU.
A victim is placed by the first method that works (all best effort, results cached on disk):
  1. postcode in the text      -> postcodes.io -> data.police.uk force      (precise)
  2. place name in the text    -> county / city / large-town gazetteer      (approximate, no API calls)
  3. Companies House by name   -> registered-office postcode -> as in 1     (needs COMPANIES_HOUSE_KEY)
Anything else is "Unplaced". Nothing here runs in the background: it only runs inside a refresh a user triggered.
"""
import difflib
import json
import os
import re
import time
from pathlib import Path

import requests

UA = {"User-Agent": "EvilEye-Dashboard/2.0 (research)"}
CH_KEY = os.environ.get("COMPANIES_HOUSE_KEY", "")
CH_ENABLED = bool(CH_KEY) or bool(os.environ.get("CH_PROXY_INJECTED"))   # the 2nd is for sandbox testing only
GEO_BUDGET = int(os.environ.get("GEO_BUDGET", "400"))   # API lookups per refresh

# 43 police forces of England & Wales -> ROCU
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
FORCE_TO_ROCU.update({"police-scotland": "Scotland", "northern-ireland": "Northern Ireland"})
REGIONS = list(ROCU_FORCES) + ["Scotland", "Northern Ireland"]
OVERSEAS = "Overseas / not UK"            # tagged UK by the source, but based abroad
FILTER_REGIONS = REGIONS + [OVERSEAS, "Unplaced"]
MATCH_VERSION = 2                          # bump when matching rules change: older "no match" results are retried
FORCE_NAMES = {f: f.replace("-", " ").title().replace(" And ", " and ") for f in FORCE_TO_ROCU}
FORCE_NAMES.update({"west-mercia": "West Mercia", "city-of-london": "City of London",
                    "metropolitan": "Metropolitan Police", "police-scotland": "Police Scotland",
                    "northern-ireland": "Police Service of Northern Ireland"})

# Place names that point to one force. Case-sensitive, whole words. Ambiguous names (Reading, Bath, Newport,
# Newtown, Bangor, St Ives, Washington, Boston...) are left out on purpose.
GAZETTEER = {
    "northumbria": "Northumberland|Newcastle upon Tyne|Newcastle|Gateshead|Sunderland|Tyneside|Tyne and Wear|Morpeth|Hexham|Alnwick|Cramlington",
    "durham": "County Durham|Durham|Darlington|Peterlee|Consett|Bishop Auckland|Newton Aycliffe",
    "cleveland": "Middlesbrough|Hartlepool|Stockton-on-Tees|Redcar|Teesside|Cleveland|Billingham|Thornaby",
    "greater-manchester": "Greater Manchester|Manchester|Salford|Bolton|Wigan|Oldham|Rochdale|Stockport|Tameside|Trafford|Altrincham|Ashton-under-Lyne|Sale|Stretford",
    "lancashire": "Lancashire|Preston|Blackburn|Blackpool|Burnley|Lancaster|Chorley|Accrington|Morecambe|Fleetwood|Leyland",
    "merseyside": "Merseyside|Liverpool|Birkenhead|Wirral|St Helens|Southport|Bootle|Knowsley|Huyton|Kirkby",
    "cheshire": "Cheshire|Chester|Warrington|Crewe|Macclesfield|Northwich|Widnes|Runcorn|Nantwich|Wilmslow|Knutsford",
    "cumbria": "Cumbria|Carlisle|Kendal|Barrow-in-Furness|Penrith|Workington|Whitehaven|Windermere|Keswick",
    "west-yorkshire": "West Yorkshire|Leeds|Bradford|Wakefield|Huddersfield|Halifax|Kirklees|Calderdale|Dewsbury|Keighley|Pudsey",
    "south-yorkshire": "South Yorkshire|Sheffield|Doncaster|Rotherham|Barnsley",
    "north-yorkshire": "North Yorkshire|York|Harrogate|Scarborough|Skipton|Ripon|Northallerton|Selby|Whitby|Thirsk",
    "humberside": "Humberside|Kingston upon Hull|Hull|Grimsby|Scunthorpe|Beverley|Goole|East Riding|Cleethorpes|Bridlington|Driffield",
    "derbyshire": "Derbyshire|Derby|Chesterfield|Buxton|Matlock|Ilkeston|Glossop|Alfreton",
    "leicestershire": "Leicestershire|Leicester|Loughborough|Hinckley|Melton Mowbray|Coalville|Market Harborough",
    "lincolnshire": "Lincolnshire|Lincoln|Grantham|Skegness|Stamford|Gainsborough|Spalding",
    "northamptonshire": "Northamptonshire|Northampton|Kettering|Corby|Wellingborough|Daventry|Towcester",
    "nottinghamshire": "Nottinghamshire|Nottingham|Mansfield|Newark|Worksop|Beeston|Hucknall",
    "west-midlands": "West Midlands|Birmingham|Coventry|Wolverhampton|Solihull|Walsall|Dudley|West Bromwich|Sutton Coldfield|Stourbridge|Halesowen|Smethwick|Oldbury|Bilston|Wednesbury|Tipton",
    "staffordshire": "Staffordshire|Stoke-on-Trent|Stafford|Burton-on-Trent|Burton upon Trent|Lichfield|Tamworth|Cannock|Newcastle-under-Lyme|Leek|Rugeley|Uttoxeter",
    "warwickshire": "Warwickshire|Warwick|Leamington Spa|Royal Leamington Spa|Nuneaton|Rugby|Stratford-upon-Avon|Bedworth|Kenilworth",
    "west-mercia": "West Mercia|Worcestershire|Shropshire|Herefordshire|Worcester|Shrewsbury|Hereford|Telford|Kidderminster|Redditch|Bromsgrove|Malvern|Evesham|Pershore|Droitwich|Bridgnorth|Ludlow|Oswestry",
    "bedfordshire": "Bedfordshire|Bedford|Luton|Dunstable|Leighton Buzzard|Biggleswade",
    "cambridgeshire": "Cambridgeshire|Cambridge|Peterborough|Ely|Huntingdon|St Neots",
    "essex": "Essex|Chelmsford|Colchester|Southend-on-Sea|Southend|Basildon|Harlow|Brentwood|Braintree|Clacton|Thurrock|Billericay",
    "hertfordshire": "Hertfordshire|Watford|St Albans|Hemel Hempstead|Stevenage|Hatfield|Welwyn|Hertford|Letchworth|Hitchin|Borehamwood|Bishop's Stortford",
    "norfolk": "Norfolk|Norwich|King's Lynn|Great Yarmouth|Thetford|Cromer",
    "suffolk": "Suffolk|Ipswich|Bury St Edmunds|Lowestoft|Felixstowe|Sudbury|Newmarket",
    "city-of-london": "City of London",
    "metropolitan": "Greater London|London|Croydon|Bromley|Hounslow|Harrow|Barnet|Ealing|Enfield|Wembley|Camden|Islington|Hackney|Westminster|Kensington|Chelsea|Canary Wharf|Docklands|Uxbridge|Kingston upon Thames",
    "hampshire": "Hampshire|Southampton|Portsmouth|Winchester|Basingstoke|Aldershot|Fareham|Farnborough|Isle of Wight|Andover|Eastleigh",
    "kent": "Kent|Maidstone|Canterbury|Dover|Ashford|Tunbridge Wells|Dartford|Gravesend|Folkestone|Margate|Sevenoaks|Medway|Chatham|Rochester|Sittingbourne",
    "surrey": "Surrey|Guildford|Woking|Epsom|Redhill|Reigate|Camberley|Staines|Leatherhead|Weybridge|Farnham|Dorking",
    "sussex": "Sussex|Brighton|Hove|Crawley|Eastbourne|Worthing|Chichester|Horsham|Hastings|Bognor Regis|Haywards Heath",
    "thames-valley": "Berkshire|Oxfordshire|Buckinghamshire|Oxford|Slough|Milton Keynes|Newbury|Maidenhead|Bracknell|Windsor|High Wycombe|Aylesbury|Banbury|Abingdon|Wokingham|Thatcham",
    "avon-and-somerset": "Somerset|Bristol|Taunton|Weston-super-Mare|Yeovil|Bridgwater|Clevedon|Keynsham",
    "devon-and-cornwall": "Devon|Cornwall|Exeter|Plymouth|Torquay|Truro|Newquay|Barnstaple|Paignton|Falmouth|Penzance",
    "dorset": "Dorset|Bournemouth|Poole|Dorchester|Weymouth|Christchurch|Wimborne",
    "gloucestershire": "Gloucestershire|Gloucester|Cheltenham|Stroud|Cirencester|Tewkesbury",
    "wiltshire": "Wiltshire|Swindon|Salisbury|Trowbridge|Chippenham|Devizes|Melksham",
    "south-wales": "South Wales|Cardiff|Swansea|Bridgend|Port Talbot|Barry|Neath|Pontypridd|Merthyr Tydfil|Caerphilly|Rhondda",
    "gwent": "Gwent|Cwmbran|Abergavenny|Ebbw Vale|Monmouthshire",
    "dyfed-powys": "Dyfed|Powys|Carmarthenshire|Carmarthen|Llanelli|Aberystwyth|Haverfordwest|Pembrokeshire|Brecon|Welshpool|Ceredigion",
    "north-wales": "North Wales|Wrexham|Llandudno|Rhyl|Colwyn Bay|Conwy|Flintshire|Anglesey|Denbighshire|Gwynedd|Holyhead|Deeside",
    "police-scotland": "Scotland|Edinburgh|Glasgow|Aberdeen|Dundee|Inverness|Stirling|Paisley|Livingston|Falkirk|East Kilbride|Dunfermline|Kilmarnock|Fife|Lanarkshire|Highlands",
    "northern-ireland": "Northern Ireland|Belfast|Londonderry|Derry|Lisburn|Newry|Armagh|Craigavon|Ballymena",
}
# wide-area names (usually the first entry of each list, plus a few extras): used only to choose a better label
COUNTYISH = {names.split("|")[0] for names in GAZETTEER.values()} | {"Greater London", "Tyne and Wear", "Tyneside", "Teesside",
                                                                      "Cleveland", "Berkshire", "Oxfordshire", "Buckinghamshire"}
_TERMS = sorted(((t, f) for f, names in GAZETTEER.items() for t in names.split("|")), key=lambda x: -len(x[0]))
_TERM_RE = [(re.compile(r"(?<![\w-])" + re.escape(t) + r"(?![\w-])"), t, f) for t, f in _TERMS]
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b")
_STOP = re.compile(r"\b(the|limited|ltd|plc|llp|lp|cic|inc|uk|company|co)\b")


def norm(name):
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and "))
    return re.sub(r"\s+", " ", _STOP.sub(" ", s)).strip()


def rocu_of(force):
    return FORCE_TO_ROCU.get(force)


def place_text(text):
    """Gazetteer match. Longest names win (so 'Newcastle-under-Lyme' beats 'Newcastle'); most mentions wins,
    ties go to the earliest mention. Returns (force, matched_name) or None."""
    work, hits = text or "", []
    for rx, term, force in _TERM_RE:
        for m in rx.finditer(work):
            hits.append((m.start(), force, term))
        work = rx.sub(lambda m: " " * len(m.group(0)), work)   # mask so shorter names cannot re-match inside
    if not hits:
        return None
    votes = {}
    for pos, force, _ in hits:
        n, first = votes.get(force, (0, pos))
        votes[force] = (n + 1, min(first, pos))
    best = sorted(votes.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[0][0]
    cands = [t for p, f, t in sorted(hits) if f == best]
    specific = [t for t in cands if t not in COUNTYISH]      # prefer "Stourbridge" over "West Midlands"
    return best, (specific or cands)[0]


def postcode_in(text):
    m = POSTCODE_RE.search((text or "").upper())
    return f"{m.group(1)} {m.group(2)}" if m else None


# ----------------------------------------------------------------- cache
def load_geo(data_dir: Path):
    try:
        return json.loads((data_dir / "geocache.json").read_text())
    except Exception:
        return {}


def save_geo(data_dir: Path, geo):
    (data_dir / "geocache.json").write_text(json.dumps(geo))


# ----------------------------------------------------------------- API lookups
def resolve_postcode(pc):
    """postcode -> force -> ROCU. Returns a geo record."""
    try:
        p = requests.get("https://api.postcodes.io/postcodes/" + pc.replace(" ", ""), headers=UA, timeout=20)
        if not p.ok:
            return {"status": "no-postcode", "postcode": pc}
        pr = p.json()["result"]
    except Exception:
        return {"status": "error"}
    rec = {"status": "placed", "postcode": pc, "district": pr.get("admin_district")}
    if pr.get("country") == "Scotland":
        rec.update(force="police-scotland")
    elif pr.get("country") == "Northern Ireland":
        rec.update(force="northern-ireland")
    else:
        try:
            f = requests.get("https://data.police.uk/api/locate-neighbourhood",
                             params={"q": f'{pr["latitude"]},{pr["longitude"]}'}, headers=UA, timeout=20)
            force = f.json().get("force") if f.ok else None
        except Exception:
            force = None
        if force not in FORCE_TO_ROCU:
            return {"status": "no-force", "postcode": pc}
        rec.update(force=force)
    rec["rocu"] = FORCE_TO_ROCU[rec["force"]]
    return rec


# ----------------------------------------------------------------- overseas detection
_FOREIGN = ("Australia|Sydney|Melbourne|Brisbane|Auckland|New Zealand|United States|USA|U\\.S\\.|New York|New Jersey|California|Texas|Florida|"
            "Illinois|Chicago|Atlanta|Seattle|Canada|Toronto|Vancouver|Montreal|Germany|Berlin|Munich|Duisburg|Hamburg|France|Paris|Spain|Madrid|"
            "Barcelona|Marbella|Italy|Milan|Rome|Netherlands|Amsterdam|Belgium|Brussels|Switzerland|Zurich|Sweden|Stockholm|Norway|Denmark|"
            "Copenhagen|Poland|Warsaw|India|Mumbai|Bangalore|Delhi|Singapore|Hong Kong|Dubai|UAE|South Africa|Johannesburg|Cape Town|Nigeria|"
            "Brazil|Mexico|Japan|Tokyo|China|Shanghai|Republic of Ireland|Dublin|Portugal|Lisbon|Greece|Turkey|Israel")
_FOREIGN_RE = re.compile(r"(?<![\w-])(?:" + _FOREIGN + r")(?![\w-])")
_HQ_RE = re.compile(r"(?i:headquartered|based|located|domiciled|HQ'?d)\s+(?:in|at)\s+(?:the\s+)?([^.;\n]{0,70})")
_UK_RE = re.compile(r"\b(?:UK|U\.K\.|United Kingdom|England|Scotland|Wales|Northern Ireland|Britain|British)\b")


def detect_overseas(text, website=""):
    """Probable overseas head office: the text says it is HQ'd/based abroad, and nothing in it points to the UK.
    Offices abroad are not enough (a UK group with a Sydney office is still UK)."""
    if (website or "").lower().strip().rstrip("/").endswith(".uk"):
        return None
    m = _HQ_RE.search(text or "")          # the FIRST "based/headquartered in ..." phrase decides
    if not m:
        return None
    span = m.group(1)
    if _UK_RE.search(span) or place_text(span):
        return None
    f = _FOREIGN_RE.search(span)
    return f.group(0) if f else None


# ----------------------------------------------------------------- Companies House
_GENERIC = re.compile(r"\b(group|holdings|holding|services|solutions|international|the|uk|limited|ltd|plc|llp)\b", re.I)
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$")


def domain_label(site):
    """'www.birchallfoodservice.co.uk' -> 'birchallfoodservice'. Empty if the field is not a domain."""
    s = re.sub(r"^https?://", "", (site or "").lower().strip()).split("/")[0]
    s = re.sub(r"^www\.", "", s)
    if not _DOMAIN_RE.match(s):
        return ""
    parts = s.split(".")
    label = parts[-3] if len(parts) >= 3 and parts[-2] in ("co", "org", "ltd", "plc", "me", "net", "gov", "ac", "sch") else parts[-2]
    return re.sub(r"[^a-z0-9]", "", label)


def _compact(name):
    return re.sub(r"\s+", "", norm(name))


def score_candidate(name, label, item):
    """-> (accepted, similarity, confidence). Accept on a near-exact name, or a good name plus the website domain
    agreeing with the company name (catches trading names and Ltd/Group variants)."""
    s = difflib.SequenceMatcher(None, norm(name), norm(item.get("title"))).ratio()
    c = _compact(item.get("title"))
    dom = len(label) >= 4 and bool(c) and (c == label or c.startswith(label) or label.startswith(c)
                                           or difflib.SequenceMatcher(None, c, label).ratio() >= 0.85)
    active = item.get("company_status") == "active"
    if s >= 0.92:
        return True, s, "name"
    if dom and (s >= 0.75 or (s >= 0.6 and active)):
        return True, s, "name+domain"
    return False, s, ""


def _ch_search(q):
    r = requests.get("https://api.company-information.service.gov.uk/search/companies",
                     params={"q": q, "items_per_page": 5}, auth=(CH_KEY or "proxy-injected", ""), headers=UA, timeout=25)
    return r


def resolve_company(name, website=""):
    """Companies House: name (and website domain) -> registered-office postcode -> force. Needs a key."""
    label = domain_label(website)
    queries = [name]
    simple = re.sub(r"\s+", " ", _GENERIC.sub(" ", name)).strip()
    if simple and norm(simple) != norm(name) and len(simple) >= 3:
        queries.append(simple)
    best, best_key = None, (-1, -1, -1.0)
    for i, q in enumerate(queries):
        try:
            r = _ch_search(q)
            if r.status_code == 429:
                return {"status": "rate-limited"}
            if r.status_code in (401, 403):
                return {"status": "auth-error"}
            items = r.json().get("items", []) if r.ok else []
        except Exception:
            return {"status": "error"}
        for it in items:
            ok, s, conf = score_candidate(name, label, it)
            if ok and (it.get("address") or {}).get("postal_code"):
                key = (1, 1 if it.get("company_status") == "active" else 0, s)
                if key > best_key:
                    best, best_key, best_conf = it, key, conf
        if best:
            break
        if i + 1 < len(queries):
            time.sleep(0.55)
    if not best:
        return {"status": "no-match"}
    rec = resolve_postcode(best["address"]["postal_code"])
    rec["company"] = best.get("title")
    rec["confidence"] = best_conf
    return rec


TERMINAL = ("placed", "no-match", "no-force", "no-postcode")


def is_terminal(rec):
    """A placed result is final. A 'no match' is only final under the current matching rules, so improving the
    rules (MATCH_VERSION) automatically retries the earlier misses."""
    if not rec:
        return False
    return rec.get("status") == "placed" or (rec.get("status") in TERMINAL and rec.get("v") == MATCH_VERSION)


def geo_pass(victims, geo, data_dir: Path, budget=GEO_BUDGET, stop=lambda: False):
    """Look up victims not yet resolved by an API (newest first), up to `budget`. Returns how many were tried."""
    todo, seen = [], set()
    for v in sorted(victims, key=lambda v: str(v.get("discovered") or ""), reverse=True):
        k = v["key"]
        if not k or k in seen or is_terminal(geo.get(k)) or v.get("manual"):
            continue
        seen.add(k)
        if v.get("text_overseas") or v.get("region_source") == "text-overseas":
            continue            # probable overseas head office: do not match it to a UK company
        # a victim already placed from place names only needs the API if there is a postcode to make it precise
        if v.get("region_source") == "text" and not v.get("postcode_hint"):
            continue
        if not v.get("postcode_hint") and not CH_ENABLED:
            continue            # nothing to look up with; retried automatically once a key is configured
        todo.append(v)
    done = 0
    for v in todo[:budget]:
        if stop():
            break
        rec, used_ch = None, False
        if v.get("postcode_hint"):
            rec = resolve_postcode(v["postcode_hint"])
            rec["source"] = "postcode"
        if (not rec or rec["status"] != "placed") and CH_ENABLED:
            rec, used_ch = resolve_company(v["post_title"], v.get("website", "")), True
            rec["source"] = "companies-house"
        if not rec:
            continue
        if rec["status"] in ("rate-limited", "auth-error"):
            break
        rec["v"] = MATCH_VERSION
        geo[v["key"]] = rec
        done += 1
        if done % 25 == 0:
            save_geo(data_dir, geo)
        time.sleep(0.55 if used_ch else 0.15)   # Companies House allows 600 requests / 5 min
    save_geo(data_dir, geo)
    return done
