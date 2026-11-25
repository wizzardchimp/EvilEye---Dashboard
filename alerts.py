"""Email alerts: which breaches a group should be told about, the message, and the hand-over to the mail service.

No Flask and no database in here, so it can be tested on its own. The mail service is any provider with a Resend-style HTTPS API
(POST JSON with a bearer key). Railway blocks the usual mail ports on its smaller plans, so an HTTPS API is the way that always works.

Settings, all from environment variables (the key is a secret: it is only ever read here, never shown, logged or stored):
  EMAIL_API_KEY       the provider's API key
  EMAIL_FROM          the sender, e.g. "EvilEye <alerts@evileye.work>" (the domain must be verified with the provider)
  PUBLIC_URL          where the dashboard is, for the links in the email (default https://www.evileye.work)
  EMAIL_API_URL       the provider's endpoint (default Resend's); changed only for testing
  ALERT_RECENT_DAYS   only breaches first seen within this many days are ever emailed (default 7)
  EMAIL_DAILY_CAP     at most this many alert emails in 24 hours (default 200; the provider's own free plan is 100 a day)
  ALERT_RETRY_SECONDS after a run in which nothing could be sent, wait this long before trying again (default 600)
  EMAIL_SEND_GAP_MS   pause between one message and the next in a run (default 600: Resend allows 2 requests a second)
"""
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

API_URL = os.environ.get("EMAIL_API_URL", "https://api.resend.com/emails").strip()
API_KEY = os.environ.get("EMAIL_API_KEY", "").strip()
SENDER = os.environ.get("EMAIL_FROM", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://www.evileye.work").strip().rstrip("/")


def _int(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


RECENT_DAYS = _int("ALERT_RECENT_DAYS", 7, 1, 60)
DAILY_CAP = _int("EMAIL_DAILY_CAP", 200, 1, 5000)
RETRY_PAUSE = _int("ALERT_RETRY_SECONDS", 600, 0, 3600)    # after a run in which nothing could be sent, wait this long before trying again
SEND_GAP = _int("EMAIL_SEND_GAP_MS", 600, 0, 5000) / 1000
MAX_LISTED = 20                      # breaches listed in one email; the rest are counted ("and 3 more")

_LOCAL = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
EMAIL_RE = re.compile(rf"^{_LOCAL}@{_LABEL}(?:\.{_LABEL})+$")
SENDER_RE = re.compile(rf"^(?:[^<>\r\n\"]{{1,60}} <{_LOCAL}@{_LABEL}(?:\.{_LABEL})+>|{_LOCAL}@{_LABEL}(?:\.{_LABEL})+)$")
CONTROL = re.compile(r"[\x00-\x1f\x7f  ​-‏‪-‮⁦-⁩﻿]")


def clean_email(raw):
    """A tidy lower-case address, or None if it is not a plausible one (no spaces, one @, a dot in the domain, 254 characters at most)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    return s if 3 <= len(s) <= 254 and EMAIL_RE.match(s) and ".." not in s else None


def status():
    """Whether sending is set up, and if not why, without ever exposing the key."""
    if not API_KEY:
        return {"configured": False, "reason": "EMAIL_API_KEY is not set in Railway.", "sender": ""}
    if not SENDER:
        return {"configured": False, "reason": "EMAIL_FROM is not set in Railway.", "sender": ""}
    if not SENDER_RE.match(SENDER):
        return {"configured": False, "reason": "EMAIL_FROM is not a valid sender (use: Name <address@your-domain>).", "sender": ""}
    if not re.match(r"^https?://", API_URL):
        return {"configured": False, "reason": "EMAIL_API_URL is not a web address.", "sender": ""}
    return {"configured": True, "reason": "", "sender": SENDER}


def configured():
    return status()["configured"]


def send(to, subject, text, body_html):
    """One message to one person. Returns (ok, short reason). Never raises, and never puts the key or the address in the reason."""
    if not configured():
        return False, status()["reason"]
    for attempt in (1, 2):
        try:
            r = requests.post(API_URL, timeout=15, headers={"Authorization": "Bearer " + API_KEY, "User-Agent": "EvilEye-Dashboard/2.0"},
                              json={"from": SENDER, "to": [to], "subject": subject, "text": text, "html": body_html})
        except requests.RequestException as e:
            return False, f"could not reach the mail service ({type(e).__name__})"
        if r.status_code == 429 and attempt == 1:            # told to slow down: wait as long as it asks (up to 5 seconds), then once more
            try:
                wait = float(r.headers.get("Retry-After", "1"))
            except ValueError:
                wait = 1.0
            time.sleep(max(0.2, min(wait, 5.0)))
            continue
        break
    if 200 <= r.status_code < 300:
        return True, ""
    why = ""
    try:
        j = r.json()
        why = str(j.get("message") or j.get("error") or j.get("name") or "")
    except ValueError:
        pass
    why = CONTROL.sub(" ", why).replace(to, "the address")[:140]
    return False, f"the mail service said {r.status_code}" + (f": {why}" if why else "")


def ts(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def matching(victims, regions, include_unplaced, now, days=None):
    """The breaches a group watching these regions should hear about: first seen recently, placed in one of its regions (or, if it
    asked for them, not placed yet). Overseas ones and ones flagged as false positives are never included."""
    cutoff = now - timedelta(days=days or RECENT_DAYS)
    want = set(regions)
    out = []
    for v in victims:
        if v.get("flag_applied"):
            continue
        r = v.get("rocu")
        if r not in want and not (include_unplaced and r == "Unplaced"):
            continue
        t = ts(v.get("seen"))
        if t and t >= cutoff:
            out.append(v)
    return out


def one_line(s, n=120):
    """Plain text for a subject or a name: no control characters, no runs of spaces."""
    return re.sub(r"\s+", " ", CONTROL.sub(" ", str(s or ""))).strip()[:n]


def link(title):
    return f"{PUBLIC_URL}/?open={quote(str(title), safe='')}"


def _when(t):
    return t.strftime("%d %b %Y %H:%M UTC") if t else "date unknown"


def _place(v):
    r = v.get("rocu") or ""
    r = "region not known yet" if r == "Unplaced" else r
    return f"{r} · {one_line(v['town'], 60)}" if v.get("town") else r


def compose(victims, group_names, now=None):
    """(subject, text, html) for the breaches a person has not been told about yet. No leak-site address, no company website and no
    free text from the feeds goes in: just the name, gang, sector, place, when it was first seen, and a link into the dashboard."""
    now = now or datetime.now(timezone.utc)
    vs = sorted(victims, key=lambda v: str(v.get("seen") or ""), reverse=True)
    n, shown = len(vs), vs[:MAX_LISTED]
    regions = sorted({("region not known yet" if v.get("rocu") == "Unplaced" else v.get("rocu") or "") for v in vs})
    where = regions[0] if len(regions) == 1 else f"{len(regions)} regions" if len(regions) > 3 else ", ".join(regions[:-1]) + " and " + regions[-1]
    plural = "breach" if n == 1 else "breaches"
    subject = one_line(f"EvilEye: {n} new UK {plural} in {where}", 150)
    groups = ", ".join(one_line(g, 40) for g in sorted(group_names, key=str.lower))
    more = f"and {n - len(shown)} more: open the dashboard to see them all." if n > len(shown) else ""
    foot = (f"You are getting this because you are in the group{'s' if len(group_names) > 1 else ''}: {groups}. "
            "To stop these emails, untick \"Email me alerts\" in your account on the dashboard (the person icon), or ask an admin.")
    lines = [f"{n} new UK {plural} in {where}.", ""]
    rows = []
    for i, v in enumerate(shown, 1):
        name, gang, sector = one_line(v["post_title"], 120), one_line(v.get("group"), 60), one_line(v.get("activity"), 60)
        meta = " · ".join(x for x in (gang, sector) if x)
        seen = _when(ts(v.get("seen")))
        url = link(v["post_title"])
        lines += [f"{i}. {name}", f"   {meta}" if meta else "", f"   {_place(v)}", f"   First seen {seen}", f"   {url}", ""]
        rows.append(f'<tr><td style="padding:12px 0;border-top:1px solid #e3e6eb"><div style="font-size:16px;font-weight:600;color:#161b22">'
                    f'<a href="{html.escape(url, quote=True)}" style="color:#161b22;text-decoration:none">{html.escape(name)}</a></div>'
                    f'<div style="font-size:14px;color:#4a5563;margin-top:2px">{html.escape(meta)}</div>'
                    f'<div style="font-size:14px;color:#4a5563">{html.escape(_place(v))} · first seen {html.escape(seen)}</div>'
                    f'<div style="margin-top:6px"><a href="{html.escape(url, quote=True)}" style="font-size:14px;color:#c2183f">Open in EvilEye</a></div></td></tr>')
    if more:
        lines += [more, ""]
        rows.append(f'<tr><td style="padding:12px 0;border-top:1px solid #e3e6eb;font-size:14px;color:#4a5563">{html.escape(more)}</td></tr>')
    lines += [foot]
    body = (f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:16px;color:#161b22">'
            f'<h2 style="margin:0 0 4px;font-size:20px">Evil<span style="color:#c2183f">Eye</span></h2>'
            f'<p style="margin:0 0 12px;font-size:15px">{n} new UK {plural} in {html.escape(where)}.</p>'
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(rows)}</table>'
            f'<p style="margin:16px 0 0;font-size:12px;color:#6b7686;line-height:1.5">{html.escape(foot)}</p></div>')
    return subject, "\n".join(x for i, x in enumerate(lines) if x != "" or (i and lines[i - 1] != "")), body


def compose_test(sender_name=""):
    return ("EvilEye: test email",
            "This is a test email from the EvilEye dashboard. If you can read it, alert emails can reach you.",
            '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:16px"><h2 style="margin:0 0 8px">Evil<span style="color:#c2183f">Eye</span></h2>'
            '<p style="margin:0">This is a test email from the EvilEye dashboard. If you can read it, alert emails can reach you.</p></div>')
