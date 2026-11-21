"""EvilEye - Dashboard backend.

Data refresh policy: there is NO background loop. External feeds are fetched only
  * when a signed-in user opens the dashboard and the stored data is stale (STALE_MINUTES), or
  * when a user presses Refresh.
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
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, Response, g, jsonify, redirect, request, send_file, send_from_directory, session
from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

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
ALLOC = {}              # victim key -> {initials, allocated_by, allocated_at}: "I am working on this"


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
        ALLOC.clear()
        ALLOC.update({r["key"]: r for r in db.many("select key, initials, allocated_by, allocated_at from victim_alloc")})
    except Exception as e:
        STATE["errors"]["marks"] = str(e)


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
            rec = {
                "post_title": title, "key": geo.norm(title), "group": v.get("group_name") or "unknown",
                "activity": _sector(v.get("activity")), "discovered": v.get("published") or v.get("discovered"),
                "seen": v.get("discovered") or v.get("published") or "", "year": _year(v),
                "website": (v.get("website") or "").strip(), "post_url": v.get("post_url") or "",
                "description": desc[:700], "reposts": 0,
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
        victims = list(rows.values())
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


def _ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0]).strip()[:64]


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
    except Exception as e:                      # keep serving; login reports the database error
        STATE["errors"]["db"] = str(e)
if STATE["victims"]:
    _apply_geo(STATE["victims"])


def current_user():
    if hasattr(g, "user"):
        return g.user
    g.user = None
    uid = session.get("uid")
    if LEGACY:
        if uid == "legacy" and session.get("ok"):
            g.user = {"id": 0, "username": "admin", "role": "admin", "region": session.get("region", ""), "legacy": True,
                      "prev_visit": session.get("prev_visit"), "onboarded_at": session.get("onboarded"), "must_change": False}
    elif isinstance(uid, int):
        try:
            row = db.one("select id, username, role, region, active, password_hash, prev_visit, onboarded_at, must_change, initials from users where id=%s", (uid,))
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
        return redirect("/login")
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and p.startswith("/api/") \
            and request.headers.get("X-Requested-With") != "fetch":
        return jsonify(error="bad request"), 400          # CSRF guard: browsers cannot add this cross-site
    if user.get("must_change") and p not in ("/", "/logout", "/health", "/api/me", "/api/me/password"):
        return jsonify(error="Set your own password first.", code="password_change_required"), 403
    return None


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


def _locked(username, ip):
    """Lock a username after 6 failures in 15 minutes. The per-IP limit is much higher so one shared office
    address is not locked out by a single person's typos."""
    row = db.one("select count(*) filter (where lower(username)=lower(%s)) as by_user, "
                 "count(*) filter (where ip=%s) as by_ip from activity "
                 "where event='login_failed' and ts > now() - interval '15 minutes'", (username, ip))
    return row["by_user"] >= 6 or row["by_ip"] >= 40


@app.get("/login")
def login_page():
    return (BASE / "login.html").read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/login")
def login_post():
    username = (request.form.get("username") or "").strip()[:64]
    pw = request.form.get("password") or ""
    if LEGACY:
        if PASSWORD and hmac.compare_digest(pw.encode(), PASSWORD.encode()):
            session.clear()
            session.update(uid="legacy", ok=True)
            session.permanent = True
            return redirect("/")
        time.sleep(1)
        return redirect("/login?error=1")
    try:
        bootstrap_admin()
        if _locked(username, _ip()):
            log("login_failed", "locked out (too many attempts)", username=username)
            time.sleep(1)
            return redirect("/login?error=2")
        row = db.one("select * from users where lower(username)=lower(%s)", (username,))
        ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, pw) and bool(row) and row["active"]
        if not ok:
            log("login_failed", "disabled account" if row and not row["active"] else "bad credentials", username=username)
            time.sleep(1)
            return redirect("/login?error=1")
        session.clear()
        session.update(uid=row["id"], pwv=_pwv(row["password_hash"]))
        session.permanent = True
        db.run("update users set last_login=now() where id=%s", (row["id"],))
        log("login", "", user=row)
        return redirect("/")
    except Exception:
        return redirect("/login?error=3")


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
    return send_from_directory(BASE / "static", "index.html")


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
    return jsonify(username=u["username"], role=u["role"], region=u["region"], database=not LEGACY,
                   regions=geo.FILTER_REGIONS, initials=u.get("initials") or "", prev_visit=p.isoformat() if p else None, onboarded=bool(u.get("onboarded_at")),
                   must_change=bool(u.get("must_change")))


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
    rows = db.many("""select u.id, u.username, u.role, u.region, u.active, u.created_at, u.created_by, u.last_login,
                      (select count(*) from activity a where a.user_id = u.id) as events,
                      (select max(a.ts) from activity a where a.user_id = u.id) as last_activity, u.must_change
                      from users u order by lower(u.username)""")
    return jsonify(users=[{**r, "created_at": r["created_at"].isoformat(),
                           "last_login": r["last_login"].isoformat() if r["last_login"] else None,
                           "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None} for r in rows],
                   regions=geo.FILTER_REGIONS)


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
    u, prev = current_user(), ALLOC.get(v["key"])
    db.run("""insert into victim_alloc (key, victim, initials, allocated_by) values (%s,%s,%s,%s)
              on conflict (key) do update set initials=excluded.initials, allocated_by=excluded.allocated_by, allocated_at=now()""",
           (v["key"], v["post_title"], initials, u["username"]))
    if b.get("remember") or not u.get("initials"):
        db.run("update users set initials=%s where id=%s", (initials, u["id"]))
    _marks_changed()
    log("victim_alloc", f'{v["post_title"]} -> {initials}' + (f' (took over from {prev["initials"]})' if prev and prev["initials"] != initials else ""))
    return jsonify(ok=True, initials=initials)


@app.delete("/api/victims/alloc")
def victim_alloc_clear():
    if LEGACY:
        return jsonify(error="Database not connected."), 503
    v = _find_victim(request.args.get("title"))
    a = ALLOC.get(v["key"]) if v else None
    if not a:
        return jsonify(error="That victim is not allocated."), 404
    u = current_user()
    if u["role"] != "admin" and u["username"] != a["allocated_by"] and (u.get("initials") or "") != a["initials"]:
        return jsonify(error=f'Only {a["initials"]} ({a["allocated_by"]}) or an admin can release this.'), 403
    db.run("delete from victim_alloc where key=%s", (v["key"],))
    _marks_changed()
    log("victim_alloc_clear", f'{v["post_title"]} (was {a["initials"]})')
    return jsonify(ok=True)


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


VIEW_KEYS = ("post_title", "group", "activity", "discovered", "seen", "year", "website", "post_url", "rocu", "force", "force_name",
             "town", "region_source", "registered_name", "manual", "override_note", "description", "reposts", "overseas_weak")


def _view(v, prev):
    """The fields the browser needs for a victim (not the internal matching fields), plus 'new since your last visit'."""
    d = {k: v.get(k, "") for k in VIEW_KEYS}
    t = _ts(v.get("seen"))
    d["new"] = bool(prev and t and t > prev)
    d["why"] = _why_unplaced(v)
    d["flag"] = v.get("flag")
    d["alloc"] = _alloc_view(v)
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
        items = [v for v in items if v["key"] in ALLOC]
    elif mark == "mine":
        u = current_user()
        ini = u.get("initials") or ""
        items = [v for v in items if v["key"] in ALLOC and (ALLOC[v["key"]]["allocated_by"] == u["username"] or (ini and ALLOC[v["key"]]["initials"] == ini))]
    if request.args.get("group"):
        items = [v for v in items if v["group"] == request.args["group"]]
    if request.args.get("sector"):
        items = [v for v in items if v["activity"] == request.args["sector"]]
    q = request.args.get("q", "").lower()
    if q:
        items = [v for v in items if q in (v["post_title"] + " " + v["website"] + " " + v["group"] + " " + (v.get("town") or "")).lower()]
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
                 "first_seen_utc", "website", "false_positive_flag", "allocated_to"])
    safe = lambda s: ("'" + str(s)) if str(s)[:1] in ("=", "+", "-", "@") else str(s)      # no spreadsheet formula injection
    for v in items:
        wr.writerow([safe(v["post_title"]), safe(v.get("town") or ""), v["rocu"], v.get("force_name", ""),
                     v.get("region_source", ""), safe(v["group"]), v["activity"], v.get("seen", ""), safe(v["website"]),
                     (v["flag"]["kind"].replace("_", " ") + " by " + v["flag"]["by"]) if v.get("flag") and v["flag"]["applied"] else "",
                     (ALLOC.get(v["key"]) or {}).get("initials", "")])
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
    if q:
        items = [i for i in items if q in json.dumps(i).lower()]
    return jsonify(total=len(items), items=items[:_int("limit", 120)])


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
                      "new": _view(v, prev)["new"], "manual": bool(v.get("manual")), "flag": v.get("flag"), "alloc": _alloc_view(v)}
    n_uk, n_reg = max(1, _int("uk", 5, 1500)), max(1, _int("reg", 5, 1500))      # "View more" asks for longer lists
    reg_all = [x for x in recent if x["rocu"] == region] if region else []
    return jsonify(region=region, uk=[pick(v) for v in uk_recent[:n_uk]], uk_total=len(uk_recent),
                   regional=[pick(v) for v in reg_all[:n_reg]], regional_total=len(reg_all),
                   updated=STATE.get("last_refresh", {}).get("rl_victims_gb"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
