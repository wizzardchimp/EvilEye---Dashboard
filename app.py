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
import io
import json
import os
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import db
import geo

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT = DATA_DIR / "snapshot.json"
SCHEMA_VERSION = 3

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
STATE = {"victims": [], "posts": [], "gangs": [], "intel": [], "last_refresh": {}, "errors": {}}
_refreshing = False
_geo_running = False
GEO = geo.load_geo(DATA_DIR)


def _sector(a):
    """ransomware.live uses 'Not Found' (or nothing) when it has not classified a victim."""
    a = (a or "").strip()
    return "Unclassified" if a in ("", "Not Found", "Unknown") else a


def _load_snapshot():
    try:
        snap = json.loads(SNAPSHOT.read_text())
        if snap.get("schema") == SCHEMA_VERSION:
            STATE.update({k: snap[k] for k in ("victims", "posts", "gangs", "intel", "last_refresh", "errors")})
    except Exception:
        pass


_load_snapshot()


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


def _apply_geo(victims):
    """Region = API result from the cache if there is one, else the place-name guess, else Unplaced."""
    for v in victims:
        g = GEO.get(v["key"]) or {}
        if g.get("status") == "placed":
            v.update(rocu=g["rocu"], force=g["force"], region_source=g.get("source", "api"),
                     registered_name=g.get("company") or "")
        elif v.get("text_force"):
            v.update(force=v["text_force"], rocu=geo.rocu_of(v["text_force"]), region_source="text")
        else:
            v.update(force="", rocu="Unplaced", region_source="")
        v["force_name"] = geo.FORCE_NAMES.get(v["force"], "")


# ----------------------------------------------------------------- ingest
def _rss(url, source, kind, limit=25, skip=None):
    root = ET.fromstring(_get(url, False))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if skip and skip(title):
            continue
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).isoformat()
        except Exception:
            pub = None
        summary = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        out.append({"title": title, "source": source, "kind": kind, "url": (it.findtext("link") or "").strip(),
                    "summary": re.sub(r"\s+", " ", summary).strip()[:300], "published": pub, "extra": {}})
        if len(out) >= limit:
            break
    return out


def _ingest():
    global _refreshing
    errors, stamps = {}, {}
    new = {k: STATE[k] for k in ("victims", "posts", "gangs", "intel")}

    # ransomware.live: UK victims
    try:
        victims = []
        for v in _get("https://api.ransomware.live/v2/countryvictims/GB"):
            desc = v.get("description") or ""
            title = v.get("post_title") or ""
            hit = geo.place_text(title + ". " + desc)
            victims.append({
                "post_title": title, "key": geo.norm(title), "group": v.get("group_name") or "unknown",
                "activity": _sector(v.get("activity")), "discovered": v.get("published") or v.get("discovered"),
                "seen": v.get("discovered") or v.get("published") or "", "year": _year(v),
                "website": (v.get("website") or "").strip(), "post_url": v.get("post_url") or "",
                "text_force": hit[0] if hit else "", "text_term": hit[1] if hit else "",
                "postcode_hint": geo.postcode_in(desc), "registered_name": ""})
        _apply_geo(victims)
        new["victims"] = victims
        stamps["rl_victims_gb"] = _now_iso()
    except Exception as e:
        errors["rl_victims_gb"] = str(e)

    # aggregated attack feed: ransomware.live recent + RansomLook recent
    posts, intel = [], []
    try:
        for v in _get("https://api.ransomware.live/v2/recentvictims"):
            posts.append({"title": v.get("victim") or "", "source": "ransomware.live", "group": v.get("group") or "",
                          "country": v.get("country") or "", "detail": (v.get("description") or "")[:200],
                          "date": v.get("discovered"), "url": v.get("url") or ""})
            if v.get("press"):
                intel.append({"title": v.get("victim") or "", "source": "ransomware.live", "kind": "press",
                              "summary": (v.get("description") or "")[:260], "url": v.get("url") or "",
                              "published": v.get("discovered"), "extra": {}})
        stamps["rl_recent"] = _now_iso()
    except Exception as e:
        errors["rl_recent"] = str(e)
    rlook_groups = set()
    try:
        for v in _get("https://www.ransomlook.io/api/recent/100"):
            rlook_groups.add(v.get("group_name") or "")
            d = (v.get("discovered") or "").replace(" ", "T")
            posts.append({"title": v.get("post_title") or "", "source": "ransomlook", "group": v.get("group_name") or "",
                          "country": "", "detail": (v.get("description") or "")[:200], "date": d, "url": v.get("link") or ""})
            if len([i for i in intel if i["source"] == "ransomlook"]) < 30:
                intel.append({"title": v.get("post_title") or "", "source": "ransomlook", "kind": "leak-post",
                              "summary": (v.get("description") or "")[:260], "url": v.get("link") or "",
                              "published": d, "extra": {}})
        stamps["ransomlook"] = _now_iso()
    except Exception as e:
        errors["ransomlook"] = str(e)
    if posts:
        posts.sort(key=lambda p: p.get("date") or "", reverse=True)
        new["posts"] = posts

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

    # alerts and news
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
    feed("bleepingcomputer", lambda: _rss("https://www.bleepingcomputer.com/feed/", "bleepingcomputer", "news", 20))
    feed("krebs", lambda: _rss("https://krebsonsecurity.com/feed/", "krebs", "news", 10))
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

    with _lock:
        STATE.update(new)
        STATE["last_refresh"] = {**STATE.get("last_refresh", {}), **stamps}
        STATE["errors"] = errors
        try:
            SNAPSHOT.write_text(json.dumps({"schema": SCHEMA_VERSION,
                                            **{k: STATE[k] for k in ("victims", "posts", "gangs", "intel", "last_refresh", "errors")}}))
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
        SNAPSHOT.write_text(json.dumps({"schema": SCHEMA_VERSION,
                                        **{k: STATE[k] for k in ("victims", "posts", "gangs", "intel", "last_refresh", "errors")}}))
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
EVENTS_FROM_CLIENT = {"tab_view", "search", "filter", "region_change", "copy_leak_site", "export", "modal_open"}
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
        db.run("insert into users (username, password_hash, role, created_by) values (%s,%s,'admin','bootstrap')",
               (ADMIN_USER, generate_password_hash(PASSWORD)))


if not LEGACY:
    try:
        db.init()
        bootstrap_admin()
    except Exception as e:                      # keep serving; login reports the database error
        STATE["errors"]["db"] = str(e)


def current_user():
    if hasattr(g, "user"):
        return g.user
    g.user = None
    uid = session.get("uid")
    if LEGACY:
        if uid == "legacy" and session.get("ok"):
            g.user = {"id": 0, "username": "admin", "role": "admin", "region": session.get("region", ""), "legacy": True}
    elif isinstance(uid, int):
        try:
            row = db.one("select id, username, role, region, active, password_hash from users where id=%s", (uid,))
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


@app.post("/api/open")
def api_open():
    log("page_open")
    started = start_refresh() if is_stale() else False
    return jsonify(refreshing=_refreshing, started=started, stale=is_stale())


@app.post("/api/refresh")
def api_refresh():
    log("refresh", "manual")
    return jsonify(started=start_refresh(), refreshing=_refreshing), 202


# ---- current user
@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify(username=u["username"], role=u["role"], region=u["region"], database=not LEGACY,
                   regions=geo.REGIONS + ["Unplaced"])


@app.put("/api/me/region")
def api_me_region():
    u = current_user()
    r = (request.get_json(silent=True) or {}).get("region", "")
    if r and r not in geo.REGIONS + ["Unplaced"]:
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
    h = generate_password_hash(new)
    db.run("update users set password_hash=%s where id=%s", (h, u["id"]))
    session["pwv"] = _pwv(h)
    log("password_change", "own password")
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
    log(ev, str(b.get("detail", ""))[:200])
    return jsonify(ok=True)


# ---- admin: users and activity
def _admin_count_excluding(uid):
    return db.one("select count(*) as c from users where role='admin' and active and id<>%s", (uid,))["c"]


@app.get("/api/admin/users")
@admin_required
def admin_users():
    rows = db.many("""select u.id, u.username, u.role, u.region, u.active, u.created_at, u.created_by, u.last_login,
                      (select count(*) from activity a where a.user_id = u.id) as events
                      from users u order by lower(u.username)""")
    return jsonify(users=[{**r, "created_at": r["created_at"].isoformat(),
                           "last_login": r["last_login"].isoformat() if r["last_login"] else None} for r in rows],
                   regions=geo.REGIONS + ["Unplaced"])


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
    if region and region not in geo.REGIONS + ["Unplaced"]:
        return jsonify(error="Unknown region."), 400
    if db.one("select 1 as x from users where lower(username)=lower(%s)", (username,)):
        return jsonify(error="That username already exists."), 409
    row = db.run("insert into users (username, password_hash, role, region, created_by) values (%s,%s,%s,%s,%s) returning id",
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
        notes.append("password reset")
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
        if b["region"] and b["region"] not in geo.REGIONS + ["Unplaced"]:
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


@app.get("/api/summary")
def api_summary():
    v = _uk()
    allv = STATE["victims"]
    years = Counter(x["year"] for x in v)
    by_region = Counter(x["rocu"] for x in allv)
    src = Counter(x["region_source"] or "none" for x in allv)
    return jsonify(
        uk_total=len(v), uk_all=len(allv), region=_region(),
        attacks_total=len(STATE["posts"]) or len(allv), gangs_total=len(STATE["gangs"]),
        by_year=[{"year": y, "count": n} for y, n in sorted(years.items())][-8:],
        top_gangs=[{"group": g_, "count": n} for g_, n in _counts("group").most_common(8)],
        top_sectors=[{"sector": s, "count": n} for s, n in _counts("activity").most_common(8)],
        by_region=[{"region": r, "count": by_region.get(r, 0)} for r in geo.REGIONS + ["Unplaced"]],
        placement={"total": len(allv), "placed": len(allv) - by_region.get("Unplaced", 0),
                   "from_text": src.get("text", 0), "from_postcode": src.get("postcode", 0),
                   "from_companies_house": src.get("companies-house", 0), "unplaced": by_region.get("Unplaced", 0),
                   "looked_up": sum(1 for x in GEO.values() if x.get("status") in geo.TERMINAL),
                   "api_enabled": geo.CH_ENABLED, "running": _geo_running})


KINDS = {"year": ("year", "UK victims by year"), "gang": ("group", "UK victims by gang"),
         "sector": ("activity", "UK victims by sector")}


@app.get("/api/summary/detail")
def api_detail():
    kind = request.args.get("kind", "sector")
    key, title = KINDS.get(kind, KINDS["sector"])
    total = len(_uk())
    items = [{"label": k, "count": n, "share": _share(n, total)} for k, n in _counts(key).most_common(30)]
    return jsonify(title=title + (f" ({_region()})" if _region() else ""), items=items)


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
    return jsonify(label=label, explain=explain, count=len(rows), share=_share(len(rows), len(_uk())),
                   split_title="By sector" if other == "activity" else "By gang",
                   split=[{"label": k, "count": n} for k, n in split], victims=rows[:40])


def _int(name, default, hi=500):
    try:
        return max(0, min(int(request.args.get(name, default)), hi))
    except ValueError:
        return default


@app.get("/api/victims")
def api_victims():
    q = request.args.get("q", "").lower()
    items = _uk()
    if request.args.get("group"):
        items = [v for v in items if v["group"] == request.args["group"]]
    if request.args.get("sector"):
        items = [v for v in items if v["activity"] == request.args["sector"]]
    if q:
        items = [v for v in items if q in (v["post_title"] + " " + v["website"] + " " + v["group"]).lower()]
    items = sorted(items, key=lambda v: str(v.get("seen") or ""), reverse=True)
    off, lim = _int("offset", 0, 10**6), _int("limit", 200)
    allv = STATE["victims"]
    facets = {"groups": sorted({v["group"] for v in allv if v["group"]}, key=str.lower),
              "sectors": sorted({v["activity"] for v in allv if v["activity"]})}
    return jsonify(total=len(items), items=items[off:off + lim], facets=facets)


@app.get("/api/posts")
def api_posts():
    q = request.args.get("q", "").lower()
    vmap = {v["key"]: v["rocu"] for v in STATE["victims"]}
    items = []
    for p in STATE["posts"]:
        rocu = vmap.get(geo.norm(p["title"]), "") if p.get("country") in ("GB", "") else ""
        items.append(dict(p, rocu=rocu))
    r = _region()
    if r:
        items = [p for p in items if p["rocu"] == r]
    if q:
        items = [p for p in items if q in (p["title"] + " " + p["group"] + " " + p["detail"]).lower()]
    return jsonify(total=len(items), items=items[:_int("limit", 250)])


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


@app.get("/api/ticker")
def api_ticker():
    """Five most recent UK victims, and five most recent in the signed-in user's region. Read from the server's
    cache only: this never calls an external service."""
    u = current_user()
    region = u["region"]
    # skip anonymous "Company #123 [PENDING EXPOSURE]" placeholders: they say nothing useful in a ticker
    named = [v for v in STATE["victims"] if not re.match(r"^Company #\d+", v["post_title"])]
    recent = sorted(named, key=lambda v: str(v.get("seen") or ""), reverse=True)
    pick = lambda v: {"title": v["post_title"], "group": v["group"], "sector": v["activity"], "seen": v.get("seen"),
                      "rocu": v["rocu"], "force": v["force_name"], "website": v["website"]}
    return jsonify(region=region, uk=[pick(v) for v in recent[:5]],
                   regional=[pick(v) for v in [x for x in recent if x["rocu"] == region][:5]] if region else [],
                   updated=STATE.get("last_refresh", {}).get("rl_victims_gb"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
