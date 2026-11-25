"""EvilEye - Dashboard backend.

Data refresh policy: there is NO background loop. External feeds are fetched only
  * when a signed-in user opens the dashboard and the stored data is stale (STALE_MINUTES), or
  * when a user presses Refresh.
Email alerts to groups (alerts.py) go out at the end of those refreshes, once the regions have been looked up: they are not on a timer either.
Users and their activity live in PostgreSQL (DATABASE_URL). Without it the app falls back to a single shared
password (EVILEYE_PASSWORD) and user management is unavailable.
"""
import csv
import hashlib
import hmac
import html as htmllib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests
from flask import Flask, Response, g, jsonify, redirect, request, send_file, send_from_directory, session
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

import alerts
import db
import geo

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT = DATA_DIR / "snapshot.json"
THUMB_DIR = DATA_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
OG_FILE = DATA_DIR / "ogcache.json"            # article URL -> its og:image ("" = looked, none found)
OG_BUDGET = 20                                 # article pages fetched per refresh to find a missing thumbnail
THUMB_DNS_CHECK = os.environ.get("THUMB_DNS_CHECK", "1") != "0"      # only ever switched off for sandbox testing
Image.MAX_IMAGE_PIXELS = 30_000_000            # refuse decompression bombs
SCHEMA_VERSION = 5
SNAP_KEYS = ("victims", "gangs", "intel", "news", "last_refresh", "errors")

PASSWORD = os.environ.get("EVILEYE_PASSWORD", "")          # legacy password, and first-admin bootstrap
ADMIN_USER = os.environ.get("EVILEYE_ADMIN_USER", "admin")
STALE_MINUTES = int(os.environ.get("STALE_MINUTES", "30"))
LEGACY = not db.enabled()
UA = {"User-Agent": "EvilEye-Dashboard/2.0 (research)"}

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT")),
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14)

# ----------------------------------------------------------------- state
_lock = threading.Lock()
STATE = {"victims": [], "gangs": [], "intel": [], "news": [], "last_refresh": {}, "errors": {}}
_refreshing = False
_geo_running = False
GEO = geo.load_geo(DATA_DIR)
geo.load_status(DATA_DIR)
OVERRIDES = {}          # victim key -> {rocu, note, set_by}: manual placements made by an admin (PostgreSQL)
FLAGS = {}              # victim key -> {kind, rocu, note, flagged_by, flagged_at}: false positives flagged by any user
ALLOC = {}              # victim key -> {initials, allocated_by, allocated_at}: the live (not completed) Task marker, kept for the older allocate API
MARKER_TYPES = {}       # id -> {id, name, colour, builtin, active}
MARKERS = {}            # victim key -> [marker rows]: every marker (live or completed) on that victim


def _sector(a):
    """ransomware.live uses 'Not Found' (or nothing) when it has not classified a victim."""
    a = (a or "").strip()
    return "Unclassified" if a in ("", "Not Found", "Unknown") else a


def _load_snapshot():
    try:
        snap = json.loads(SNAPSHOT.read_text())
        if snap.get("schema") == SCHEMA_VERSION:
            STATE.update({k: snap[k] for k in SNAP_KEYS})
    except Exception:
        pass


_load_snapshot()


def _save_snapshot():
    SNAPSHOT.write_text(json.dumps({"schema": SCHEMA_VERSION, **{k: STATE[k] for k in SNAP_KEYS}}))


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _get(url, as_json=True):
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    return r.json() if as_json else r.text


def _year(item):
    for k in ("published", "discovered", "attackdate"):
        if item.get(k):
            return str(item[k])[:4]
    return "unknown"


def load_overrides():
    if LEGACY:
        return
    try:
        rows = db.many("select key, rocu, note, set_by, town from placement_overrides")
        OVERRIDES.clear()
        OVERRIDES.update({r["key"]: r for r in rows})
    except Exception as e:
        STATE["errors"]["overrides"] = str(e)


def load_marks():
    """Flags and allocations (PostgreSQL) into memory, like the manual placements."""
    if LEGACY:
        return
    try:
        FLAGS.clear()
        FLAGS.update({r["key"]: r for r in db.many("select key, kind, rocu, note, flagged_by, flagged_at from victim_flags")})
        # build the new picture first, then swap it in with one assignment each, so a request never sees a half-empty list
        global MARKER_TYPES, MARKERS, ALLOC
        types = {r["id"]: r for r in db.many("select id, name, colour, builtin, active from marker_types")}
        markers, alloc = {}, {}
        for r in db.many("select id, key, victim, type_id, initials, allocated_by, allocated_at, started_at, completed_at, updated_by, updated_at, marked_at from victim_markers order by id"):
            markers.setdefault(r["key"], []).append(r)
            if types.get(r["type_id"], {}).get("builtin") and not r["completed_at"] and r["initials"]:         # ALLOC: a live Task that somebody has
                alloc[r["key"]] = {"initials": r["initials"], "allocated_by": r["allocated_by"], "allocated_at": r["allocated_at"]}
        MARKER_TYPES, MARKERS, ALLOC = types, markers, alloc
    except Exception as e:
        STATE["errors"]["marks"] = str(e)


MANUAL = []             # breaches added by hand, as victim records (PostgreSQL manual_breaches)


def _manual_rec(r):
    """A victim record in the shape the feed's victims have, for a breach somebody entered."""
    d, made = r["occurred"], r["created_at"]
    seen = made if made.date() == d else datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)
    iso = seen.astimezone(timezone.utc).isoformat()
    return {"post_title": r["title"], "key": r["key"], "group": r["gang"] or "unknown", "activity": _sector(r["sector"]), "discovered": iso, "seen": iso,
            "year": str(d.year), "website": r["website"], "post_url": "", "description": r["description"], "reposts": 0, "data": r["data"],
            "data_basis": "stated" if r["data"] else "", "ransom": r["ransom"], "text_force": "", "text_term": "", "postcode_hint": "", "text_overseas": "",
            "overseas_weak": False, "registered_name": "", "town": r["town"], "manual_breach": r["id"], "added_by": r["created_by"], "entered_rocu": r["rocu"]}


def load_manual():
    if LEGACY:
        return
    global MANUAL
    try:
        MANUAL = [_manual_rec(r) for r in db.many("select * from manual_breaches order by id")]
    except Exception as e:
        STATE["errors"]["manual"] = str(e)


def _merge_manual(victims):
    """The feed's victims plus the ones entered by hand (a hand-entered breach never replaces a feed victim of the same name)."""
    feed = [v for v in victims if not v.get("manual_breach")]
    have = {v["key"] for v in feed}
    return feed + [dict(m) for m in MANUAL if m["key"] not in have]


def _apply_geo(victims):
    """Region, in order of trust: an admin's manual placement, an API result (postcode / Companies House),
    the place-name guess, a probable overseas head office, else Unplaced."""
    for v in victims:
        g = GEO.get(v["key"]) or {}
        ov = OVERRIDES.get(v["key"])
        v["manual"] = bool(ov)
        v["override_note"] = ov["note"] if ov else ""
        if ov:
            v.update(rocu=ov["rocu"], force="", region_source="manual", town=ov.get("town") or "",
                     registered_name=v.get("registered_name", ""))
        elif v.get("entered_rocu"):                        # the person who added it chose its region
            v.update(rocu=v["entered_rocu"], force="", region_source="entered", town=v.get("town", ""))
        elif g.get("status") == "placed":
            v.update(rocu=g["rocu"], force=g["force"], region_source=g.get("source", "api"), town=g.get("district") or "",
                     registered_name=g.get("company") or "")
        elif v.get("text_force"):
            v.update(force=v["text_force"], rocu=geo.rocu_of(v["text_force"]), region_source="text", town=v.get("text_term", ""))
        elif v.get("text_overseas"):
            v.update(force="", rocu=geo.OVERSEAS, region_source="text-overseas", town="")
        else:
            v.update(force="", rocu="Unplaced", region_source="", town="")
        # A user's false-positive flag takes a victim out of the region it was shown in (or out of the UK). It never
        # overrides an admin's manual placement, and "not in <region>" stops applying if the automatic region has moved on.
        fl = FLAGS.get(v["key"])
        v["flag"], v["flag_applied"] = None, False
        if fl:
            hit = not ov and (fl["kind"] == "not_uk" or v["rocu"] == fl["rocu"])
            v["flag"] = {"kind": fl["kind"], "region": fl["rocu"], "by": fl["flagged_by"], "note": fl["note"], "applied": hit,
                         "at": fl["flagged_at"].isoformat() if fl.get("flagged_at") else ""}
            if hit:
                v.update(rocu=geo.OVERSEAS if fl["kind"] == "not_uk" else "Unplaced", force="", region_source="flag", town="")
                v["flag_applied"] = True
        v["force_name"] = geo.FORCE_NAMES.get(v["force"], "")


# ----------------------------------------------------------------- ingest
def _abs_url(link, base):
    """RansomLook gives relative links ("/post/<id>"): make them point at its site, never at ours."""
    link = (link or "").strip()
    if not link:
        return ""
    u = urljoin(base, link)
    return u if urlparse(u).scheme in ("http", "https") else ""


def _plain(text, limit=300):
    """Tags removed, HTML entities decoded (some feeds double-encode them) and whitespace collapsed."""
    t = text or ""
    for _ in range(3):
        u = htmllib.unescape(t)
        if u == t:
            break
        t = u
    t = re.sub(r"&?#?;?x[0-9a-f]{2,4};|&#;x?[0-9a-f]*;?", "", t)       # malformed entities some feeds ship ("&#;x26;")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).strip()[:limit]


def _rss(url, source, kind, limit=25, skip=None):
    root = ET.fromstring(_get(url, False))
    out = []
    for it in root.iter("item"):
        title = _plain(it.findtext("title"), 300)
        if skip and skip(title):
            continue
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).isoformat()
        except Exception:
            pub = None
        out.append({"title": title, "source": source, "kind": kind, "url": _abs_url(it.findtext("link"), url),
                    "summary": _plain(it.findtext("description")), "published": pub, "extra": {}})
        if len(out) >= limit:
            break
    return out


# ---- dark web news: specialist sources first; general security press is kept only when it is dark-web relevant
NEWS_SOURCES = [   # id, feed, display name, weight
    ("darkwebinformer", "https://darkwebinformer.com/rss/", "Dark Web Informer", 3),
    ("databreaches", "https://databreaches.net/feed/", "DataBreaches.net", 2),
    ("hackread", "https://hackread.com/feed/", "Hackread", 1),
    ("therecord", "https://therecord.media/feed", "The Record", 1),
    ("thehackernews", "https://feeds.feedburner.com/TheHackersNews", "The Hacker News", 1),
    ("securityweek", "https://feeds.feedburner.com/securityweek", "SecurityWeek", 1),
    ("darkreading", "https://www.darkreading.com/rss.xml", "Dark Reading", 1),
    ("bleepingcomputer", "https://www.bleepingcomputer.com/feed/", "BleepingComputer", 1),
    ("krebs", "https://krebsonsecurity.com/feed/", "Krebs on Security", 1),
]
THEMES = [
    ("Ransomware", re.compile(r"ransomware|extortion|leak site|decrypt", re.I)),
    ("Data breach / leak", re.compile(r"data breach|breach|\bleak(ed|s)?\b|exposed|stolen data|sale claim|database", re.I)),
    ("Access & credentials", re.compile(r"initial access|credential|stealer log|combolist|infostealer|account takeover|access broker|\brdp\b", re.I)),
    ("Malware", re.compile(r"malware|loader|botnet|trojan|backdoor|stealer|\brat\b|wiper|spyware", re.I)),
    ("Forums & marketplaces", re.compile(r"dark ?web|darknet|\bforum\b|marketplace|breachforums|telegram|underground|for sale|selling", re.I)),
    ("Law enforcement", re.compile(r"arrest|seiz(ed|ure)|takedown|sentenc|extradit|\bcharged\b|indict|europol|\bfbi\b|national crime agency", re.I)),
    ("Vulnerabilities", re.compile(r"cve-\d|zero-day|0-day|vulnerabilit|exploit", re.I)),
]
DARK_RE = re.compile(r"dark ?web|darknet|ransomware|extortion|\bleak(ed|s)?\b|breach|stolen|threat actor|data theft|sale claim|"
                     r"for sale|infostealer|stealer|initial access|underground|telegram|marketplace|credential", re.I)
UK_NEWS_RE = re.compile(r"\b(UK|U\.K\.|United Kingdom|Britain|British|England|Scotland|Wales|Northern Ireland|NHS|NCSC|"
                        r"National Crime Agency|Companies House)\b|\.co\.uk")
# ransomware.live lists some groups under ordinary words. These only count when capitalised AND the text is about ransomware...
COMMON_WORDS = {"play", "cactus", "medusa", "hunters", "royal", "rhysida", "everest", "sarcoma", "abyss", "snake", "babuk", "brotherhood",
                "blackout", "bluesky", "cyclops", "direwolf", "eclipse", "embargo", "endurance", "entropy", "exorcist", "genesis",
                "inception", "insomnia", "interlock", "kryptos", "deadlock", "barracuda", "argonauts", "chaos", "ember"}
# ...and these catalogue entries are not group names at all
NOT_GROUPS = {"unknown", "underground", "test", "none", "other", "leaked data", "leakeddata", "data leak", "dataleak", "cyberfeeddigest"}


# ---- thumbnails: the server fetches, checks, shrinks and caches each image; browsers never contact the news sites
def _public_url(url):
    """True only for http(s) URLs whose host is a public address (no localhost, private, link-local or metadata IPs)."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return ipaddress.ip_address(host).is_global           # an IP literal must itself be public
    except ValueError:
        pass
    if not THUMB_DNS_CHECK:
        return True
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def _safe_get(url, max_bytes, accept, truncate=False, timeout=8):
    """GET with SSRF protection: public hosts only, re-checked on every redirect (max 4), size capped, no cookies.
    Returns (content_type, bytes) or None."""
    for _ in range(5):
        if not _public_url(url):
            return None
        try:
            r = requests.get(url, headers={**UA, "Accept": accept}, timeout=timeout, stream=True, allow_redirects=False)
        except requests.RequestException:
            return None
        if r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get("Location", ""))
            r.close()
            continue
        if not r.ok:
            r.close()
            return None
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        buf = bytearray()
        try:
            for chunk in r.iter_content(32768):
                buf += chunk
                if len(buf) > max_bytes:
                    if truncate:
                        break
                    return None
        except requests.RequestException:
            return None
        finally:
            r.close()
        return ctype, bytes(buf)
    return None


MEDIA = "{http://search.yahoo.com/mrss/}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


def _feed_image(it):
    """The post's own image from the feed entry (media:content / media:thumbnail / enclosure / first <img>), or ''."""
    for tag in ("content", "thumbnail"):
        for el in it.findall(MEDIA + tag):
            u, medium, typ = el.get("url"), el.get("medium") or "", el.get("type") or ""
            if u and (tag == "thumbnail" or (medium in ("", "image") and (not typ or typ.startswith("image")))):
                return u
    for el in it.findall("enclosure"):
        if (el.get("type") or "").startswith("image") and el.get("url"):
            return el.get("url")
    html = (it.findtext(CONTENT_NS + "encoded") or "") + (it.findtext("description") or "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', html)
    return m.group(1) if m and not re.search(r"1x1|pixel|spacer|tracking|\.gif", m.group(1), re.I) else ""


def _og_image(article_url):
    """Fallback for feeds without images: the article page's og:image / twitter:image."""
    got = _safe_get(article_url, 400_000, "text/html", truncate=True)
    if not got or "html" not in got[0]:
        return ""
    page = got[1].decode("utf-8", errors="ignore")
    for pat in (r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']'):
        m = re.search(pat, page, re.I)
        if m:
            u = urljoin(article_url, m.group(1).replace("&amp;", "&"))
            return u if urlparse(u).scheme in ("http", "https") else ""
    return ""


def _news_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def _news_feed(fid, url, name, weight):
    root = ET.fromstring(_get(url, False))
    out = []
    for it in root.iter("item"):
        title = _plain(it.findtext("title"), 300)
        link = _abs_url(it.findtext("link"), url)          # http(s) only, never relative, never javascript:
        if not title or not link:
            continue
        try:
            d = parsedate_to_datetime(it.findtext("pubDate"))
            pub = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
        except Exception:
            pub = None
        desc = it.findtext("description") or ""
        summary = _plain(desc, 320)
        cats = [c.text.strip() for c in it.findall("category") if c.text][:5]
        out.append({"title": title, "url": link, "source": fid, "source_name": name, "weight": weight,
                    "published": pub, "summary": summary, "cats": cats, "thumb": _feed_image(it)})
    return out


def _group_matcher(gangs):
    """Find ransomware-group names in text. Short or everyday-word names ('Play', 'Medusa') only count when they
    are capitalised and the text is about ransomware, so ordinary sentences do not match."""
    sure, tricky, canon = [], [], {}
    for g_ in gangs:
        n = g_["name"]
        if n.lower() in NOT_GROUPS:
            continue
        for variant in {n, re.sub(r"\d+$", "", n)}:
            if len(variant) < 4 or variant.lower() in NOT_GROUPS:
                continue
            canon[variant.lower()] = n
            (tricky if len(variant) < 7 or variant.lower() in COMMON_WORDS else sure).append(variant)
    pat = lambda names, flags: re.compile(r"(?<![\w-])(" + "|".join(re.escape(x) for x in sorted(names, key=len, reverse=True)) + r")(?![\w-])", flags) if names else None
    sure_re = pat(sure, re.I)
    tricky_re = pat([t.capitalize() for t in tricky] + [t for t in tricky if t != t.lower()], 0)
    ctx = re.compile(r"ransom|extortion|affiliate|leak site|gang", re.I)

    def find(text):
        found = []
        if sure_re:
            found += [canon.get(m.group(1).lower()) for m in sure_re.finditer(text)]
        if tricky_re and ctx.search(text):
            found += [canon.get(m.group(1).lower()) for m in tricky_re.finditer(text)]
        out = []
        for f in found:
            if f and f not in out:
                out.append(f)
        return out[:3]
    return find


def _build_news(gangs):
    """Fetch every news feed, keep the dark-web relevant items, tag themes / ransomware groups / UK mentions."""
    find_groups = _group_matcher(gangs)
    items, errs, stamps = {}, {}, {}
    for fid, url, name, weight in NEWS_SOURCES:
        try:
            for n in _news_feed(fid, url, name, weight):
                text = n["title"] + ". " + n["summary"] + " " + " ".join(n["cats"])
                if fid != "darkwebinformer" and not DARK_RE.search(text):
                    continue                                    # e.g. a Windows update story: not dark web news
                themes = [t for t, rx in THEMES if rx.search(text)]
                n.update(themes=themes, groups=find_groups(text), uk=bool(UK_NEWS_RE.search(text)),
                         score=n.pop("weight") + min(3, len(themes)))
                k = geo.norm(n["title"])
                if k not in items or n["score"] > items[k]["score"]:
                    items[k] = n
            stamps["news_" + fid] = _now_iso()
        except Exception as e:
            errs["news_" + fid] = str(e)
    out = sorted(items.values(), key=lambda n: n["published"] or "", reverse=True)[:300]
    # thumbnails: the feed's own image, else the article's og:image (a few pages per refresh, remembered on disk)
    try:
        og = json.loads(OG_FILE.read_text())
    except Exception:
        og = {}
    budget = OG_BUDGET
    for n in out:
        n["id"] = _news_id(n["url"])
        if not n.get("thumb") and n["url"] in og:
            n["thumb"] = og[n["url"]]
        elif not n.get("thumb") and budget > 0:
            budget -= 1
            og[n["url"]] = n["thumb"] = _og_image(n["url"])
    try:
        OG_FILE.write_text(json.dumps(dict(list(og.items())[-600:])))
        keep = {n["id"] for n in out}
        for f in THUMB_DIR.glob("*"):                          # drop cached thumbnails of items that have aged out
            if f.stem not in keep:
                f.unlink(missing_ok=True)
    except Exception as e:
        errs["thumbs"] = str(e)
    return out, errs, stamps


_SZ = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(KB|MB|GB|TB|PB|kilobytes?|megabytes?|gigabytes?|terabytes?|petabytes?)\b", re.I)


def _size_text(m):
    n = m.group(1).replace(",", "")
    try:
        if float(n) <= 0:
            return ""
    except ValueError:
        return ""
    return f"{n} {m.group(2)[0].upper()}B"


def _data_amount(raw, desc):
    """How much data the post claims: the feed's own data_size when it starts with a clean size, else a size quoted in the
    post text. Returns (text, basis): basis is "stated", "post" (quoted in the text) or "" (not stated)."""
    m = _SZ.match((raw or "").strip())
    if m and _size_text(m):
        return _size_text(m), "stated"
    for m in _SZ.finditer(desc or ""):
        if _size_text(m):
            return _size_text(m), "post"
    return "", ""


def _ingest():
    global _refreshing
    errors, stamps = {}, {}
    new = {k: STATE[k] for k in ("victims", "gangs", "intel", "news")}

    # ransomware.live: UK victims. The same company claimed again by the same group is ONE victim (newest sighting).
    try:
        rows = {}
        for v in _get("https://api.ransomware.live/v2/countryvictims/GB"):
            desc = v.get("description") or ""
            title = v.get("post_title") or ""
            hit = geo.place_text(title + ". " + desc)
            pc = geo.postcode_in(desc)
            site = (v.get("website") or "").lower().rstrip("/")
            over = None if (hit or pc) else geo.detect_overseas(title + ". " + desc, v.get("website") or "")
            weak = False
            if not over and not hit and not pc and not site.endswith(".uk"):
                over = geo.bare_foreign(desc)                      # e.g. a description that is just "United States"
                weak = bool(over)
            data_text, data_basis = _data_amount(v.get("data_size"), desc)
            ransom = str(v.get("ransom") or "").strip()[:40]
            if ransom.lower() in ("0", "n/a", "none", "null"):
                ransom = ""
            rec = {
                "post_title": title, "key": geo.norm(title), "group": v.get("group_name") or "unknown",
                "activity": _sector(v.get("activity")), "discovered": v.get("published") or v.get("discovered"),
                "seen": v.get("discovered") or v.get("published") or "", "year": _year(v),
                "website": (v.get("website") or "").strip(), "post_url": v.get("post_url") or "",
                "description": desc[:700], "reposts": 0, "data": data_text, "data_basis": data_basis, "ransom": ransom,
                "text_force": hit[0] if hit else "", "text_term": hit[1] if hit else "",
                "postcode_hint": pc, "text_overseas": over or "", "overseas_weak": weak, "registered_name": "", "town": ""}
            k = (rec["key"] or title, rec["group"])
            old = rows.get(k)
            if old:
                rec["reposts"] = old["reposts"] + 1
                if str(old["seen"]) > str(rec["seen"]):
                    old["reposts"] += 1
                    continue
            rows[k] = rec
        load_manual()
        victims = _merge_manual(list(rows.values()))
        load_overrides()
        load_marks()
        _apply_geo(victims)
        new["victims"] = victims
        stamps["rl_victims_gb"] = _now_iso()
    except Exception as e:
        errors["rl_victims_gb"] = str(e)

    # leak-site posts and press mentions feed the Alerts tab
    intel = []
    try:
        for v in _get("https://api.ransomware.live/v2/recentvictims"):
            if v.get("press"):
                intel.append({"title": v.get("victim") or "", "source": "ransomware.live", "kind": "press",
                              "summary": (v.get("description") or "")[:260], "url": _abs_url(v.get("url"), "https://www.ransomware.live/"),
                              "published": v.get("discovered"), "extra": {}})
        stamps["rl_recent"] = _now_iso()
    except Exception as e:
        errors["rl_recent"] = str(e)
    rlook_groups = set()
    try:
        n_rl = 0
        for v in _get("https://www.ransomlook.io/api/recent/100"):
            rlook_groups.add(v.get("group_name") or "")
            if n_rl < 30:
                n_rl += 1
                intel.append({"title": v.get("post_title") or "", "source": "ransomlook", "kind": "leak-post",
                              "summary": (v.get("description") or "")[:260], "url": _abs_url(v.get("link"), "https://www.ransomlook.io/"),
                              "published": (v.get("discovered") or "").replace(" ", "T"), "extra": {}})
        stamps["ransomlook"] = _now_iso()
    except Exception as e:
        errors["ransomlook"] = str(e)

    # gangs: ransomware.live groups + deepdarkCTI + RansomLook names
    try:
        gangs = {}
        for gr in _get("https://api.ransomware.live/v2/groups"):
            name = gr.get("name") or ""
            gangs[name.lower()] = {
                "name": name, "altname": gr.get("altname"), "description": gr.get("description") or "",
                "leak_sites": [{"slug": l.get("slug"), "fqdn": l.get("fqdn"), "available": l.get("available")}
                               for l in (gr.get("locations") or [])],
                "sources": ["ransomware.live"], "added_date": gr.get("added_date")}
        try:
            md = _get("https://raw.githubusercontent.com/fastfire/deepdarkCTI/main/ransomware_gang.md", False)
            for line in md.splitlines():
                m = re.match(r"\|\[([^\]]+)\]\(([^)]+)\)\|(ONLINE|OFFLINE|VALID|[A-Z ]*)\|", line)
                if not m or "list" in m.group(1).lower():
                    continue
                name = re.sub(r"\s*\((Deep|Victims page)[^)]*\)", "", m.group(1)).strip()
                gr = gangs.setdefault(name.lower(), {"name": name, "altname": None, "description": "", "leak_sites": [],
                                                    "sources": [], "added_date": None})
                if "deepdarkcti" not in gr["sources"]:
                    gr["sources"].append("deepdarkcti")
                gr["leak_sites"].append({"slug": m.group(2), "fqdn": None, "status": m.group(3) or None})
            stamps["deepdarkcti"] = _now_iso()
        except Exception as e:
            errors["deepdarkcti"] = str(e)
        for n in rlook_groups:
            if n:
                gr = gangs.setdefault(n.lower(), {"name": n, "altname": None, "description": "", "leak_sites": [],
                                                 "sources": [], "added_date": None})
                if "ransomlook" not in gr["sources"]:
                    gr["sources"].append("ransomlook")
        new["gangs"] = sorted(gangs.values(), key=lambda x: x["name"].lower())
        stamps["rl_groups"] = _now_iso()
    except Exception as e:
        errors["rl_groups"] = str(e)

    # alerts and advisories
    def feed(name, fn):
        try:
            intel.extend(fn())
            stamps[name] = _now_iso()
        except Exception as e:
            errors[name] = str(e)

    feed("ncsc", lambda: [dict(i, kind=(i["url"].split("/")[3] if i["url"].count("/") >= 4 else "ncsc"))
                          for i in _rss("https://www.ncsc.gov.uk/api/1/services/v1/all-rss-feed.xml", "ncsc", "ncsc", 25)])

    def kev():
        k = _get("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
        vulns = sorted(k.get("vulnerabilities", []), key=lambda x: x.get("dateAdded", ""), reverse=True)[:100]
        return [{"title": x.get("vulnerabilityName") or x.get("cveID"), "source": "cisa", "kind": "KEV",
                 "summary": x.get("shortDescription") or "", "published": x.get("dateAdded"),
                 "url": "https://nvd.nist.gov/vuln/detail/" + (x.get("cveID") or ""),
                 "extra": {"cve": x.get("cveID"), "vendor": x.get("vendorProject")}} for x in vulns]
    feed("cisa_kev", kev)
    feed("sans_isc", lambda: _rss("https://isc.sans.edu/rssfeed.xml", "sans-isc", "diary", 10,
                                  skip=lambda t: "stormcast" in t.lower()))

    def hibp():
        b = sorted(_get("https://haveibeenpwned.com/api/v3/breaches"), key=lambda x: x.get("AddedDate", ""), reverse=True)
        out = []
        for x in b[:40]:
            if x.get("IsFabricated") or x.get("IsSpamList"):
                continue
            out.append({"title": f'{x["Title"]}: {x.get("PwnCount", 0):,} accounts', "source": "hibp", "kind": "breach",
                        "summary": ("Data: " + ", ".join(x.get("DataClasses", [])[:6]) + ". " +
                                    re.sub(r"<[^>]+>", "", x.get("Description", "")))[:300],
                        "url": "https://haveibeenpwned.com/PwnedWebsites#" + x.get("Name", ""),
                        "published": x.get("AddedDate"), "extra": {"vendor": x.get("Domain") or ""}})
        return out
    feed("hibp", hibp)
    if intel:
        intel.sort(key=lambda i: str(i.get("published") or ""), reverse=True)
        new["intel"] = intel

    # dark web news
    news, nerr, nstamps = _build_news(new["gangs"])
    errors.update(nerr)
    stamps.update(nstamps)
    if news:
        new["news"] = news

    with _lock:
        STATE.update(new)
        STATE["last_refresh"] = {**STATE.get("last_refresh", {}), **stamps}
        STATE["errors"] = errors
        try:
            _save_snapshot()
        except Exception as e:
            STATE["errors"]["snapshot"] = str(e)
    _refreshing = False
    _place()
    _run_alerts()


def _place():
    """Bounded API placement of victims into ROCU regions (resumes where the last refresh stopped)."""
    global _geo_running
    if _geo_running or not STATE["victims"]:
        return
    _geo_running = True
    try:
        geo.geo_pass(STATE["victims"], GEO, DATA_DIR)
        _apply_geo(STATE["victims"])
        _save_snapshot()
    except Exception as e:
        STATE["errors"]["geo"] = str(e)
    finally:
        _geo_running = False


def start_refresh():
    global _refreshing
    with _lock:
        if _refreshing:
            return False
        _refreshing = True
    threading.Thread(target=_ingest, daemon=True).start()
    return True


def is_stale():
    last = STATE.get("last_refresh", {}).get("rl_victims_gb")
    if not last:
        return True
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60 > STALE_MINUTES


# ----------------------------------------------------------------- users, sessions, activity
DUMMY_HASH = generate_password_hash("not-a-real-password")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
EVENTS_FROM_CLIENT = {"tab_view", "search", "filter", "region_change", "copy_leak_site", "export", "modal_open", "victim_view",
                     "screen_wake_on", "screen_wake_off", "screen_wake_failed", "link_click"}
_rate = defaultdict(deque)


def _pwv(h):
    return hashlib.sha256(h.encode()).hexdigest()[:16]


CLIENT_IP_HEADER = os.environ.get("CLIENT_IP_HEADER", "X-Real-IP").strip()   # set by Railway's edge; "" = ignore it
PROXY_HOPS = max(1, int(os.environ.get("PROXY_HOPS", "2") or 2))             # entries our own infrastructure adds to the END of X-Forwarded-For (Railway: "client, edge")


def _valid_ip(v):
    try:
        return str(ipaddress.ip_address((v or "").strip()))
    except ValueError:
        return None


def _ip():
    """The client's address, for the activity log and the sign-in lockout.

    Railway's edge overwrites X-Real-IP and X-Forwarded-For (checked against the live site: forged values never
    arrive), and X-Real-IP is the visitor's address, so that is preferred. The fallback is X-Forwarded-For, which
    on Railway reads "visitor, edge" (the edge adds itself last), so the visitor is PROXY_HOPS=2 from the right.
    Counting from the right rather than taking the leftmost entry means entries a visitor puts at the front are
    never believed, even on a host that appends instead of overwriting. Anything that is not a valid IP address is
    ignored, and with no usable proxy header (a local copy) the connection's own address is used."""
    ip = _valid_ip(request.headers.get(CLIENT_IP_HEADER)) if CLIENT_IP_HEADER else None
    if not ip:
        parts = [p for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        ip = _valid_ip(parts[-PROXY_HOPS]) if len(parts) >= PROXY_HOPS else None
    return ip or _valid_ip(request.remote_addr) or "unknown"


def log(event, detail="", user=None, username=None):
    """Write an activity row. Never raises: logging must not break a request."""
    if LEGACY:
        return
    try:
        u = user if user is not None else getattr(g, "user", None)
        db.run("insert into activity (user_id, username, event, detail, ip) values (%s,%s,%s,%s,%s)",
               (u["id"] if u else None, username if username is not None else (u["username"] if u else ""),
                event, str(detail)[:300], _ip()))
    except Exception:
        pass


def bootstrap_admin():
    """First start: if there are no users and EVILEYE_PASSWORD is set, create the first admin from it."""
    if LEGACY or not PASSWORD:
        return
    if db.one("select count(*) as c from users")["c"] == 0:
        db.run("insert into users (username, password_hash, role, created_by, must_change) values (%s,%s,'admin','bootstrap',true)",
               (ADMIN_USER, generate_password_hash(PASSWORD)))


if not LEGACY:
    try:
        db.init()
        bootstrap_admin()
        load_overrides()
        load_marks()
        load_manual()
    except Exception as e:                      # keep serving; login reports the database error
        STATE["errors"]["db"] = str(e)
if STATE["victims"] or MANUAL:
    STATE["victims"] = _merge_manual(STATE["victims"])
    _apply_geo(STATE["victims"])


def current_user():
    if hasattr(g, "user"):
        return g.user
    g.user = None
    uid = session.get("uid")
    if LEGACY:
        if uid == "legacy" and session.get("ok"):
            g.user = {"id": 0, "username": "admin", "role": "admin", "region": session.get("region", ""), "legacy": True,
                      "prev_visit": session.get("prev_visit"), "onboarded_at": session.get("onboarded"), "must_change": False,
                      "theme": session.get("theme", "")}
    elif isinstance(uid, int):
        try:
            row = db.one("select id, username, role, region, active, password_hash, prev_visit, onboarded_at, must_change, initials, theme, email, email_alerts from users where id=%s", (uid,))
            if row and row["active"] and session.get("pwv") == _pwv(row["password_hash"]):
                g.user = row
        except Exception:
            g.user = None
    return g.user


@app.before_request
def guard():
    p = request.path
    if p in ("/login", "/healthz", "/favicon.ico") or p.startswith("/static/"):
        return None
    user = current_user()
    if not user:
        if p.startswith("/api/") or p == "/health":
            return jsonify(error="unauthorized"), 401
        want = _open_param(request.args.get("open"))
        return redirect("/login" + (f"?open={quote(want, safe='')}" if want and p == "/" else ""))
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and p.startswith("/api/") \
            and request.headers.get("X-Requested-With") != "fetch":
        return jsonify(error="bad request"), 400          # CSRF guard: browsers cannot add this cross-site
    if user.get("must_change") and p not in ("/", "/logout", "/health", "/api/me", "/api/me/password"):
        return jsonify(error="Set your own password first.", code="password_change_required"), 403
    return None


# ----------------------------------------------------------------- security headers
CSP_MODE = os.environ.get("CSP_MODE", "enforce").strip().lower()       # enforce | report-only | off  (an emergency switch, see the README)
CSP = "; ".join(["default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",   # inline styles only: a few style="..." attributes
                 "img-src 'self'", "font-src 'self'", "connect-src 'self'", "object-src 'none'", "base-uri 'none'",
                 "form-action 'self'", "frame-ancestors 'none'"])
PERMISSIONS = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), accelerometer=(), gyroscope=(), magnetometer=()"   # wake lock and clipboard stay allowed


@app.after_request
def security_headers(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")                              # nobody may put the dashboard in a frame (clickjacking)
    h.setdefault("Referrer-Policy", "no-referrer")                       # outside links never learn where the click came from
    h.setdefault("Permissions-Policy", PERMISSIONS)
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if CSP_MODE != "off":
        h.setdefault("Content-Security-Policy-Report-Only" if CSP_MODE == "report-only" else "Content-Security-Policy", CSP)
    if not request.path.startswith("/static/"):
        h.setdefault("Cache-Control", "no-store")                        # pages and data are never kept by the browser or a proxy
    return resp


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or u["role"] != "admin":
            return jsonify(error="admin only"), 403
        if LEGACY:
            return jsonify(error="Database not connected: user management is unavailable."), 503
        return fn(*a, **kw)
    return wrapper


LOCK_PAIR, LOCK_USER, LOCK_IP = 6, 30, 60     # wrong passwords in 15 minutes: one name from one address / one name overall / one address overall


def _locked(username, ip):
    """Should this sign-in attempt be refused without even checking the password?

    Only genuine wrong-password attempts count (not the 'locked out' rows themselves, so hammering a locked
    account does not extend the lock). The tight limit is per name AND address: someone guessing at 'admin'
    from their own address is stopped after 6 tries, but the real admin, coming from a different address, is
    not locked out by them. The two broad limits catch guessing spread over many addresses or many names; they
    do not apply to an address this person has successfully signed in from in the last 30 days, so they cannot
    be used to keep a real user out."""
    row = db.one("select count(*) filter (where lower(username)=lower(%s) and ip=%s) as pair, "
                 "count(*) filter (where lower(username)=lower(%s)) as by_user, "
                 "count(*) filter (where ip=%s) as by_ip from activity "
                 "where event='login_failed' and detail in ('bad credentials','disabled account') "
                 "and ts > now() - interval '15 minutes'", (username, ip, username, ip))
    if row["pair"] >= LOCK_PAIR:
        return True
    if row["by_user"] >= LOCK_USER or row["by_ip"] >= LOCK_IP:
        known = db.one("select 1 as k from activity where event='login' and lower(username)=lower(%s) and ip=%s "
                       "and ts > now() - interval '30 days' limit 1", (username, ip))
        return not known
    return False


def _open_param(raw):
    """The breach a link from an email wants opened: plain text only, 200 characters at most, else nothing."""
    s = re.sub(r"\s+", " ", raw or "").strip()[:200]
    return "" if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in s) else s


@app.get("/login")
def login_page():
    page = (BASE / "login.html").read_text()
    want = _open_param(request.args.get("open"))
    page = page.replace("<!--open-->", f'<input type="hidden" name="open" value="{htmllib.escape(want, quote=True)}">' if want else "")
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()[:64]
    pw = request.form.get("password") or ""
    want = _open_param(request.form.get("open"))
    after = "/?open=" + quote(want, safe="") if want else "/"
    again = lambda n: f"/login?error={n}" + (f"&open={quote(want, safe='')}" if want else "")
    if LEGACY:
        if PASSWORD and hmac.compare_digest(pw.encode(), PASSWORD.encode()):
            session.clear()
            session.update(uid="legacy", ok=True)
            session.permanent = True
            return redirect(after)
        time.sleep(1)
        return redirect(again(1))
    try:
        bootstrap_admin()
        if _locked(username, _ip()):
            log("login_failed", "locked out (too many attempts)", username=username)
            time.sleep(1)
            return redirect(again(2))
        row = db.one("select * from users where lower(username)=lower(%s)", (username,))
        ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, pw) and bool(row) and row["active"]
        if not ok:
            log("login_failed", "disabled account" if row and not row["active"] else "bad credentials", username=username)
            time.sleep(1)
            return redirect(again(1))
        session.clear()
        session.update(uid=row["id"], pwv=_pwv(row["password_hash"]))
        session.permanent = True
        db.run("update users set last_login=now() where id=%s", (row["id"],))
        log("login", "", user=row)
        return redirect(after)
    except Exception:
        return redirect(again(3))


@app.get("/logout")
def logout():
    u = current_user()
    if u:
        log("logout")
    session.clear()
    return redirect("/login")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    resp = send_from_directory(BASE / "static", "index.html")
    resp.headers["Cache-Control"] = "no-store"                           # Flask's own default is "no-cache", which still lets a copy be kept
    return resp


@app.get("/health")
def health():
    return jsonify(errors=STATE.get("errors") or None, last_refresh=STATE.get("last_refresh", {}),
                   refreshing=_refreshing, placing=_geo_running)


def touch_visit(u):
    """Record this page open and return when the user's PREVIOUS visit was. A visit starts when a page is opened more
    than 30 minutes after the last one, so reloading does not reset "new since your last visit"."""
    now = datetime.now(timezone.utc)
    if LEGACY:
        last, prev = session.get("last_visit"), session.get("prev_visit")
        if not last or (now - (_ts(last) or now)).total_seconds() > 1800:
            prev = last
        session["prev_visit"], session["last_visit"] = prev, now.isoformat()
        u["prev_visit"] = prev
        return prev
    row = db.run("""update users set prev_visit = case when last_visit is null or last_visit < now() - interval '30 minutes'
                    then last_visit else prev_visit end, last_visit = now() where id=%s returning prev_visit""", (u["id"],))
    u["prev_visit"] = row["prev_visit"] if row else None
    return u["prev_visit"].astimezone(timezone.utc).isoformat() if u["prev_visit"] else None


@app.post("/api/open")
def api_open():
    log("page_open")
    prev = None
    try:
        prev = touch_visit(current_user())
    except Exception:
        pass
    started = start_refresh() if is_stale() else False
    return jsonify(refreshing=_refreshing, started=started, stale=is_stale(), prev_visit=prev)


@app.post("/api/refresh")
def api_refresh():
    log("refresh", "manual")
    return jsonify(started=start_refresh(), refreshing=_refreshing), 202


# ---- current user
@app.get("/api/me")
def api_me():
    u = current_user()
    p = _prev_visit()
    try:
        groups = [] if LEGACY else _user_group_names(u["id"])
    except Exception:
        groups = []
    return jsonify(username=u["username"], role=u["role"], region=u["region"], database=not LEGACY, groups=groups,
                   regions=geo.FILTER_REGIONS, initials=u.get("initials") or "", theme=u.get("theme") or "",
                   email=(u.get("email") or ""), email_alerts=bool(u.get("email_alerts", True)), prev_visit=p.isoformat() if p else None, onboarded=bool(u.get("onboarded_at")),
                   must_change=bool(u.get("must_change")))


@app.put("/api/me/theme")
def api_me_theme():
    """The person's chosen look, kept on their account so it follows them to other devices."""
    t = (request.get_json(silent=True) or {}).get("theme")
    if t not in ("light", "dark"):
        return jsonify(error="Choose light or dark."), 400
    if LEGACY:
        session["theme"] = t
    else:
        db.run("update users set theme=%s where id=%s", (t, current_user()["id"]))
    return jsonify(ok=True, theme=t)


@app.post("/api/me/onboarded")
def api_me_onboarded():
    """The welcome tour has been finished (or skipped): do not show it again unless the user asks."""
    u = current_user()
    skipped = bool((request.get_json(silent=True) or {}).get("skipped"))
    if LEGACY:
        session["onboarded"] = datetime.now(timezone.utc).isoformat()
    else:
        db.run("update users set onboarded_at=now() where id=%s", (u["id"],))
    log("onboarding_skipped" if skipped else "onboarding_done", u["region"] or "All UK")
    return jsonify(ok=True)


@app.get("/api/regions")
def api_regions():
    """The ROCU regions with the police forces each one covers and how many victims are on record (for the region picker)."""
    by = Counter(v["rocu"] for v in STATE["victims"])
    out = [{"name": r, "forces": [geo.FORCE_NAMES[f] for f in fs], "count": by.get(r, 0)} for r, fs in geo.ROCU_FORCES.items()]
    out.append({"name": "Scotland", "forces": [geo.FORCE_NAMES["police-scotland"]], "count": by.get("Scotland", 0)})
    out.append({"name": "Northern Ireland", "forces": [geo.FORCE_NAMES["northern-ireland"]], "count": by.get("Northern Ireland", 0)})
    return jsonify(regions=out, total=sum(v["count"] for v in out))


@app.put("/api/me/region")
def api_me_region():
    u = current_user()
    r = (request.get_json(silent=True) or {}).get("region", "")
    if r and r not in geo.FILTER_REGIONS:
        return jsonify(error="unknown region"), 400
    if LEGACY:
        session["region"] = r
    else:
        db.run("update users set region=%s where id=%s", (r, u["id"]))
    log("region_change", r or "All UK")
    return jsonify(region=r)


@app.post("/api/me/password")
def api_me_password():
    u = current_user()
    if LEGACY:
        return jsonify(error="Database not connected."), 503
    b = request.get_json(silent=True) or {}
    row = db.one("select password_hash from users where id=%s", (u["id"],))
    if not check_password_hash(row["password_hash"], b.get("current", "")):
        log("password_change_failed", "wrong current password")
        return jsonify(error="Current password is wrong."), 400
    new = b.get("new", "")
    if len(new) < 10:
        return jsonify(error="New password must be at least 10 characters."), 400
    if new == b.get("current", ""):
        return jsonify(error="Your new password must be different from the current one."), 400
    if u["username"].lower() in new.lower():
        return jsonify(error="Your password must not contain your username."), 400
    h = generate_password_hash(new)
    db.run("update users set password_hash=%s, must_change=false where id=%s", (h, u["id"]))
    session["pwv"] = _pwv(h)
    log("password_change", "first sign-in: chose own password" if u.get("must_change") else "own password")
    return jsonify(ok=True)


@app.post("/api/activity")
def api_activity():
    u = current_user()
    b = request.get_json(silent=True) or {}
    ev = b.get("event")
    if ev not in EVENTS_FROM_CLIENT:
        return jsonify(error="unknown event"), 400
    q = _rate[u["id"]]
    now = time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 120:
        return jsonify(error="rate limited"), 429
    q.append(now)
    log(ev, str(b.get("detail", ""))[:300])
    return jsonify(ok=True)


# ---- admin: users and activity
def _admin_count_excluding(uid):
    return db.one("select count(*) as c from users where role='admin' and active and id<>%s", (uid,))["c"]


@app.get("/api/admin/users")
@admin_required
def admin_users():
    rows = db.many("""select u.id, u.username, u.role, u.region, u.active, u.created_at, u.created_by, u.last_login, u.email, u.email_alerts,
                      (select count(*) from activity a where a.user_id = u.id) as events,
                      (select max(a.ts) from activity a where a.user_id = u.id) as last_activity, u.must_change
                      from users u order by lower(u.username)""")
    gids = defaultdict(list)
    for m in db.many("select user_id, group_id from user_group_members order by group_id"):
        gids[m["user_id"]].append(m["group_id"])
    return jsonify(users=[{**r, "created_at": r["created_at"].isoformat(),
                           "last_login": r["last_login"].isoformat() if r["last_login"] else None,
                           "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None,
                           "group_ids": gids.get(r["id"], [])} for r in rows],
                   groups=_groups_payload(), regions=geo.FILTER_REGIONS)


@app.post("/api/admin/users")
@admin_required
def admin_user_create():
    b = request.get_json(silent=True) or {}
    username, pw = (b.get("username") or "").strip(), b.get("password") or ""
    role, region = b.get("role", "user"), b.get("region", "")
    if not USERNAME_RE.match(username):
        return jsonify(error="Username must be 3-32 characters: letters, numbers, dot, dash, underscore."), 400
    if len(pw) < 10:
        return jsonify(error="Password must be at least 10 characters."), 400
    if role not in ("admin", "user"):
        return jsonify(error="Role must be admin or user."), 400
    if region and region not in geo.FILTER_REGIONS:
        return jsonify(error="Unknown region."), 400
    if db.one("select 1 as x from users where lower(username)=lower(%s)", (username,)):
        return jsonify(error="That username already exists."), 409
    row = db.run("insert into users (username, password_hash, role, region, created_by, must_change) values (%s,%s,%s,%s,%s,true) returning id",
                 (username, generate_password_hash(pw), role, region, current_user()["username"]))
    log("user_create", f"{username} ({role})")
    return jsonify(id=row["id"]), 201


@app.patch("/api/admin/users/<int:uid>")
@admin_required
def admin_user_update(uid):
    me = current_user()
    b = request.get_json(silent=True) or {}
    row = db.one("select * from users where id=%s", (uid,))
    if not row:
        return jsonify(error="No such user."), 404
    sets, vals, notes = [], [], []
    if "password" in b:
        if len(b["password"]) < 10:
            return jsonify(error="Password must be at least 10 characters."), 400
        sets.append("password_hash=%s")
        vals.append(generate_password_hash(b["password"]))
        sets.append("must_change=true")
        notes.append("password reset (user must choose their own)")
    if "role" in b:
        if b["role"] not in ("admin", "user"):
            return jsonify(error="Role must be admin or user."), 400
        if b["role"] != "admin" and row["role"] == "admin" and (uid == me["id"] or _admin_count_excluding(uid) == 0):
            return jsonify(error="You cannot remove the last active admin."), 400
        sets.append("role=%s")
        vals.append(b["role"])
        notes.append(f"role={b['role']}")
    if "active" in b:
        if not b["active"] and row["role"] == "admin" and (uid == me["id"] or _admin_count_excluding(uid) == 0):
            return jsonify(error="You cannot disable the last active admin (or yourself)."), 400
        sets.append("active=%s")
        vals.append(bool(b["active"]))
        notes.append("enabled" if b["active"] else "disabled")
    if "region" in b:
        if b["region"] and b["region"] not in geo.FILTER_REGIONS:
            return jsonify(error="Unknown region."), 400
        sets.append("region=%s")
        vals.append(b["region"])
        notes.append(f"region={b['region'] or 'All UK'}")
    if "email" in b:
        if b["email"] in ("", None):
            sets.append("email=null")
            notes.append("email cleared")
        else:
            addr = alerts.clean_email(b["email"])
            if not addr:
                return jsonify(error="That does not look like an email address."), 400
            sets.append("email=%s")
            vals.append(addr)
            notes.append("email set")
    if "email_alerts" in b:
        if not isinstance(b["email_alerts"], bool):
            return jsonify(error="email_alerts must be true or false."), 400
        sets.append("email_alerts=%s")
        vals.append(b["email_alerts"])
        notes.append("alert emails " + ("on" if b["email_alerts"] else "off"))
    if not sets:
        return jsonify(error="Nothing to change."), 400
    db.run(f"update users set {', '.join(sets)} where id=%s", (*vals, uid))
    log("user_update", f"{row['username']}: {', '.join(notes)}")
    return jsonify(ok=True)


@app.delete("/api/admin/users/<int:uid>")
@admin_required
def admin_user_delete(uid):
    me = current_user()
    row = db.one("select * from users where id=%s", (uid,))
    if not row:
        return jsonify(error="No such user."), 404
    if uid == me["id"]:
        return jsonify(error="You cannot delete your own account."), 400
    if row["role"] == "admin" and _admin_count_excluding(uid) == 0:
        return jsonify(error="You cannot delete the last active admin."), 400
    db.run("delete from users where id=%s", (uid,))           # activity rows stay (username kept, user_id nulled)
    log("user_delete", row["username"])
    return jsonify(ok=True)


# ---- admin: groups (named sets of people; someone can be in several)
GROUP_NAME_MAX, GROUP_MAX = 40, 200


def _tidy_label(raw, maxlen, noun):
    """(tidy name, None) or (None, why not). Runs of spaces collapsed, 1 to maxlen characters, at least one letter or number, and no
    control or invisible formatting characters (they could make a name look like something it is not)."""
    if not isinstance(raw, str):
        return None, f"Give the {noun} a name."
    name = re.sub(r"\s+", " ", raw).strip()
    if not name:
        return None, f"Give the {noun} a name."
    if len(name) > maxlen:
        return None, f"A {noun} name can be at most {maxlen} characters."
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in name) or not re.search(r"[^\W_]", name):
        return None, f"A {noun} name needs at least one letter or number, and no hidden or control characters."
    return name, None


def _group_name(raw):
    return _tidy_label(raw, GROUP_NAME_MAX, "group")


def _groups_payload():
    mem = defaultdict(list)
    for m in db.many("select m.group_id, m.user_id, u.username from user_group_members m join users u on u.id = m.user_id order by lower(u.username)"):
        mem[m["group_id"]].append({"id": m["user_id"], "username": m["username"]})
    return [{"id": g["id"], "name": g["name"], "created_at": g["created_at"].isoformat(), "created_by": g["created_by"],
             "members": mem.get(g["id"], []), "count": len(mem.get(g["id"], []))}
            for g in db.many("select id, name, created_at, created_by from user_groups order by lower(name), id")]


def _user_group_names(uid):
    return [r["name"] for r in db.many("select g.name from user_group_members m join user_groups g on g.id = m.group_id where m.user_id=%s order by lower(g.name)", (uid,))]


@app.get("/api/admin/groups")
@admin_required
def admin_groups():
    return jsonify(groups=_groups_payload())


@app.post("/api/admin/groups")
@admin_required
def admin_group_create():
    name, err = _group_name((request.get_json(silent=True) or {}).get("name"))
    if err:
        return jsonify(error=err), 400
    if db.one("select count(*) as c from user_groups")["c"] >= GROUP_MAX:
        return jsonify(error=f"That is the most groups there can be ({GROUP_MAX})."), 400
    try:
        row = db.run("insert into user_groups (name, created_by) values (%s,%s) returning id", (name, current_user()["username"]))
    except db.Duplicate:
        return jsonify(error="There is already a group with that name."), 409
    log("group_create", name)
    return jsonify(id=row["id"], name=name), 201


@app.patch("/api/admin/groups/<int:gid>")
@admin_required
def admin_group_rename(gid):
    old = db.one("select name from user_groups where id=%s", (gid,))
    if not old:
        return jsonify(error="No such group."), 404
    name, err = _group_name((request.get_json(silent=True) or {}).get("name"))
    if err:
        return jsonify(error=err), 400
    try:
        db.run("update user_groups set name=%s where id=%s", (name, gid))
    except db.Duplicate:
        return jsonify(error="There is already a group with that name."), 409
    if name != old["name"]:
        log("group_rename", f"{old['name']} → {name}")
    return jsonify(ok=True, name=name)


@app.delete("/api/admin/groups/<int:gid>")
@admin_required
def admin_group_delete(gid):
    g = db.one("select name, (select count(*) from user_group_members where group_id=%s) as n from user_groups where id=%s", (gid, gid))
    if not g:
        return jsonify(error="No such group."), 404
    db.run("delete from user_groups where id=%s", (gid,))                  # the links go with it; the people stay
    log("group_delete", f"{g['name']} ({g['n']} member{'' if g['n'] == 1 else 's'} taken out of it)")
    return jsonify(ok=True)


def _log_members(group, added, removed):
    bits = [f"+{n}" for n in added] + [f"-{n}" for n in removed]
    if bits:
        log("group_members", f"{group}: {', '.join(bits)}")


@app.put("/api/admin/groups/<int:gid>/members")
@admin_required
def admin_group_members_set(gid):
    """Make the group's members exactly these people (the tick-list dialog)."""
    g = db.one("select name from user_groups where id=%s", (gid,))
    if not g:
        return jsonify(error="No such group."), 404
    ids = (request.get_json(silent=True) or {}).get("user_ids")
    if not isinstance(ids, list) or len(ids) > 5000 or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return jsonify(error="user_ids must be a list of user ids."), 400
    want = set(ids)
    users = {r["id"]: r["username"] for r in db.many("select id, username from users")}
    if not want <= set(users):
        return jsonify(error="One of those people no longer exists."), 400
    have = {r["user_id"] for r in db.many("select user_id from user_group_members where group_id=%s", (gid,))}
    me = current_user()["username"]
    steps = [("delete from user_group_members where group_id=%s and not (user_id = any(%s))", (gid, list(want)))]
    steps += [("insert into user_group_members (user_id, group_id, added_by) values (%s,%s,%s) on conflict do nothing", (uid, gid, me)) for uid in want - have]
    db.txn(steps)
    _log_members(g["name"], sorted(users[u] for u in want - have), sorted(users[u] for u in have - want))
    return jsonify(ok=True, count=len(want))


@app.put("/api/admin/groups/<int:gid>/members/<int:uid>")
@admin_required
def admin_group_member_add(gid, uid):
    g, u = db.one("select name from user_groups where id=%s", (gid,)), db.one("select username from users where id=%s", (uid,))
    if not g or not u:
        return jsonify(error="No such group or person."), 404
    row = db.run("insert into user_group_members (user_id, group_id, added_by) values (%s,%s,%s) on conflict do nothing returning user_id",
                 (uid, gid, current_user()["username"]))
    if row:
        _log_members(g["name"], [u["username"]], [])
    return jsonify(ok=True, changed=bool(row))


@app.delete("/api/admin/groups/<int:gid>/members/<int:uid>")
@admin_required
def admin_group_member_remove(gid, uid):
    g, u = db.one("select name from user_groups where id=%s", (gid,)), db.one("select username from users where id=%s", (uid,))
    if not g or not u:
        return jsonify(error="No such group or person."), 404
    row = db.run("delete from user_group_members where user_id=%s and group_id=%s returning user_id", (uid, gid))
    if row:
        _log_members(g["name"], [], [u["username"]])
    return jsonify(ok=True, changed=bool(row))


# ----------------------------------------------------------------- email alerts to groups
# A group is emailed about breaches placed in the regions it watches, first seen within the last few days, that it has not been told about.
# Feeds are only read when someone opens the dashboard (see the top of this file), so alerts go out after those refreshes, not on a clock.
ALERT_LOCK = threading.Lock()
_alert_pause = {"until": 0.0}                 # after a run in which nothing could be sent, wait before trying again
_test_hits = defaultdict(deque)


def _setting(key, default=""):
    row = db.one("select value from app_settings where key=%s", (key,))
    return row["value"] if row else default


def _set_setting(key, value):
    db.run("insert into app_settings (key, value) values (%s, %s) on conflict (key) do update set value=excluded.value", (key, value))


def _alerts_on():
    try:
        return _setting("alerts_enabled") == "1"
    except Exception:
        return False


def _email_log(kind, groups, sent, subject, ok, detail):
    db.run("insert into email_log (kind, group_name, to_count, subject, ok, detail) values (%s,%s,%s,%s,%s,%s)",
           (kind, alerts.one_line(groups, 200), sent, alerts.one_line(subject, 200), ok, alerts.one_line(detail, 400)))


def _baseline(gid, regions, include_unplaced):
    """Breaches already on the dashboard when a group's alerts start are marked as told, so switching alerts on is not a flood."""
    uniq = {v["key"]: v for v in alerts.matching(list(STATE["victims"]), regions, include_unplaced, datetime.now(timezone.utc))}
    if uniq:
        db.run("insert into alert_sent (victim_key, group_id, victim, kind) select k, %s, t, 'baseline' from unnest(%s::text[], %s::text[]) as x(k, t) on conflict do nothing",
               (gid, list(uniq), [v["post_title"] for v in uniq.values()]))
    return len(uniq)


def _release(gid, keys):
    db.run("delete from alert_sent where group_id=%s and kind='pending' and victim_key = any(%s)", (gid, keys))


def _run_alerts():
    """After a refresh and placement: email each person in an alerting group about breaches that are new to their groups. Never raises."""
    if LEGACY or not alerts.configured() or time.time() < _alert_pause["until"] or not ALERT_LOCK.acquire(blocking=False):
        return
    try:
        db.run("delete from email_log where ts < now() - interval '90 days'")            # keep the tables small
        db.run("delete from alert_sent where sent_at < now() - interval '120 days'")
        if not _alerts_on():
            return
        db.run("delete from alert_sent where kind='pending' and sent_at < now() - interval '1 hour'")
        now = datetime.now(timezone.utc)
        victims = list(STATE["victims"])
        todo = {}                                              # group id -> (group, its new breaches), each claimed in alert_sent first
        for gr in db.many("select g.id, g.name, a.regions, a.include_unplaced from group_alerts a join user_groups g on g.id = a.group_id where a.enabled"):
            cand = {}
            for v in sorted(alerts.matching(victims, gr["regions"], gr["include_unplaced"], now), key=lambda x: str(x.get("seen") or ""), reverse=True):
                cand.setdefault(v["key"], v)
            if not cand:
                continue
            done = {r["victim_key"] for r in db.many("select victim_key from alert_sent where group_id=%s and victim_key = any(%s)", (gr["id"], list(cand)))}
            fresh = [v for k, v in cand.items() if k not in done]
            if not fresh:
                continue
            got = {r["victim_key"] for r in db.many(
                "insert into alert_sent (victim_key, group_id, victim, kind) select k, %s, t, 'pending' from unnest(%s::text[], %s::text[]) as x(k, t) on conflict do nothing returning victim_key",
                (gr["id"], [v["key"] for v in fresh], [v["post_title"] for v in fresh]))}
            fresh = [v for v in fresh if v["key"] in got]
            if fresh:
                todo[gr["id"]] = (gr, fresh)
        if not todo:
            return
        people = {}                                            # one email per person, even if several of their groups match
        for m in db.many("select m.group_id, u.username, u.email from user_group_members m join users u on u.id = m.user_id "
                         "where u.active and u.email_alerts and u.email is not null and m.group_id = any(%s)", (list(todo),)):
            addr = alerts.clean_email(m["email"])
            if not addr:
                continue
            who = people.setdefault(addr, {"username": m["username"], "groups": {}, "victims": {}})
            gr, fresh = todo[m["group_id"]]
            who["groups"][gr["id"]] = gr["name"]
            for v in fresh:
                who["victims"].setdefault(v["key"], v)
        for gid in set(todo) - {g for w in people.values() for g in w["groups"]}:      # nobody there to tell: leave it for when someone is
            _release(gid, [v["key"] for v in todo[gid][1]])
            todo.pop(gid)
        if not people:
            return
        names = ", ".join(sorted({n for w in people.values() for n in w["groups"].values()}, key=str.lower))
        titles = {v["key"]: v["post_title"] for _, fresh in todo.values() for v in fresh}
        summary = ", ".join(list(titles.values())[:3]) + (f" and {len(titles) - 3} more" if len(titles) > 3 else "")
        used = db.one("select coalesce(sum(to_count), 0) as n from email_log where kind='alert' and ts > now() - interval '24 hours'")["n"]
        if used + len(people) > alerts.DAILY_CAP:
            for gid, (_, fresh) in todo.items():
                _release(gid, [v["key"] for v in fresh])
            _alert_pause["until"] = time.time() + alerts.RETRY_PAUSE
            _email_log("alert", names, 0, summary, False, f"Not sent: the limit of {alerts.DAILY_CAP} alert emails in 24 hours would be passed ({used} already sent).")
            return
        sent, fails, ok_groups = 0, [], set()
        for i, (addr, who) in enumerate(people.items()):
            if i:
                time.sleep(alerts.SEND_GAP)
            subject, text, body = alerts.compose(list(who["victims"].values()), list(who["groups"].values()), now)
            ok, why = alerts.send(addr, subject, text, body)
            if ok:
                sent += 1
                ok_groups |= set(who["groups"])
            else:
                fails.append(f"{who['username']}: {why}")
        for gid, (_, fresh) in todo.items():
            keys = [v["key"] for v in fresh]
            if gid in ok_groups:
                db.run("update alert_sent set kind='sent', sent_at=now() where group_id=%s and kind='pending' and victim_key = any(%s)", (gid, keys))
            else:
                _release(gid, keys)                            # nothing got through: try again next time
        if not sent:
            _alert_pause["until"] = time.time() + alerts.RETRY_PAUSE
        _email_log("alert", names, sent, summary, not fails, f"{sent} of {len(people)} people emailed" + ("; " + "; ".join(fails[:3]) if fails else ""))
    except Exception as e:
        STATE["errors"]["alerts"] = str(e)[:200]
        _alert_pause["until"] = time.time() + alerts.RETRY_PAUSE
    finally:
        ALERT_LOCK.release()


@app.get("/api/admin/alerts")
@admin_required
def admin_alerts():
    st = alerts.status()
    cfg = {r["group_id"]: r for r in db.many("select group_id, enabled, regions, include_unplaced, updated_by, updated_at from group_alerts")}
    mem = defaultdict(list)
    for m in db.many("select m.group_id, u.username, u.email, u.email_alerts from user_group_members m join users u on u.id = m.user_id where u.active order by lower(u.username)"):
        mem[m["group_id"]].append(m)
    groups = []
    for gr in db.many("select id, name from user_groups order by lower(name), id"):
        c, ms = cfg.get(gr["id"]) or {}, mem.get(gr["id"], [])
        has = lambda m: bool(alerts.clean_email(m["email"] or ""))
        groups.append({"id": gr["id"], "name": gr["name"], "enabled": bool(c.get("enabled")), "regions": list(c.get("regions") or []),
                       "include_unplaced": bool(c.get("include_unplaced")), "members": len(ms),
                       "recipients": sum(1 for m in ms if has(m) and m["email_alerts"]),
                       "no_email": [m["username"] for m in ms if not has(m)], "opted_out": [m["username"] for m in ms if has(m) and not m["email_alerts"]],
                       "updated_by": c.get("updated_by", ""), "updated_at": c["updated_at"].isoformat() if c.get("updated_at") else None})
    used = db.one("select coalesce(sum(to_count), 0) as n from email_log where kind='alert' and ts > now() - interval '24 hours'")["n"]
    rows = db.many("select ts, kind, group_name, to_count, subject, ok, detail from email_log order by id desc limit 40")
    return jsonify(configured=st["configured"], reason=st["reason"], sender=st["sender"], public_url=alerts.PUBLIC_URL, enabled=_alerts_on(),
                   window_days=alerts.RECENT_DAYS, daily_cap=alerts.DAILY_CAP, sent_today=int(used), regions=geo.REGIONS, groups=groups,
                   log=[{**r, "ts": r["ts"].isoformat()} for r in rows])


@app.put("/api/admin/alerts/settings")
@admin_required
def admin_alerts_settings():
    on = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(on, bool):
        return jsonify(error="enabled must be true or false."), 400
    if on and not alerts.configured():
        return jsonify(error="Email sending is not set up yet. " + alerts.status()["reason"]), 409
    if on and not _alerts_on():            # start from now: what is already on the dashboard is not emailed
        for gr in db.many("select group_id, regions, include_unplaced from group_alerts where enabled"):
            _baseline(gr["group_id"], gr["regions"], gr["include_unplaced"])
    _set_setting("alerts_enabled", "1" if on else "0")
    log("alerts_settings", "alert emails " + ("on" if on else "off"))
    return jsonify(enabled=on)


@app.put("/api/admin/groups/<int:gid>/alerts")
@admin_required
def admin_group_alerts(gid):
    gr = db.one("select id, name from user_groups where id=%s", (gid,))
    if not gr:
        return jsonify(error="No such group."), 404
    b = request.get_json(silent=True) or {}
    enabled, regions, inc = b.get("enabled"), b.get("regions", []), b.get("include_unplaced", False)
    if not isinstance(enabled, bool) or not isinstance(inc, bool) or not isinstance(regions, list) or len(regions) > len(geo.REGIONS) \
            or any(not isinstance(r, str) or r not in geo.REGIONS for r in regions):
        return jsonify(error="Choose regions from the list."), 400
    regions = [r for r in geo.REGIONS if r in set(regions)]
    if enabled and not regions and not inc:
        return jsonify(error="Choose at least one region to be emailed about."), 400
    old = db.one("select enabled, regions, include_unplaced from group_alerts where group_id=%s", (gid,))
    db.run("insert into group_alerts (group_id, enabled, regions, include_unplaced, updated_by, updated_at) values (%s,%s,%s,%s,%s,now()) "
           "on conflict (group_id) do update set enabled=excluded.enabled, regions=excluded.regions, include_unplaced=excluded.include_unplaced, updated_by=excluded.updated_by, updated_at=now()",
           (gid, enabled, regions, inc, current_user()["username"]))
    widened = not old or not old["enabled"] or bool(set(regions) - set(old["regions"])) or (inc and not old["include_unplaced"])
    skipped = _baseline(gid, regions, inc) if enabled and widened else 0
    log("alerts_group", f"{gr['name']}: " + (("on for " + (", ".join(regions) or "no region") + (" and not-yet-placed" if inc else "")) if enabled else "off"))
    return jsonify(ok=True, already_listed=skipped)


@app.post("/api/admin/alerts/test")
@admin_required
def admin_alerts_test():
    u = current_user()
    if not alerts.configured():
        return jsonify(error="Email sending is not set up yet. " + alerts.status()["reason"]), 409
    row = db.one("select email from users where id=%s", (u["id"],))
    to = alerts.clean_email((row or {}).get("email") or "")
    if not to:
        return jsonify(error="Your own account has no email address yet. Add it with the envelope button on your row in Users, then try again."), 400
    q, now = _test_hits[u["id"]], time.time()
    while q and now - q[0] > 600:
        q.popleft()
    if len(q) >= 5:
        return jsonify(error="That is enough test emails for now. Try again in a few minutes."), 429
    q.append(now)
    subject, text, body = alerts.compose_test()
    ok, why = alerts.send(to, subject, text, body)
    _email_log("test", "", 1 if ok else 0, f"Test email to {u['username']}", ok, why)
    log("alerts_test", "sent" if ok else "failed: " + why)
    return jsonify(ok=ok, error="" if ok else "The mail service did not accept it: " + why)


@app.put("/api/me/email-alerts")
def api_me_email_alerts():
    on = (request.get_json(silent=True) or {}).get("on")
    if LEGACY or not isinstance(on, bool):
        return jsonify(error="Not available."), 400
    db.run("update users set email_alerts=%s where id=%s", (on, current_user()["id"]))
    log("email_alerts", "on" if on else "off")
    return jsonify(on=on)


def _activity_query():
    where, vals = [], []
    if request.args.get("user"):
        where.append("lower(username)=lower(%s)")
        vals.append(request.args["user"])
    if request.args.get("event"):
        where.append("event=%s")
        vals.append(request.args["event"])
    if request.args.get("q"):
        where.append("(detail ilike %s or username ilike %s)")
        vals += [f"%{request.args['q']}%"] * 2
    return (" where " + " and ".join(where)) if where else "", vals


@app.get("/api/admin/activity")
@admin_required
def admin_activity():
    w, vals = _activity_query()
    limit = max(1, min(int(request.args.get("limit", 100)), 1000))
    offset = max(0, int(request.args.get("offset", 0)))
    total = db.one(f"select count(*) as c from activity{w}", vals)["c"]
    rows = db.many(f"select id, ts, username, event, detail, ip from activity{w} order by ts desc, id desc limit %s offset %s",
                   (*vals, limit, offset))
    return jsonify(total=total, items=[{**r, "ts": r["ts"].isoformat()} for r in rows],
                   users=[r["username"] for r in db.many("select distinct username from activity where username<>'' order by 1")],
                   events=[r["event"] for r in db.many("select distinct event from activity order by 1")])


@app.get("/api/admin/activity.csv")
@admin_required
def admin_activity_csv():
    w, vals = _activity_query()
    rows = db.many(f"select ts, username, event, detail, ip from activity{w} order by ts desc limit 20000", vals)
    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["time_utc", "username", "event", "detail", "ip"])
    safe = lambda s: ("'" + s) if s[:1] in ("=", "+", "-", "@") else s          # no spreadsheet formula injection
    for r in rows:
        wr.writerow([r["ts"].isoformat(), safe(r["username"]), r["event"], safe(r["detail"]), r["ip"]])
    log("export", f"activity csv ({len(rows)} rows)")
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=evileye-activity.csv"})


# ---- admin: manual placement of a victim into a region
PLACEABLE = geo.REGIONS + [geo.OVERSEAS]


@app.put("/api/admin/placement")
@admin_required
def admin_placement_set():
    b = request.get_json(silent=True) or {}
    title, rocu, note = (b.get("title") or "").strip(), b.get("rocu", ""), (b.get("note") or "").strip()[:200]
    town = (b.get("town") or "").strip()[:60]
    key = geo.norm(title)
    if not key or not any(v["key"] == key for v in STATE["victims"]):
        return jsonify(error="Unknown victim."), 404
    if rocu not in PLACEABLE:
        return jsonify(error="Choose a ROCU region, Scotland, Northern Ireland or Overseas / not UK."), 400
    db.run("""insert into placement_overrides (key, victim, rocu, note, town, set_by) values (%s,%s,%s,%s,%s,%s)
              on conflict (key) do update set rocu=excluded.rocu, note=excluded.note, town=excluded.town,
                                              set_by=excluded.set_by, set_at=now()""",
           (key, title, rocu, note, town, current_user()["username"]))
    load_overrides()
    _apply_geo(STATE["victims"])
    log("placement_set", f"{title} -> {rocu}" + (f" ({town})" if town else "") + (f" [{note}]" if note else ""))
    return jsonify(ok=True)


@app.delete("/api/admin/placement")
@admin_required
def admin_placement_clear():
    title = (request.args.get("title") or "").strip()
    key = geo.norm(title)
    if not db.one("select 1 as x from placement_overrides where key=%s", (key,)):
        return jsonify(error="No manual placement for that victim."), 404
    db.run("delete from placement_overrides where key=%s", (key,))
    load_overrides()
    _apply_geo(STATE["victims"])
    log("placement_clear", title)
    return jsonify(ok=True)


# ---- any user: flag a false positive, and put initials on a victim to show it is being worked
INITIALS_RE = re.compile(r"^[A-Za-z]{2,4}$")


def _find_victim(title):
    key = geo.norm((title or "").strip())
    return next((v for v in STATE["victims"] if key and v["key"] == key), None)


def _manual_changed():
    load_manual()
    with _lock:
        STATE["victims"] = _merge_manual(STATE["victims"])
    _apply_geo(STATE["victims"])
    try:
        _save_snapshot()
    except Exception as e:
        STATE["errors"]["snapshot"] = str(e)


BREACH_LIMITS = {"gang": 60, "sector": 60, "website": 200, "town": 60, "data": 40, "ransom": 40, "description": 700}
BREACH_MAX = 2000


def _breach_fields(b, partial=False):
    """(clean fields, error). Required: name, region and date; everything else is optional (and may be left out)."""
    out = {}
    if not partial or "title" in b:
        name, why = _tidy_label(b.get("title"), 200, "breach")
        if why:
            return None, why
        out["title"] = name
    if not partial or "rocu" in b:
        if b.get("rocu") not in PLACEABLE:
            return None, "Choose a region."
        out["rocu"] = b["rocu"]
    if not partial or "date" in b:
        try:
            d = datetime.strptime(str(b.get("date")), "%Y-%m-%d").date()
        except ValueError:
            return None, "Give the date of the breach."
        if d < datetime(2000, 1, 1).date() or d > (datetime.now(timezone.utc) + timedelta(days=1)).date():
            return None, "The date must be after 1999 and not in the future."
        out["occurred"] = d
    for k, n in BREACH_LIMITS.items():
        if k in b:
            s = b[k]
            if s is None:
                s = ""
            if not isinstance(s, str):
                return None, f"{k.capitalize()} must be text."
            s = s.strip() if k == "description" else re.sub(r"\s+", " ", s).strip()
            if len(s) > n or any(unicodedata.category(ch) in ("Cc", "Cf") and ch not in "\n\t" for ch in s):
                return None, f"{k.capitalize()} is too long, or has hidden characters (at most {n})."
            out[{"gang": "gang"}.get(k, k)] = s
    return out, None


def _breach_row(r):
    return {"id": r["id"], "title": r["title"], "rocu": r["rocu"], "date": r["occurred"].isoformat(), "gang": r["gang"], "sector": r["sector"], "website": r["website"],
            "town": r["town"], "data": r["data"], "ransom": r["ransom"], "description": r["description"], "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat(), "updated_by": r["updated_by"], "updated_at": r["updated_at"].isoformat()}


@app.post("/api/breaches")
def breach_add():
    """Anyone signed in can add a breach by hand. Name, region and date are required; the rest is optional."""
    if LEGACY:
        return jsonify(error="Database not connected: breaches cannot be added."), 503
    f, why = _breach_fields(request.get_json(silent=True) or {})
    if why:
        return jsonify(error=why), 400
    key = geo.norm(f["title"])
    if not key:
        return jsonify(error="Give the breach a name with letters or numbers."), 400
    if any(v["key"] == key for v in STATE["victims"]):
        return jsonify(error="A breach with that name is already on the dashboard."), 409
    if db.one("select count(*) as n from manual_breaches")["n"] >= BREACH_MAX:
        return jsonify(error="Too many hand-added breaches."), 400
    u = current_user()
    try:
        row = db.run("""insert into manual_breaches (key, title, rocu, occurred, gang, sector, website, town, data, ransom, description, created_by, updated_by)
                        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                     (key, f["title"], f["rocu"], f["occurred"], f.get("gang", ""), f.get("sector", ""), f.get("website", ""), f.get("town", ""), f.get("data", ""),
                      f.get("ransom", ""), f.get("description", ""), u["username"], u["username"]))
    except db.Duplicate:
        return jsonify(error="A breach with that name is already on the dashboard."), 409
    _manual_changed()
    log("breach_add", f'{f["title"]} ({f["rocu"]})')
    return jsonify(ok=True, id=row["id"], title=f["title"]), 201


@app.get("/api/admin/breaches")
@admin_required
def admin_breaches():
    return jsonify(items=[_breach_row(r) for r in db.many("select * from manual_breaches order by occurred desc, id desc")], regions=PLACEABLE)


@app.patch("/api/admin/breaches/<int:bid>")
@admin_required
def admin_breach_update(bid):
    r = db.one("select * from manual_breaches where id=%s", (bid,))
    if not r:
        return jsonify(error="No such breach."), 404
    f, why = _breach_fields(request.get_json(silent=True) or {}, partial=True)
    if why:
        return jsonify(error=why), 400
    if not f:
        return jsonify(error="Nothing to change."), 400
    new = {"title": r["title"], "rocu": r["rocu"], "occurred": r["occurred"], **{k: r[k] for k in BREACH_LIMITS}}
    new.update(f)
    key = geo.norm(new["title"])
    if not key:
        return jsonify(error="Give the breach a name with letters or numbers."), 400
    if key != r["key"] and (any(v["key"] == key for v in STATE["victims"]) or db.one("select 1 as x from manual_breaches where key=%s", (key,))):
        return jsonify(error="A breach with that name is already on the dashboard."), 409
    steps = [("""update manual_breaches set key=%s, title=%s, rocu=%s, occurred=%s, gang=%s, sector=%s, website=%s, town=%s, data=%s, ransom=%s, description=%s,
                 updated_by=%s, updated_at=now() where id=%s""",
              (key, new["title"], new["rocu"], new["occurred"], new["gang"], new["sector"], new["website"], new["town"], new["data"], new["ransom"], new["description"],
               current_user()["username"], bid))]
    if key != r["key"]:                  # a renamed breach keeps its flags, tasks and placement
        steps += [("update victim_markers set key=%s, victim=%s where key=%s", (key, new["title"], r["key"])), ("update victim_flags set key=%s where key=%s", (key, r["key"])),
                  ("update placement_overrides set key=%s where key=%s", (key, r["key"]))]
    db.txn(steps)
    if key != r["key"]:
        load_overrides()
        load_marks()
    _manual_changed()
    log("breach_update", f'{new["title"]}: ' + ", ".join(sorted(f)))
    return jsonify(ok=True)


@app.delete("/api/admin/breaches/<int:bid>")
@admin_required
def admin_breach_delete(bid):
    r = db.one("select * from manual_breaches where id=%s", (bid,))
    if not r:
        return jsonify(error="No such breach."), 404
    db.txn([("delete from victim_markers where key=%s", (r["key"],)), ("delete from victim_flags where key=%s", (r["key"],)),
            ("delete from placement_overrides where key=%s", (r["key"],)), ("delete from manual_breaches where id=%s", (bid,))])
    load_overrides()
    load_marks()
    _manual_changed()
    log("breach_delete", r["title"])
    return jsonify(ok=True)


def _marks_changed():
    load_marks()
    _apply_geo(STATE["victims"])


@app.put("/api/victims/flag")
def victim_flag_set():
    if LEGACY:
        return jsonify(error="Database not connected: flags are unavailable."), 503
    b = request.get_json(silent=True) or {}
    v, kind, note = _find_victim(b.get("title")), b.get("kind"), (b.get("note") or "").strip()[:120]
    if not v:
        return jsonify(error="Unknown victim."), 404
    if kind not in ("not_uk", "not_region"):
        return jsonify(error="Choose: not in this region, or not in the UK."), 400
    if v["key"] in FLAGS:
        return jsonify(error="Already flagged as a false positive. Undo that flag first."), 409
    if v.get("manual"):
        return jsonify(error="An admin set this victim's region by hand, so it cannot be flagged. Ask an admin to change it."), 409
    if kind == "not_region" and v["rocu"] not in geo.REGIONS:
        return jsonify(error="Only a victim shown in a region can be flagged as not in that region."), 400
    if kind == "not_uk" and v["rocu"] == geo.OVERSEAS:
        return jsonify(error="Already shown as overseas / not UK."), 400
    region = v["rocu"]                                   # the region it was shown in: needed to undo/review it, and for "not in <region>"
    db.run("insert into victim_flags (key, victim, kind, rocu, note, flagged_by) values (%s,%s,%s,%s,%s,%s)",
           (v["key"], v["post_title"], kind, region, note, current_user()["username"]))
    _marks_changed()
    log("victim_flag", f'{v["post_title"]}: ' + ("not in the UK" if kind == "not_uk" else f"not in {region}") + (f' [{note}]' if note else ""))
    return jsonify(ok=True)


@app.delete("/api/victims/flag")
def victim_flag_clear():
    if LEGACY:
        return jsonify(error="Database not connected."), 503
    v = _find_victim(request.args.get("title"))
    fl = FLAGS.get(v["key"]) if v else None
    if not fl:
        return jsonify(error="That victim is not flagged."), 404
    u = current_user()
    if u["role"] != "admin" and u["username"] != fl["flagged_by"]:
        return jsonify(error=f'Only {fl["flagged_by"]} or an admin can undo this flag.'), 403
    db.run("delete from victim_flags where key=%s", (v["key"],))
    _marks_changed()
    log("victim_flag_clear", v["post_title"])
    return jsonify(ok=True)


# ---- markers: Task (built in) and custom ones. Initials, and when it was allocated, started and completed.
MARKER_NAME_MAX, MARKER_MAX = 24, 30
COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _builtin_type():
    return next((t for t in MARKER_TYPES.values() if t["builtin"]), None)


def _marker_view(m):
    t = MARKER_TYPES.get(m["type_id"]) or {}
    iso = lambda x: x.isoformat() if x else None
    return {"id": m["id"], "type_id": m["type_id"], "name": t.get("name", "?"), "colour": t.get("colour", "#94a2b7"), "builtin": bool(t.get("builtin")),
            "initials": m["initials"], "by": m["allocated_by"], "allocated_at": iso(m["allocated_at"]), "started_at": iso(m["started_at"]),
            "completed_at": iso(m["completed_at"]), "marked_at": iso(m.get("marked_at") or m["allocated_at"]),
            "status": "completed" if m["completed_at"] else "started" if m["started_at"] else "allocated" if m["initials"] else "unallocated"}


def _live_task(key):
    """The Task that is live on a breach (allocated, started or still unallocated), if any."""
    bt = _builtin_type()
    return next((m for m in MARKERS.get(key, []) if bt and m["type_id"] == bt["id"] and not m["completed_at"]), None)


def _live_markers(key):
    return [m for m in MARKERS.get(key, []) if not m["completed_at"]]


def _can_edit_marker(m, u):
    return u["role"] == "admin" or u["username"] == m["allocated_by"] or bool(m["initials"] and (u.get("initials") or "") == m["initials"])


def _who(m):
    """Who may change a marker, in words (an unallocated Task has no initials, only whoever marked it)."""
    return (f'{m["initials"]} ({m["allocated_by"]})' if m["initials"] else f'{m["allocated_by"]} (who marked it)')


def _when(raw, now):
    """A date and time the browser sent ('now' means the server's clock). None if it is not a date or is in the future."""
    if raw == "now":
        return now
    d = _ts(raw) if isinstance(raw, str) else None
    return d if d and d <= now + timedelta(hours=1) else None


def _marker_put(v, mt, initials, u, allocated_at=None, unallocated=False):
    """Put a marker on a breach, or take over the one that is there. A completed marker starts a fresh cycle. initials '' with unallocated=True
    marks a Task nobody has taken yet; allocating over one of those sets the date it was allocated. Returns (id, previous initials)."""
    prev = next((m for m in MARKERS.get(v["key"], []) if m["type_id"] == mt["id"]), None)
    when = allocated_at or datetime.now(timezone.utc)
    if prev and prev["completed_at"]:
        db.run("update victim_markers set initials=%s, allocated_by=%s, allocated_at=%s, marked_at=%s, started_at=null, completed_at=null, updated_by=%s, updated_at=now() where id=%s",
               (initials, u["username"], when, when, u["username"], prev["id"]))
        mid, old = prev["id"], None
    elif prev and not prev["initials"] and initials:        # somebody takes an unallocated Task: this is when it was allocated
        db.run("update victim_markers set initials=%s, allocated_by=%s, allocated_at=%s, marked_at=least(coalesce(marked_at, %s), %s), updated_by=%s, updated_at=now() where id=%s",
               (initials, u["username"], when, when, when, u["username"], prev["id"]))
        mid, old = prev["id"], ""
    elif prev:
        db.run("update victim_markers set initials=%s, allocated_by=%s, updated_by=%s, updated_at=now() where id=%s", (initials, u["username"], u["username"], prev["id"]))
        mid, old = prev["id"], prev["initials"]
    else:
        # an upsert: two people allocating the same breach at the same instant both get an answer (the later one takes over)
        mid, old = db.run("""insert into victim_markers (key, victim, type_id, initials, allocated_by, allocated_at, marked_at, updated_by) values (%s,%s,%s,%s,%s,%s,%s,%s)
                             on conflict (key, type_id) do update set initials=excluded.initials, allocated_by=excluded.allocated_by, updated_by=excluded.updated_by, updated_at=now()
                             returning id""", (v["key"], v["post_title"], mt["id"], initials, u["username"], when, when, u["username"]))["id"], None
    return mid, old


@app.get("/api/marker-types")
def api_marker_types():
    counts = {}
    for ms in MARKERS.values():
        for m in ms:
            c = counts.setdefault(m["type_id"], [0, 0])
            c[1] += 1
            c[0] += 0 if m["completed_at"] else 1
    return jsonify(types=[{"id": t["id"], "name": t["name"], "colour": t["colour"], "builtin": bool(t["builtin"]), "active": bool(t["active"]),
                           "live": counts.get(t["id"], [0, 0])[0], "total": counts.get(t["id"], [0, 0])[1]}
                          for t in sorted(MARKER_TYPES.values(), key=lambda t: (not t["builtin"], t["name"].lower()))])


def _colour(raw):
    return raw.lower() if isinstance(raw, str) and COLOUR_RE.match(raw) else None


@app.post("/api/admin/marker-types")
@admin_required
def admin_marker_type_create():
    b = request.get_json(silent=True) or {}
    name, err = _tidy_label(b.get("name"), MARKER_NAME_MAX, "marker")
    if err:
        return jsonify(error=err), 400
    colour = _colour(b.get("colour"))
    if not colour:
        return jsonify(error="Choose a colour (six-digit hex such as #4f9dff)."), 400
    if len(MARKER_TYPES) >= MARKER_MAX:
        return jsonify(error=f"That is the most markers there can be ({MARKER_MAX})."), 400
    try:
        row = db.run("insert into marker_types (name, colour, created_by) values (%s,%s,%s) returning id", (name, colour, current_user()["username"]))
    except db.Duplicate:
        return jsonify(error="There is already a marker with that name."), 409
    load_marks()
    log("marker_type_create", f"{name} ({colour})")
    return jsonify(id=row["id"], name=name, colour=colour), 201


@app.patch("/api/admin/marker-types/<int:tid>")
@admin_required
def admin_marker_type_update(tid):
    t = MARKER_TYPES.get(tid)
    if not t:
        return jsonify(error="No such marker."), 404
    b = request.get_json(silent=True) or {}
    sets, vals, notes = [], [], []
    if "name" in b:
        name, err = _tidy_label(b["name"], MARKER_NAME_MAX, "marker")
        if err:
            return jsonify(error=err), 400
        sets.append("name=%s"); vals.append(name); notes.append(f"name {t['name']} → {name}")
    if "colour" in b:
        colour = _colour(b["colour"])
        if not colour:
            return jsonify(error="Choose a colour (six-digit hex such as #4f9dff)."), 400
        sets.append("colour=%s"); vals.append(colour); notes.append(f"colour {colour}")
    if "active" in b:
        if not b["active"] and t["builtin"]:
            return jsonify(error="The Task marker cannot be retired."), 400
        sets.append("active=%s"); vals.append(bool(b["active"])); notes.append("in use" if b["active"] else "retired")
    if not sets:
        return jsonify(error="Nothing to change."), 400
    try:
        db.run(f"update marker_types set {', '.join(sets)} where id=%s", (*vals, tid))
    except db.Duplicate:
        return jsonify(error="There is already a marker with that name."), 409
    load_marks()
    log("marker_type_update", f"{t['name']}: {', '.join(notes)}")
    return jsonify(ok=True)


@app.delete("/api/admin/marker-types/<int:tid>")
@admin_required
def admin_marker_type_delete(tid):
    t = MARKER_TYPES.get(tid)
    if not t:
        return jsonify(error="No such marker."), 404
    if t["builtin"]:
        return jsonify(error="The Task marker cannot be deleted."), 400
    n = db.one("select count(*) as c from victim_markers where type_id=%s", (tid,))["c"]
    if n:
        return jsonify(error=f"{n} breach{'es carry' if n != 1 else ' carries'} this marker, so it cannot be deleted. Retire it instead: it stays on those breaches but cannot be used again."), 409
    db.run("delete from marker_types where id=%s", (tid,))
    load_marks()
    log("marker_type_delete", t["name"])
    return jsonify(ok=True)


@app.put("/api/victims/markers")
def victim_marker_set():
    """Put a marker on a breach (Task unless another is named), or take over the one that is there."""
    if LEGACY:
        return jsonify(error="Database not connected: markers are unavailable."), 503
    b = request.get_json(silent=True) or {}
    v, initials = _find_victim(b.get("title")), (b.get("initials") or "").strip().upper()
    if not v:
        return jsonify(error="Unknown victim."), 404
    tid = b.get("type_id")
    mt = _builtin_type() if tid is None else MARKER_TYPES.get(tid) if isinstance(tid, int) and not isinstance(tid, bool) else None
    if not mt:
        return jsonify(error="Unknown marker."), 404
    unalloc = b.get("unallocated") is True
    if unalloc:
        if not mt["builtin"]:
            return jsonify(error="Only the Task can be left unallocated."), 400
        if initials:
            return jsonify(error="Either give initials or leave it unallocated, not both."), 400
    elif not INITIALS_RE.match(initials):
        return jsonify(error="Initials must be 2 to 4 letters."), 400
    u = current_user()
    existing = next((m for m in MARKERS.get(v["key"], []) if m["type_id"] == mt["id"]), None)
    if unalloc and existing and not existing["completed_at"]:
        return jsonify(error="This breach already has a live Task."), 409
    if not mt["active"] and not (existing and not existing["completed_at"]):
        return jsonify(error=f'The marker "{mt["name"]}" has been retired and cannot be used any more.'), 400
    at = None
    if b.get("allocated_at") is not None:
        at = _when(b["allocated_at"], datetime.now(timezone.utc))
        if not at:
            return jsonify(error="The allocated date is not a valid date and time (and cannot be in the future)."), 400
    mid, old = _marker_put(v, mt, initials, u, at, unalloc)
    if not unalloc and (b.get("remember") or not u.get("initials")):
        db.run("update users set initials=%s where id=%s", (initials, u["id"]))
    _marks_changed()
    took = f" (took over from {old})" if old and old != initials else " (was unallocated)" if old == "" and initials else ""
    if unalloc:
        log("task_unallocated", f'{v["post_title"]} marked as a Task, unallocated')
    elif mt["builtin"]:                                  # the Task marker keeps the older log names
        log("victim_alloc", f'{v["post_title"]} -> {initials}{took}')
    else:
        log("marker_set", f'{mt["name"]}: {v["post_title"]} -> {initials}{took}')
    return jsonify(ok=True, id=mid, initials=initials, marker=_marker_view(next(m for m in MARKERS[v["key"]] if m["id"] == mid)))


MARKER_FIELDS = (("allocated_at", "allocated"), ("started_at", "started"), ("completed_at", "completed"))


@app.patch("/api/victims/markers/<int:mid>")
def victim_marker_update(mid):
    """Change the initials, or any of the three dates (an ISO time, 'now', or null to clear started / completed)."""
    if LEGACY:
        return jsonify(error="Database not connected."), 503
    m = db.one("select * from victim_markers where id=%s", (mid,))
    if not m:
        return jsonify(error="No such marker."), 404
    u = current_user()
    if not _can_edit_marker(m, u):
        return jsonify(error=f'Only {_who(m)} or an admin can change this.'), 403
    b = request.get_json(silent=True) or {}
    now = datetime.now(timezone.utc)
    new = {k: m[k] for k in ("initials", "allocated_at", "started_at", "completed_at")}
    builtin = bool((MARKER_TYPES.get(m["type_id"]) or {}).get("builtin"))
    was_unalloc = not m["initials"]
    changes = []
    if "initials" in b:
        ini = (b["initials"] or "").strip().upper() if isinstance(b["initials"], str) else ""
        if ini == "" and builtin and isinstance(b["initials"], str):          # take the initials off a Task that has not started: it stays, unallocated
            if m["started_at"] or m["completed_at"]:
                return jsonify(error="Only a task that has not started can be unallocated."), 400
        elif not INITIALS_RE.match(ini):
            return jsonify(error="Initials must be 2 to 4 letters."), 400
        new["initials"] = ini
    for field, label in MARKER_FIELDS:
        if field not in b:
            continue
        if b[field] is None:
            if field == "allocated_at":
                return jsonify(error="A marker always has the date it was allocated."), 400
            new[field] = None
        else:
            d = _when(b[field], now)
            if not d:
                return jsonify(error=f"The {label} date is not a valid date and time (and cannot be in the future)."), 400
            new[field] = d
    if not any(k in b for k in ("initials", "allocated_at", "started_at", "completed_at")):
        return jsonify(error="Nothing to change."), 400
    if not new["initials"] and (new["started_at"] or new["completed_at"] or "allocated_at" in b):
        return jsonify(error="Put someone's initials on it first: an unallocated task cannot be started, completed or given an allocated date."), 400
    marked = m.get("marked_at") or m["allocated_at"]
    if new["initials"] and was_unalloc and "allocated_at" not in b:       # allocated by editing: it is allocated now
        new["allocated_at"] = now
    elif not new["initials"] and m["initials"]:                          # unallocated again: the date goes back to when it was first marked
        new["allocated_at"] = marked
    if new["started_at"] and new["started_at"] < new["allocated_at"]:
        return jsonify(error="It cannot be started before it was allocated."), 400
    if new["completed_at"] and new["completed_at"] < new["allocated_at"]:
        return jsonify(error="It cannot be completed before it was allocated."), 400
    if new["completed_at"] and new["started_at"] and new["completed_at"] < new["started_at"]:
        return jsonify(error="It cannot be completed before it was started."), 400
    db.run("update victim_markers set initials=%s, allocated_by=%s, allocated_at=%s, started_at=%s, completed_at=%s, marked_at=least(coalesce(marked_at, %s), %s), updated_by=%s, updated_at=now() where id=%s",
           (new["initials"], u["username"] if ((not new["initials"] and m["initials"]) or (new["initials"] and was_unalloc)) else m["allocated_by"], new["allocated_at"], new["started_at"], new["completed_at"], marked, new["allocated_at"], u["username"], mid))
    for field, label in MARKER_FIELDS:
        if new[field] != m[field]:
            changes.append(f"{label} cleared" if new[field] is None else f"{label} {new[field].strftime('%d %b %Y %H:%M')}")
    if new["initials"] != m["initials"]:
        changes.append(f"initials {m['initials'] or '(none)'} → {new['initials'] or '(none)'}")
    _marks_changed()
    mt = MARKER_TYPES.get(m["type_id"]) or {"name": "?", "builtin": False}
    log("marker_update", f'{mt["name"]}: {m["victim"]}: ' + ", ".join(changes) if changes else f'{mt["name"]}: {m["victim"]}: no change')
    return jsonify(ok=True, marker=_marker_view(next(x for x in MARKERS[m["key"]] if x["id"] == mid)))


@app.delete("/api/victims/markers/<int:mid>")
def victim_marker_remove(mid):
    if LEGACY:
        return jsonify(error="Database not connected."), 503
    m = db.one("select * from victim_markers where id=%s", (mid,))
    if not m:
        return jsonify(error="No such marker."), 404
    u = current_user()
    if not _can_edit_marker(m, u):
        return jsonify(error=f'Only {_who(m)} or an admin can release this.'), 403
    db.run("delete from victim_markers where id=%s", (mid,))
    _marks_changed()
    mt = MARKER_TYPES.get(m["type_id"]) or {"name": "?", "builtin": False}
    was = m["initials"] or "unallocated"
    log("victim_alloc_clear" if mt["builtin"] else "marker_clear", f'{m["victim"]} (was {was})' if mt["builtin"] else f'{mt["name"]}: {m["victim"]} (was {was})')
    return jsonify(ok=True)


@app.get("/api/markers/live")
def api_markers_live():
    """Everything being worked on right now: markers that are allocated or started and not completed. Optional ?rocu= for one region."""
    region = request.args.get("rocu", "")
    by_key = {v["key"]: v for v in STATE["victims"]}
    items = []
    for key, ms in MARKERS.items():
        v = by_key.get(key)
        for m in ms:
            if m["completed_at"]:
                continue
            rocu = v["rocu"] if v else ""
            if region and rocu != region:
                continue
            items.append({**_marker_view(m), "title": v["post_title"] if v else m["victim"], "group": (v or {}).get("group", ""), "sector": (v or {}).get("activity", ""),
                          "rocu": rocu, "town": (v or {}).get("town", "")})
    # in progress first (most recently started on top), then the Tasks nobody has taken, then waiting ones, longest waiting first
    started = sorted((x for x in items if x["started_at"]), key=lambda x: x["started_at"], reverse=True)
    nobody = sorted((x for x in items if x["status"] == "unallocated"), key=lambda x: x["allocated_at"])
    waiting = sorted((x for x in items if x["status"] == "allocated"), key=lambda x: x["allocated_at"])
    items = started + nobody + waiting
    return jsonify(total=len(items), items=items[:100], ever=sum(len(ms) for ms in MARKERS.values()))


HISTORY_SORTS = ("status", "title", "marker", "who", "allocated", "started", "completed", "took")


@app.get("/api/markers/history")
def api_markers_history():
    """Every marker, live and finished, for the Task review pop-up. Optional: ?rocu= (one region), ?status= all|live|waiting|started|completed,
    ?type_id=, ?q= (every word must be found), ?sort= and ?dir=, ?offset= and ?limit= (50, at most 200)."""
    region, status, q = request.args.get("rocu", ""), request.args.get("status", "all"), " ".join(request.args.get("q", "").lower().split())[:100]
    sort = request.args.get("sort", "status") if request.args.get("sort", "status") in HISTORY_SORTS else "status"
    tid = request.args.get("type_id", type=int)
    now = datetime.now(timezone.utc)
    by_key = {}
    for v in STATE["victims"]:
        by_key.setdefault(v["key"], v)
    rows = []
    for key, ms in MARKERS.items():
        v = by_key.get(key)
        rocu = v["rocu"] if v else ""
        if region and rocu != region:
            continue
        for m in ms:
            if tid is not None and m["type_id"] != tid:
                continue
            x = {**_marker_view(m), "title": v["post_title"] if v else m["victim"], "group": (v or {}).get("group", ""), "sector": (v or {}).get("activity", ""),
                 "rocu": rocu, "town": (v or {}).get("town", ""), "updated_by": m.get("updated_by") or "",
                 "updated_at": m["updated_at"].isoformat() if m.get("updated_at") else None}
            if q and not all(w in " ".join((x["title"], x["group"], x["sector"], x["name"], x["initials"], x["by"], x["town"], x["rocu"])).lower() for w in q.split()):
                continue
            a, s, c = m["allocated_at"], m["started_at"], m["completed_at"]
            secs = lambda later, earlier: int((later - earlier).total_seconds()) if later and earlier else None
            x["to_start_s"], x["working_s"], x["to_finish_s"] = secs(s, a), secs(c, s), secs(c, a)
            x["to_allocate_s"] = secs(a, m.get("marked_at")) if m["initials"] and secs(a, m.get("marked_at")) else None       # how long a Task waited for somebody to take it
            x["took_s"] = x["to_finish_s"] if c else secs(now, a)          # a finished task: allocation to completion; a live one: how long so far
            rows.append(x)
    counts = {"all": len(rows), "live": sum(1 for x in rows if x["status"] != "completed"), "unallocated": sum(1 for x in rows if x["status"] == "unallocated"),
              "waiting": sum(1 for x in rows if x["status"] == "allocated"), "started": sum(1 for x in rows if x["status"] == "started"),
              "completed": sum(1 for x in rows if x["status"] == "completed")}
    mean = lambda xs: int(sum(xs) / len(xs)) if xs else None
    avg = {"to_start_s": mean([x["to_start_s"] for x in rows if x["to_start_s"] is not None and x["status"] == "completed"]),
           "to_finish_s": mean([x["to_finish_s"] for x in rows if x["to_finish_s"] is not None])}
    if status == "live":
        rows = [x for x in rows if x["status"] != "completed"]
    elif status in ("unallocated", "waiting", "started", "completed"):
        rows = [x for x in rows if x["status"] == {"waiting": "allocated"}.get(status, status)]
    if sort == "status":                  # what needs attention first: in progress (latest start first), nobody yet, waiting (longest first), then finished (latest first)
        rows = (sorted((x for x in rows if x["status"] == "started"), key=lambda x: _epoch(x["started_at"]), reverse=True)
                + sorted((x for x in rows if x["status"] == "unallocated"), key=lambda x: _epoch(x["allocated_at"]))
                + sorted((x for x in rows if x["status"] == "allocated"), key=lambda x: _epoch(x["allocated_at"]))
                + sorted((x for x in rows if x["status"] == "completed"), key=lambda x: _epoch(x["completed_at"]), reverse=True))
        if request.args.get("dir") == "desc":
            rows.reverse()
    else:
        field = {"title": "title", "marker": "name", "who": "initials", "allocated": "allocated_at", "started": "started_at", "completed": "completed_at", "took": "took_s"}[sort]
        text, dated = sort in ("title", "marker", "who"), sort in ("allocated", "started", "completed")
        desc = request.args.get("dir", "asc" if text else "desc") == "desc"
        have, none = [x for x in rows if x[field] is not None], [x for x in rows if x[field] is None]
        have.sort(key=(lambda x: (str(x[field]).lower(), x["title"].lower())) if text else (lambda x: _epoch(x[field])) if dated else (lambda x: x[field]), reverse=desc)
        rows = have + none                # a date that has not happened yet always sorts last
    off, lim = _int("offset", 0, 10**6), max(1, _int("limit", 50, 200))
    return jsonify(total=len(rows), offset=off, items=rows[off:off + lim], counts=counts, avg=avg, region=region)


# the older allocate API (Task only): kept as it was, now on top of markers
@app.put("/api/victims/alloc")
def victim_alloc_set():
    if LEGACY:
        return jsonify(error="Database not connected: allocation is unavailable."), 503
    b = request.get_json(silent=True) or {}
    v, initials = _find_victim(b.get("title")), (b.get("initials") or "").strip().upper()
    if not v:
        return jsonify(error="Unknown victim."), 404
    if not INITIALS_RE.match(initials):
        return jsonify(error="Initials must be 2 to 4 letters."), 400
    u, mt = current_user(), _builtin_type()
    mid, old = _marker_put(v, mt, initials, u)
    if b.get("remember") or not u.get("initials"):
        db.run("update users set initials=%s where id=%s", (initials, u["id"]))
    _marks_changed()
    log("victim_alloc", f'{v["post_title"]} -> {initials}' + (f' (took over from {old})' if old and old != initials else ""))
    return jsonify(ok=True, initials=initials)


@app.delete("/api/victims/alloc")
def victim_alloc_clear():
    if LEGACY:
        return jsonify(error="Database not connected."), 503
    v = _find_victim(request.args.get("title"))
    m = _live_task(v["key"]) if v else None
    if not m:
        return jsonify(error="That victim is not allocated."), 404
    u = current_user()
    if not _can_edit_marker(m, u):
        return jsonify(error=f'Only {_who(m)} or an admin can release this.'), 403
    db.run("delete from victim_markers where id=%s", (m["id"],))
    _marks_changed()
    log("victim_alloc_clear", f'{v["post_title"]} (was {m["initials"] or "unallocated"})')
    return jsonify(ok=True)


# ---- breach details: the gang responsible, and other places the breach is mentioned
def _slug(name):
    s = (name or "").strip().lower()
    return s if re.fullmatch(r"[a-z0-9_-]{2,40}", s) else ""        # only names that make a valid group-page address


def _gang_info(group):
    g = (group or "").strip().lower()
    gr = next((x for x in STATE["gangs"] if x["name"].lower() == g or (x.get("altname") or "").lower() == g), None)
    slug = _slug(gr["name"] if gr else group)
    sites = (gr or {}).get("leak_sites") or []
    return {"name": gr["name"] if gr else group, "altname": (gr or {}).get("altname") or "",
            "description": _plain((gr or {}).get("description"), 360), "added": (gr or {}).get("added_date") or "",
            "uk_victims": sum(1 for x in STATE["victims"] if (x["group"] or "").lower() == g),
            "sites": len(sites), "online": sum(1 for s in sites if s.get("available") is True or s.get("status") == "ONLINE"),
            "profile_url": f"https://www.ransomware.live/group/{slug}" if slug else "",
            "ransomlook_url": f"https://www.ransomlook.io/group/{slug}" if slug and gr and "ransomlook" in gr["sources"] else ""}


def _mentions(v, limit=8):
    """Items in the dashboard's own alert and news feeds that name this victim, within 30 days of the post. Matched by the
    whole company name; names too short or generic to match on their own are skipped, so a mention is never a guess."""
    name = v["key"]
    t0 = _ts(v.get("seen")) or _ts(v.get("discovered"))
    if not name or not t0 or (len(name) < 7 and " " not in name):
        return []
    rx = re.compile(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])")
    out, seen_urls = [], set()

    def consider(title, summary, url, published, source, kind):
        t = _ts(published)
        u = _abs_url(url, "")
        if not t or not u or u in seen_urls or abs((t - t0).total_seconds()) > 30 * 86400:
            return
        if rx.search(geo.norm((title or "") + " " + (summary or ""))):
            seen_urls.add(u)
            out.append({"source": source, "kind": kind, "title": (title or "")[:200], "url": u, "date": t.isoformat()})

    for it in STATE["intel"]:
        consider(it.get("title"), it.get("summary"), it.get("url"), it.get("published"), it.get("source") or "", it.get("kind") or "")
    for n in STATE["news"]:
        consider(n.get("title"), n.get("summary"), n.get("url"), n.get("published"), n.get("source_name") or n.get("source") or "", "news")
    return sorted(out, key=lambda m: m["date"], reverse=True)[:limit]


# ---- the company, as the Companies House register holds it (read when a summary is opened, then kept for a while)
CH_URL = "https://find-and-update.company-information.service.gov.uk"
CH_CACHE_FILE = DATA_DIR / "ch_company.json"
_CH_TTL = {"ok": 7 * 86400, "no_match": 86400}                  # failures (rejected key, busy, errors) are never kept
_CH_STATUS = {"auth-error": "auth_error", "rate-limited": "rate_limited", "no-match": "no_match", "error": "error"}
_ch_lock = threading.Lock()
try:
    _CH_CACHE = json.loads(CH_CACHE_FILE.read_text())
except Exception:
    _CH_CACHE = {}


def _ch_cached(key):
    e = _CH_CACHE.get(key)
    t = _ts(e.get("fetched")) if e else None
    return e if t and (datetime.now(timezone.utc) - t).total_seconds() < _CH_TTL.get(e.get("status"), 0) else None


def _ch_store(key, entry):
    entry["fetched"] = _now_iso()
    with _ch_lock:
        _CH_CACHE[key] = entry
        try:
            CH_CACHE_FILE.write_text(json.dumps(_CH_CACHE))
        except Exception:
            pass
    return entry


@app.get("/api/victims/company")
def victim_company():
    v = _find_victim(request.args.get("title"))
    if not v:
        return jsonify(error="Unknown victim."), 404
    search_url = f"{CH_URL}/search?q=" + quote(v["post_title"])
    if not geo.CH_ENABLED:
        return jsonify(status="disabled", search_url=search_url)
    hit = _ch_cached(v["key"])
    if hit:
        return jsonify(dict(hit, search_url=search_url))
    g_ = GEO.get(v["key"]) or {}
    number, matched = (g_["number"], g_.get("confidence") or "name") if g_.get("source") == "companies-house" and g_.get("number") else ("", "")
    if not number:                                    # not placed through the register: search it now, with the same matching rules
        f = geo.find_company(v["post_title"], v.get("website", ""), strict=bool(v.get("text_overseas")))
        if f["status"] != "found":
            out = {"status": _CH_STATUS.get(f["status"], "error")}
            if out["status"] == "no_match":
                out = _ch_store(v["key"], out)
            return jsonify(dict(out, search_url=search_url))
        number, matched = f["item"].get("company_number") or "", f["confidence"]
    number = number.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,10}", number):         # only ever build a register address from a well-formed number
        return jsonify(status="error", search_url=search_url)
    d = geo.company_details(number)
    if d["status"] != "ok":
        return jsonify(status=_CH_STATUS.get(d["status"], "error"), search_url=search_url)
    out = _ch_store(v["key"], {"status": "ok", "matched_by": matched, "company": d["company"],
                               "officers": d["officers"][:60] if d["officers"] is not None else None,
                               "url": f"{CH_URL}/company/{number}", "officers_url": f"{CH_URL}/company/{number}/officers"})
    return jsonify(dict(out, search_url=search_url))


@app.get("/api/victims/context")
def victim_context():
    v = _find_victim(request.args.get("title"))
    if not v:
        return jsonify(error="Unknown victim."), 404
    return jsonify(gang=_gang_info(v["group"]), mentions=_mentions(v))


# ----------------------------------------------------------------- data queries
def _region():
    return request.args.get("rocu", "")


def _uk():
    """UK victims, narrowed to the selected ROCU region when ?rocu= is given."""
    r = _region()
    v = STATE["victims"]
    return [x for x in v if x["rocu"] == r] if r else v


def _share(n, total):
    return round(100 * n / total, 1) if total else 0


def _counts(key, label_default="Unknown"):
    return Counter((v.get(key) or label_default) for v in _uk())


def _int(name, default, hi=500):
    try:
        return max(0, min(int(request.args.get(name, default)), hi))
    except ValueError:
        return default


def _ts(s):
    """Parse an ISO timestamp to an aware UTC datetime (None if it cannot be read)."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _prev_visit():
    p = (current_user() or {}).get("prev_visit")
    if isinstance(p, datetime):
        return p if p.tzinfo else p.replace(tzinfo=timezone.utc)
    return _ts(p) if p else None


def _why_unplaced(v):
    """A plain-English reason a victim has no region yet, so a missing placement is never a mystery."""
    fl = v.get("flag")
    if fl and fl.get("applied"):
        what = "not in the UK" if fl["kind"] == "not_uk" else f'not in {fl["region"]}'
        return f'Flagged as a false positive by {fl["by"]} ({what})' + (f': "{fl["note"]}"' if fl["note"] else "") + ". Undo the flag to restore the automatic placement."
    if v["rocu"] == geo.OVERSEAS and v.get("overseas_weak"):
        return (f'Its description gives only a country ("{v["text_overseas"]}"). A Companies House match confirmed by its website '
                "would place it in the UK instead; an admin can also set it by hand.")
    if v["rocu"] != "Unplaced":
        return ""
    st = (GEO.get(v["key"]) or {}).get("status")
    if st == "no-match":
        return "Looked up in Companies House: no company matched this name. An admin can set its region by hand."
    if st in ("no-postcode", "no-force"):
        return "A company matched, but its registered address could not be turned into a police region."
    if not geo.CH_ENABLED:
        return "Companies House lookup is off (no COMPANIES_HOUSE_KEY set)."
    if geo.STATUS.get("auth_error"):
        return "Companies House rejected the API key, so no lookups can run. Check COMPANIES_HOUSE_KEY in Railway."
    d = (v.get("description") or "").strip().lower()
    if d in ("", "n/a", "na", "none", "-"):
        return "Its description has no location. It will be looked up in Companies House by name (lookups run in batches after each refresh)."
    return "Not looked up yet: lookups run in batches after each refresh."


def _alloc_view(v):
    a = ALLOC.get(v["key"])
    return {"initials": a["initials"], "by": a["allocated_by"], "at": a["allocated_at"].isoformat()} if a else None


VIEW_KEYS = ("manual_breach", "added_by", "post_title", "group", "activity", "discovered", "seen", "year", "website", "post_url", "rocu", "force", "force_name",
             "town", "region_source", "registered_name", "manual", "override_note", "description", "reposts", "overseas_weak",
             "data", "data_basis", "ransom")


def _view(v, prev):
    """The fields the browser needs for a victim (not the internal matching fields), plus 'new since your last visit'."""
    d = {k: v.get(k, "") for k in VIEW_KEYS}
    t = _ts(v.get("seen"))
    d["new"] = bool(prev and t and t > prev)
    d["why"] = _why_unplaced(v)
    d["flag"] = v.get("flag")
    d["alloc"] = _alloc_view(v)
    d["markers"] = [_marker_view(m) for m in MARKERS.get(v["key"], [])]
    return d


@app.get("/api/summary")
def api_summary():
    v = _uk()
    allv = STATE["victims"]
    years = Counter(x["year"] for x in v if x["year"] != "unknown")
    by_region = Counter(x["rocu"] for x in allv)
    src = Counter(x["region_source"] or "none" for x in allv)
    sectors = _counts("activity")
    unclassified = sectors.pop("Unclassified", 0)       # kept out of the ranking, reported separately
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    prev = _prev_visit()
    return jsonify(
        uk_total=len(v), uk_all=len(allv), region=_region(), gangs_total=len(STATE["gangs"]),
        new_total=sum(1 for x in v if prev and (_ts(x.get("seen")) or prev) > prev),
        news_7d=sum(1 for n in STATE["news"] if (_ts(n["published"]) or cutoff) > cutoff),
        by_year=[{"year": y, "count": n} for y, n in sorted(years.items())][-8:],
        top_gangs=[{"group": g_, "count": n} for g_, n in _counts("group").most_common(8)],
        top_sectors=[{"sector": s, "count": n} for s, n in sectors.most_common(8)],
        unclassified={"count": unclassified, "share": _share(unclassified, len(v))},
        sector_source="Sector as classified by ransomware.live; victims it has not classified are listed separately.",
        by_region=[{"region": r, "count": by_region.get(r, 0)} for r in geo.FILTER_REGIONS],
        placement={"total": len(allv), "placed": len(allv) - by_region.get("Unplaced", 0) - by_region.get(geo.OVERSEAS, 0),
                   "from_text": src.get("text", 0), "from_postcode": src.get("postcode", 0),
                   "from_companies_house": src.get("companies-house", 0), "from_manual": src.get("manual", 0),
                   "overseas": by_region.get(geo.OVERSEAS, 0), "unplaced": by_region.get("Unplaced", 0),
                   "flagged": sum(1 for x in allv if x.get("flag_applied")),
                   "looked_up": sum(1 for x in GEO.values() if geo.is_terminal(x)),
                   "api_enabled": geo.CH_ENABLED, "running": _geo_running,
                   "lookup": {"auth_error": bool(geo.STATUS.get("auth_error")), "rate_limited": bool(geo.STATUS.get("rate_limited")),
                              "last_run": geo.STATUS.get("last_run"),
                              "no_match": sum(1 for x in allv if x["rocu"] == "Unplaced" and (GEO.get(x["key"]) or {}).get("status") == "no-match"),
                              "pending": sum(1 for x in allv if x["rocu"] == "Unplaced" and not geo.is_terminal(GEO.get(x["key"])))}})


KINDS = {"year": ("year", "UK victims by year"), "gang": ("group", "UK victims by gang"),
         "sector": ("activity", "UK victims by sector")}


@app.get("/api/summary/detail")
def api_detail():
    kind = request.args.get("kind", "sector")
    key, title = KINDS.get(kind, KINDS["sector"])
    total = len(_uk())
    c = _counts(key)
    unclassified = c.pop("Unclassified", 0) if kind == "sector" else 0
    items = [{"label": k, "count": n, "share": _share(n, total)} for k, n in c.most_common(30)]
    if unclassified:
        items.append({"label": "Unclassified", "count": unclassified, "share": _share(unclassified, total)})
    return jsonify(title=title + (f" ({_region()})" if _region() else ""), items=items, total=total)


@app.get("/api/summary/slice")
def api_slice():
    kind = request.args.get("kind", "sector")
    label = request.args.get("label", "")
    key, _ = KINDS.get(kind, KINDS["sector"])
    rows = [v for v in _uk() if (v.get(key) or "Unknown") == label]
    other = "activity" if kind != "sector" else "group"
    split = Counter((v.get(other) or "Unknown") for v in rows).most_common(12)
    explain = {"year": f"Victims first published in {label}.", "gang": f"Victims claimed by {label}.",
               "sector": f"Victims in the {label} sector."}[kind if kind in KINDS else "sector"]
    rows.sort(key=lambda v: str(v.get("seen") or ""), reverse=True)
    prev = _prev_visit()
    return jsonify(label=label, explain=explain, count=len(rows), share=_share(len(rows), len(_uk())),
                   split_title="By sector" if other == "activity" else "By gang",
                   split=[{"label": k, "count": n} for k, n in split], victims=[_view(v, prev) for v in rows[:40]])


SORTS = {"name": lambda v: v["post_title"].lower(), "group": lambda v: v["group"].lower(),
         "sector": lambda v: v["activity"].lower(), "seen": lambda v: str(v.get("seen") or ""),
         "region": lambda v: (v["rocu"].lower(), (v.get("town") or "").lower())}


def _filter_victims():
    mark = request.args.get("mark", "")
    if mark == "flagged":                    # false positives, including ones flagged out of the selected region
        r = _region()
        items = [v for v in STATE["victims"] if v.get("flag") and (not r or r in (v["rocu"], v["flag"]["region"]))]
    else:
        items = _uk()
    if mark == "alloc":
        items = [v for v in items if _live_markers(v["key"])]
    elif mark == "mine":
        u = current_user()
        ini = u.get("initials") or ""
        items = [v for v in items if any(m["allocated_by"] == u["username"] or (ini and m["initials"] == ini) for m in _live_markers(v["key"]))]
    if request.args.get("group"):
        items = [v for v in items if v["group"] == request.args["group"]]
    if request.args.get("sector"):
        items = [v for v in items if v["activity"] == request.args["sector"]]
    if request.args.get("title"):
        items = [v for v in items if v["post_title"] == request.args["title"]]
    q = request.args.get("q", "").strip()
    if q:           # every word must be found (name, registered name, gang, website, place, region, sector, description): same rules as the search box
        items = [r[1] for r in _search_rank(q, [(v, _victim_fields(v)) for v in items])]
    if request.args.get("new") == "1":
        prev = _prev_visit()
        items = [v for v in items if prev and (_ts(v.get("seen")) or prev) > prev]
    key = request.args.get("sort", "seen")
    desc = request.args.get("dir", "desc") != "asc"
    return sorted(items, key=SORTS.get(key, SORTS["seen"]), reverse=desc)


@app.get("/api/victims")
def api_victims():
    items = _filter_victims()
    off, lim = _int("offset", 0, 10**6), max(1, _int("limit", 50, 500))
    allv = STATE["victims"]
    prev = _prev_visit()
    facets = {"groups": sorted({v["group"] for v in allv if v["group"]}, key=str.lower),
              "sectors": sorted({v["activity"] for v in allv if v["activity"]})}
    return jsonify(total=len(items), offset=off, limit=lim, items=[_view(v, prev) for v in items[off:off + lim]],
                   facets=facets, new_total=sum(1 for v in _uk() if _view(v, prev)["new"]))


@app.get("/api/victims.csv")
def api_victims_csv():
    items = _filter_victims()[:5000]
    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(["victim", "town_or_district", "region_rocu", "police_force", "region_basis", "group", "sector",
                 "first_seen_utc", "website", "false_positive_flag", "allocated_to", "live_markers"])
    safe = lambda s: ("'" + str(s)) if str(s)[:1] in ("=", "+", "-", "@") else str(s)      # no spreadsheet formula injection
    for v in items:
        wr.writerow([safe(v["post_title"]), safe(v.get("town") or ""), v["rocu"], v.get("force_name", ""),
                     v.get("region_source", ""), safe(v["group"]), v["activity"], v.get("seen", ""), safe(v["website"]),
                     (v["flag"]["kind"].replace("_", " ") + " by " + v["flag"]["by"]) if v.get("flag") and v["flag"]["applied"] else "",
                     (ALLOC.get(v["key"]) or {}).get("initials", ""),
                     safe("; ".join(f'{(MARKER_TYPES.get(m["type_id"]) or {}).get("name", "?")}: ' + (f'{m["initials"]} ({"in progress" if m["started_at"] else "allocated"})' if m["initials"] else "unallocated")
                                    for m in _live_markers(v["key"])))])
    log("export", f"victims csv ({len(items)} rows" + (f", {_region()}" if _region() else "") + ")")
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=evileye-victims.csv"})


@app.get("/api/gangs")
def api_gangs():
    q = request.args.get("q", "").lower()
    uk_by_group = Counter((v["group"] or "").lower() for v in _uk())
    items = [dict(gr, activity=uk_by_group.get(gr["name"].lower(), 0)) for gr in STATE["gangs"]]
    if request.args.get("source"):
        items = [x for x in items if request.args["source"] in x["sources"]]
    if q:
        items = [x for x in items if q in (x["name"] + " " + (x["altname"] or "") + " " + x["description"]).lower()]
    if request.args.get("live") == "1":
        items = [x for x in items if any(s.get("available") is True or s.get("status") == "ONLINE" for s in x["leak_sites"])]
    if request.args.get("sort") == "active":
        items = sorted(items, key=lambda x: (-x["activity"], x["name"].lower()))
    return jsonify(total=len(items), items=items[:400])


@app.get("/api/intel")
def api_intel():
    q = request.args.get("q", "").lower()
    items = STATE["intel"]
    if request.args.get("source"):
        items = [i for i in items if i["source"] == request.args["source"]]
    if q:       # the readable fields (a JSON dump escapes quotes and accents, so a title containing either could never match)
        items = [i for i in items if q in " ".join(str(i.get(k) or "") for k in ("title", "source", "kind", "summary", "url")).lower()]
    return jsonify(total=len(items), items=items[:_int("limit", 120)])


# ---- search across everything the dashboard has cached: breaches, gangs, news and alerts
SEARCH_CAPS = {"breaches": 8, "gangs": 3, "news": 4, "alerts": 3}
_search_rate = defaultdict(deque)


def _search_rank(q, rows):
    """rows: (item, [(field_text, weight_at_word_start, weight_inside_a_word), ...]) -> [(score, item, only_weak)] best first.
    Every word typed must be found somewhere in the item. A word scores by the best field it is in (a name beats a gang
    beats a description; a weight of 0 means that kind of hit does not count), and a name that starts with, or equals,
    the whole query gets a bonus. only_weak is True when nothing but a description matched."""
    toks = [t for t in re.split(r"\s+", q.strip().lower()) if t][:6]
    if not toks:
        return []
    pats = [re.compile(r"(?<![a-z0-9])" + re.escape(t)) for t in toks]
    out = []
    for item, fields in rows:
        total, strong, ok = 0, False, True
        for t, p in zip(toks, pats):
            best = 0
            for text, w_word, w_in, *_ in fields:
                if p.search(text):
                    best = max(best, w_word)
                elif t in text:
                    best = max(best, w_in)
            if not best:
                ok = False
                break
            strong = strong or best > 5
            total += best
        if not ok:
            continue
        head = fields[0][0]
        if head == q.strip().lower():
            total += 60
        elif head.startswith(q.strip().lower()):
            total += 30
        out.append((total, item, not strong))
    return out


def _f(text, w_word, w_in):
    """One searchable field: its lower-cased text and how much a hit on a whole word / inside a word is worth."""
    return ((text or "").lower(), w_word, w_in)


def _epoch(s):
    t = _ts(s)
    return t.timestamp() if t else 0.0


def _victim_fields(v):
    """What a breach is searched on, best first: its name, the registered name, then gang / website domain / place / region / sector,
    then the description (weak, and only at the start of a word). Used by the search box and by the UK Victims filter, so both agree."""
    site = re.sub(r"^https?://", "", v.get("website") or "").split("/")[0]
    return [_f(v.get("post_title"), 30, 22), _f(v.get("registered_name"), 18, 18),
            _f(" ".join([v.get("group") or "", site, v.get("town") or "", v.get("force_name") or "",
                         "" if v["rocu"] == "Unplaced" else v["rocu"], v.get("activity") or ""]), 14, 10),
            _f(v.get("description"), 3, 0)]


@app.get("/api/search")
def api_search():
    u = current_user()
    hist = _search_rate[u["id"] if u and u.get("id") is not None else "legacy"]
    now = time.time()
    while hist and now - hist[0] > 60:
        hist.popleft()
    if len(hist) >= 300:
        return jsonify(error="Too many searches: wait a moment."), 429
    hist.append(now)
    q = (request.args.get("q") or "").strip()[:200]
    if re.match(r"https?://", q, re.I):          # a pasted web address: search on its domain (the path is never searched)
        q = re.sub(r"^https?://", "", q, flags=re.I).split("/")[0].split("?")[0]
    q = q[:80]
    if len(q) < 2:
        return jsonify(q=q, short=True, **{k: {"total": 0, "items": []} for k in SEARCH_CAPS})
    prev = _prev_visit()

    hits = sorted(_search_rank(q, [(v, _victim_fields(v)) for v in STATE["victims"]]), key=lambda r: (-r[0], -_epoch(r[1].get("seen"))))
    breaches = []
    for _, v, weak in hits[:SEARCH_CAPS["breaches"]]:
        t, fl = _ts(v.get("seen")), v.get("flag")
        breaches.append({"title": v["post_title"], "group": v.get("group") or "", "sector": v.get("activity") or "", "rocu": v["rocu"],
                         "town": v.get("town") or "", "force": v.get("force_name") or "", "seen": v.get("seen") or "",
                         "new": bool(prev and t and t > prev), "fp": bool(fl and fl.get("applied")),
                         "alloc": (ALLOC.get(v["key"]) or {}).get("initials", ""), "via": "description" if weak else ""})

    grows = [(g, [_f(g.get("name"), 30, 22), _f(g.get("altname"), 26, 20), _f(g.get("description"), 3, 0)]) for g in STATE["gangs"]]
    ghits = sorted(_search_rank(q, grows), key=lambda r: (-r[0], (r[1].get("name") or "").lower()))
    by_group = Counter((v.get("group") or "").lower() for v in STATE["victims"]) if ghits else {}
    gangs = [{"name": g["name"], "altname": g.get("altname") or "", "uk_victims": by_group.get((g["name"] or "").lower(), 0)}
             for _, g, _ in ghits[:SEARCH_CAPS["gangs"]]]

    nrows = [(n, [_f(n.get("title"), 30, 22), _f(" ".join([n.get("source_name") or "", *(n.get("themes") or []), *(n.get("groups") or [])]), 12, 10),
                  _f(n.get("summary"), 3, 0)]) for n in STATE["news"]]
    nhits = sorted(_search_rank(q, nrows), key=lambda r: (-r[0], -_epoch(r[1].get("published"))))
    news = [{"id": n.get("id"), "title": n.get("title") or "", "source": n.get("source_name") or "", "published": n.get("published") or "", "uk": bool(n.get("uk"))}
            for _, n, _ in nhits[:SEARCH_CAPS["news"]]]

    # press / leak-post rows only repeat a breach (they are listed under "Where this was sourced from"), so the alerts here are the real alerts
    arows = [(i, [_f(i.get("title"), 30, 22), _f((i.get("source") or "") + " " + (i.get("kind") or ""), 12, 10), _f(i.get("summary"), 3, 0)])
             for i in STATE["intel"] if i.get("kind") not in ("press", "leak-post")]
    ahits = sorted(_search_rank(q, arows), key=lambda r: (-r[0], -_epoch(r[1].get("published"))))
    alerts = [{"title": i.get("title") or "", "source": i.get("source") or "", "kind": i.get("kind") or "", "published": i.get("published") or ""}
              for _, i, _ in ahits[:SEARCH_CAPS["alerts"]]]

    return jsonify(q=q, short=False, breaches={"total": len(hits), "items": breaches}, gangs={"total": len(ghits), "items": gangs},
                   news={"total": len(nhits), "items": news}, alerts={"total": len(ahits), "items": alerts})


# ---- dark web news
def _news_public(n):
    d = {k: n.get(k) for k in ("id", "title", "url", "source", "source_name", "published", "summary", "cats", "themes", "groups", "uk")}
    d["thumb"] = bool(n.get("thumb"))
    return d


@app.get("/api/news")
def api_news():
    items = STATE["news"]
    src, theme, q = request.args.get("source"), request.args.get("theme"), request.args.get("q", "").lower()
    days = _int("days", 0, 365)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items = [n for n in items if (_ts(n["published"]) or cutoff) >= cutoff]
    if src:
        items = [n for n in items if n["source"] == src]
    if theme:
        items = [n for n in items if theme in n["themes"]]
    if request.args.get("uk") == "1":
        items = [n for n in items if n["uk"]]
    if request.args.get("group"):
        items = [n for n in items if request.args["group"] in n["groups"]]
    if q:
        items = [n for n in items if q in (n["title"] + " " + n["summary"]).lower()]
    off, lim = _int("offset", 0, 10**6), max(1, _int("limit", 40, 200))
    sources = Counter((n["source"], n["source_name"]) for n in STATE["news"])
    return jsonify(total=len(items), items=[_news_public(n) for n in items[off:off + lim]],
                   sources=[{"id": i, "name": nm, "count": c} for (i, nm), c in sources.most_common()],
                   themes=[t for t, _ in THEMES])


@app.get("/api/news/thumb/<nid>")
def api_news_thumb(nid):
    """A small cached JPEG of the post's image. Only images attached to known news items are fetched (never a URL from the
    browser), only from public hosts, only real raster images, at most 3 MB before shrinking."""
    if not re.fullmatch(r"[0-9a-f]{16}", nid):
        return "", 404
    item = next((n for n in STATE["news"] if n.get("id") == nid), None)
    if not item or not item.get("thumb"):
        return "", 404
    f, miss = THUMB_DIR / f"{nid}.jpg", THUMB_DIR / f"{nid}.none"
    if not f.exists():
        if miss.exists() and time.time() - miss.stat().st_mtime < 3600:
            return "", 404
        got = _safe_get(item["thumb"], 3_000_000, "image/*")
        try:
            if not got or not got[0].startswith("image/"):
                raise ValueError("not an image")
            img = Image.open(io.BytesIO(got[1]))
            if img.format not in ("JPEG", "PNG", "WEBP", "GIF"):
                raise ValueError("unsupported format")
            img.load()
            img.thumbnail((360, 240))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            tmp = f.with_suffix(".tmp")
            img.convert("RGB").save(tmp, "JPEG", quality=78, optimize=True)
            tmp.replace(f)
        except Exception:
            miss.write_bytes(b"")
            return "", 404
    resp = send_file(f, mimetype="image/jpeg", max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.get("/api/news/top")
def api_news_top():
    """The three items to show on the welcome page: highest score after a small recency penalty, at most two per source."""
    now = datetime.now(timezone.utc)
    ranked = []
    for n in STATE["news"]:
        t = _ts(n["published"])
        if t and (now - t) <= timedelta(days=7):
            ranked.append((n["score"] - (now - t).total_seconds() / 3600 / 24, n))
    out, per = [], Counter()
    for _, n in sorted(ranked, key=lambda x: -x[0]):
        if per[n["source"]] < 2:
            out.append(_news_public(n))
            per[n["source"]] += 1
        if len(out) == 3:
            break
    return jsonify(items=out, total_7d=len(ranked))


@app.get("/api/news/summary")
def api_news_summary():
    """A rule-based briefing built from the headlines, themes, groups and sources: it does not read the articles."""
    days = max(1, _int("days", 7, 30))
    now = datetime.now(timezone.utc)
    cur, prev = [], []
    for n in STATE["news"]:
        t = _ts(n["published"])
        if not t:
            continue
        age = now - t
        if age <= timedelta(days=days):
            cur.append(n)
        elif age <= timedelta(days=2 * days):
            prev.append(n)
    themes = Counter(t for n in cur for t in n["themes"])
    groups = Counter(g_ for n in cur for g_ in n["groups"])
    sources = Counter(n["source_name"] for n in cur)
    dwi = [n for n in cur if n["source"] == "darkwebinformer"]
    dwi_cats = Counter(c for n in dwi for c in n["cats"])
    uk = [n for n in cur if n["uk"]]
    # Feeds hold different amounts of history (some a few days, some weeks). A week-on-week change is only honest for
    # sources whose feed reaches back across BOTH windows, so compare just those.
    oldest = {}
    for n in STATE["news"]:
        t = _ts(n["published"])
        if t:
            oldest[n["source"]] = min(oldest.get(n["source"], t), t)
    deep = {sid for sid, t in oldest.items() if now - t >= timedelta(days=2 * days)}
    c_deep = sum(1 for n in cur if n["source"] in deep)
    p_deep = sum(1 for n in prev if n["source"] in deep)
    coverage = len(deep)
    change = round(100 * (c_deep - p_deep) / p_deep) if p_deep and c_deep is not None and deep else None
    th = themes.most_common()
    claims = sum(c for k, c in dwi_cats.items() if re.search(r"breach|leak", k, re.I))
    trend = (f"{'up' if change >= 0 else 'down'} {abs(change)}% on the previous {days} days" if change is not None else None)
    # structured points (icon key, label, text) for the page, plus a one-line headline
    points, headline = [], ""
    if cur:
        points.append({"k": "volume", "label": "Volume", "text": f"{len(cur)} items from {len(sources)} sources" + (f", {trend}" if trend else "")})
        if th:
            lead = f"{th[0][0]}: {th[0][1]} items ({round(100 * th[0][1] / len(cur))}%)"
            if len(th) > 1:
                lead += ", then " + " and ".join(f"{t} ({c})" for t, c in th[1:3])
            points.append({"k": "theme", "label": "Leading themes", "text": lead})
            headline = f"{th[0][0]} leads: {th[0][1]} of {len(cur)} items ({round(100 * th[0][1] / len(cur))}%)"
        if groups:
            points.append({"k": "groups", "label": "Ransomware groups named", "text": ", ".join(f"{g_} ({c})" for g_, c in groups.most_common(3))})
        if dwi:
            points.append({"k": "claims", "label": "Dark Web Informer", "text": f"{len(dwi)} posts, {claims} of them data breach or leak claims"})
        points.append({"k": "uk", "label": "UK", "text": f"{len(uk)} item{'s' if len(uk) != 1 else ''} mention the UK" if uk else "No items mention the UK"})
    else:
        headline = f"No dark web news in the last {days} days yet"
    briefing = " ".join([headline + "."] + [f"{p['label']}: {p['text']}." for p in points]) if cur else headline + ": refresh to load the feeds."
    return jsonify(days=days, total=len(cur), previous=len(prev), change=change, comparable_sources=coverage, briefing=briefing,
                   headline=headline, points=points, delta=trend,
                   themes=[{"theme": t, "count": c, "share": _share(c, len(cur))} for t, c in th],
                   groups=[{"group": g_, "count": c} for g_, c in groups.most_common(8)],
                   sources=[{"source": s, "count": c} for s, c in sources.most_common()],
                   claim_types=[{"type": k, "count": c} for k, c in dwi_cats.most_common(6)],
                   uk_count=len(uk), uk_items=[_news_public(n) for n in uk[:4]],
                   note="Built from headlines, categories and ransomware-group names. It does not read the articles.")


@app.get("/api/ticker")
def api_ticker():
    """The most recent UK victims, and the most recent in the signed-in user's region (five each unless ?uk= / ?reg= ask
    for more). Read from the server's cache only: this never calls an external service."""
    u = current_user()
    region = u["region"]
    prev = _prev_visit()
    # skip anonymous "Company #123 [PENDING EXPOSURE]" placeholders: they say nothing useful in a ticker
    named = [v for v in STATE["victims"] if not re.match(r"^Company #\d+", v["post_title"])]
    recent = sorted(named, key=lambda v: str(v.get("seen") or ""), reverse=True)
    uk_recent = [v for v in recent if v["rocu"] != geo.OVERSEAS]       # probable overseas HQs are not UK incidents
    pick = lambda v: {"title": v["post_title"], "group": v["group"], "sector": v["activity"], "seen": v.get("seen"),
                      "rocu": v["rocu"], "force": v["force_name"], "town": v.get("town", ""), "website": v["website"],
                      "new": _view(v, prev)["new"], "manual": bool(v.get("manual")), "flag": v.get("flag"), "alloc": _alloc_view(v),
                      "markers": [_marker_view(m) for m in MARKERS.get(v["key"], [])]}
    n_uk, n_reg = max(1, _int("uk", 5, 1500)), max(1, _int("reg", 5, 1500))      # "View more" asks for longer lists
    reg_all = [x for x in recent if x["rocu"] == region] if region else []
    return jsonify(region=region, uk=[pick(v) for v in uk_recent[:n_uk]], uk_total=len(uk_recent),
                   regional=[pick(v) for v in reg_all[:n_reg]], regional_total=len(reg_all),
                   updated=STATE.get("last_refresh", {}).get("rl_victims_gb"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
