"""Minimal CalDAV client for iCloud (F1 calendar / F2 reminders).

Read-first, local-first: discover the account's calendar (VEVENT) and reminders
(VTODO) collections, sync each incrementally (RFC 6578 sync-collection, with a
CalendarServer CTag fallback), fetch changed resources, and PUT/DELETE for the
few writes we make. iCalendar parsing uses the `icalendar` lib.

The HTTP session is INJECTABLE, so the whole flow is unit-tested against a fake
CalDAV server with no real account (mock-only for now; live iCloud verification
comes later once the bot account's app-specific password is available). See
FamView TECHNICAL_DESIGN.md for the protocol decisions this implements
(per-account partition host, sync-collection + CTag fallback, no push -> poll,
Strategy B category calendars via apple:calendar-color).
"""
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

log = logging.getLogger("house_climate.caldav")

ICLOUD_BASE = "https://caldav.icloud.com"

_NS = {
    "d": "DAV:",
    "cal": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "ical": "http://apple.com/ns/ical/",
}


class CalDAVError(Exception):
    pass


@dataclass
class Collection:
    url: str
    kind: str | None            # "VEVENT" | "VTODO" | None
    display_name: str
    color: str | None           # apple:calendar-color (#RRGGBBAA) — Strategy B category color
    ctag: str | None
    sync_token: str | None


def _q(tag: str) -> str:
    """'d:href' -> '{DAV:}href' for ElementTree with our namespace map."""
    pre, local = tag.split(":")
    return f"{{{_NS[pre]}}}{local}"


class CalDAVClient:
    def __init__(self, base_url=ICLOUD_BASE, username=None, password=None, session=None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        if username is not None:
            self.session.auth = (username, password)

    # -- transport ---------------------------------------------------------
    def _request(self, method, url, *, body=None, depth=None, headers=None):
        h = {"Content-Type": 'application/xml; charset="utf-8"'}
        if depth is not None:
            h["Depth"] = str(depth)
        if headers:
            h.update(headers)
        # Relative hrefs from the server resolve against the base host.
        if url.startswith("/"):
            url = self.base_url + url
        resp = self.session.request(method, url, data=body, headers=h, timeout=30)
        if resp.status_code >= 500 or resp.status_code in (401, 403):
            raise CalDAVError(f"{method} {url} -> HTTP {resp.status_code}")
        return resp

    def _multistatus(self, resp):
        return ET.fromstring(resp.content)

    # -- discovery ---------------------------------------------------------
    def discover(self):
        """current-user-principal -> calendar-home-set -> enumerate collections.
        Returns the VEVENT/VTODO collections in the account (Strategy B: each
        shared category calendar is its own collection with its own color)."""
        principal = self._propfind_href(
            self.base_url + "/", "d:current-user-principal", depth=0)
        home = self._propfind_href(principal, "cal:calendar-home-set", depth=0)
        return self._enumerate(home)

    def _propfind_href(self, url, prop, depth=0):
        pre = prop.split(":")[0]
        body = (
            f'<?xml version="1.0" encoding="utf-8"?>'
            f'<d:propfind xmlns:d="DAV:" xmlns:{pre}="{_NS[pre]}">'
            f'<d:prop><{prop}/></d:prop></d:propfind>')
        root = self._multistatus(self._request("PROPFIND", url, body=body, depth=depth))
        href = root.find(f".//{_q(prop)}/{_q('d:href')}")
        if href is None or not (href.text or "").strip():
            raise CalDAVError(f"{prop} not found at {url}")
        return href.text.strip()

    def _enumerate(self, home_url):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav"'
            ' xmlns:cs="http://calendarserver.org/ns/"'
            ' xmlns:ical="http://apple.com/ns/ical/"><d:prop>'
            '<d:resourcetype/><d:displayname/>'
            '<cal:supported-calendar-component-set/>'
            '<ical:calendar-color/><cs:getctag/><d:sync-token/>'
            '</d:prop></d:propfind>')
        root = self._multistatus(self._request("PROPFIND", home_url, body=body, depth=1))
        cols = []
        for resp in root.findall(_q("d:response")):
            href = resp.find(_q("d:href"))
            if href is None:
                continue
            url = (href.text or "").strip()
            rtype = resp.find(f".//{_q('d:resourcetype')}")
            is_cal = rtype is not None and rtype.find(_q("cal:calendar")) is not None
            if not is_cal:
                continue                       # skip the home collection itself, inbox, etc.
            comp = resp.find(f".//{_q('cal:supported-calendar-component-set')}"
                             f"/{_q('cal:comp')}")
            kind = comp.get("name") if comp is not None else None
            cols.append(Collection(
                url=url, kind=kind,
                display_name=self._text(resp, "d:displayname") or url.rstrip("/").rsplit("/", 1)[-1],
                color=self._text(resp, "ical:calendar-color"),
                ctag=self._text(resp, "cs:getctag"),
                sync_token=self._text(resp, "d:sync-token")))
        return cols

    @staticmethod
    def _text(resp, prop):
        el = resp.find(f".//{_q(prop)}")
        return el.text.strip() if el is not None and el.text else None

    # -- sync --------------------------------------------------------------
    def sync(self, collection_url, sync_token=None):
        """(new_sync_token, changed_hrefs, removed_hrefs). Uses RFC 6578
        sync-collection; on any failure falls back to a CTag-gated full listing
        (all current hrefs reported as changed, so the caller re-fetches)."""
        try:
            return self._sync_collection(collection_url, sync_token)
        except Exception as e:
            log.info("sync-collection unavailable (%s); CTag fallback", e)
            return self._ctag_listing(collection_url)

    def _sync_collection(self, url, sync_token):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:sync-collection xmlns:d="DAV:">'
            f'<d:sync-token>{sync_token or ""}</d:sync-token>'
            '<d:sync-level>1</d:sync-level>'
            '<d:prop><d:getetag/></d:prop></d:sync-collection>')
        root = self._multistatus(self._request("REPORT", url, body=body, depth=1))
        changed, removed = [], []
        for resp in root.findall(_q("d:response")):
            href = self._text(resp, "d:href")
            if not href:
                continue
            status = resp.find(f".//{_q('d:status')}")
            if status is not None and " 404 " in (status.text or ""):
                removed.append(href)
            else:
                changed.append(href)
        token_el = root.find(_q("d:sync-token"))
        new_token = token_el.text.strip() if token_el is not None and token_el.text else sync_token
        return new_token, changed, removed

    def _ctag_listing(self, url):
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>')
        root = self._multistatus(self._request("PROPFIND", url, body=body, depth=1))
        hrefs = []
        for resp in root.findall(_q("d:response")):
            href = self._text(resp, "d:href")
            etag = self._text(resp, "d:getetag")
            if href and etag:                  # a resource (has an ETag), not the collection
                hrefs.append(href)
        return None, hrefs, []

    def multiget(self, collection_url, hrefs):
        """{href: (etag, ics_text)} for the given resource hrefs."""
        if not hrefs:
            return {}
        href_xml = "".join(f"<d:href>{h}</d:href>" for h in hrefs)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<cal:calendar-multiget xmlns:d="DAV:"'
            ' xmlns:cal="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:getetag/><cal:calendar-data/></d:prop>'
            f'{href_xml}</cal:calendar-multiget>')
        root = self._multistatus(self._request("REPORT", collection_url, body=body, depth=1))
        out = {}
        for resp in root.findall(_q("d:response")):
            href = self._text(resp, "d:href")
            etag = self._text(resp, "d:getetag")
            data = resp.find(f".//{_q('cal:calendar-data')}")
            if href and data is not None and data.text:
                out[href] = (etag, data.text)
        return out

    # -- writes ------------------------------------------------------------
    def put(self, url, ics, *, if_match=None, if_none_match=None):
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if if_match:
            headers["If-Match"] = if_match
        if if_none_match:
            headers["If-None-Match"] = if_none_match
        resp = self._request("PUT", url, body=ics.encode("utf-8"), headers=headers)
        return resp.status_code, resp.headers.get("ETag")

    def delete(self, url, *, if_match=None):
        headers = {"If-Match": if_match} if if_match else None
        return self._request("DELETE", url, headers=headers).status_code


# -- iCalendar parsing (icalendar lib) -------------------------------------
from datetime import datetime, date, timezone as _tz   # noqa: E402
from icalendar import Calendar                          # noqa: E402


def _to_utc(v):
    """Normalize an icalendar date/datetime to (iso_string, all_day_bool)."""
    if v is None:
        return None, False
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_tz.utc)
        return v.astimezone(_tz.utc).isoformat(), False
    if isinstance(v, date):
        return v.isoformat(), True
    return str(v), False


def parse_events(ics_text):
    """Event dicts from an ICS blob (a VEVENT resource, possibly with a VTIMEZONE
    and recurrence overrides). Times normalized to UTC ISO strings; all-day
    events keep a plain date and all_day=True."""
    out = []
    for comp in Calendar.from_ical(ics_text).walk("VEVENT"):
        start = comp.get("dtstart")
        end = comp.get("dtend")
        s_iso, all_day = _to_utc(start.dt if start is not None else None)
        e_iso, _ = _to_utc(end.dt if end is not None else None)
        rid = comp.get("recurrence-id")
        out.append({
            "uid": str(comp.get("uid") or ""),
            "summary": str(comp.get("summary") or ""),
            "start": s_iso, "end": e_iso, "all_day": all_day,
            "location": (str(comp.get("location")) or None) if comp.get("location") else None,
            "rrule": comp.get("rrule").to_ical().decode() if comp.get("rrule") else None,
            "recurrence_id": _to_utc(rid.dt)[0] if rid is not None else None,
        })
    return out


def parse_todos(ics_text):
    """VTODO dicts from an ICS blob."""
    out = []
    for comp in Calendar.from_ical(ics_text).walk("VTODO"):
        due = comp.get("due")
        d_iso, _ = _to_utc(due.dt if due is not None else None)
        out.append({
            "uid": str(comp.get("uid") or ""),
            "summary": str(comp.get("summary") or ""),
            "due": d_iso,
            "status": str(comp.get("status") or "NEEDS-ACTION").upper(),
            "priority": int(comp.get("priority")) if comp.get("priority") is not None else None,
        })
    return out


def set_todo_completed(ics_text, completed: bool) -> str:
    """Return the ICS with its VTODO STATUS flipped to COMPLETED / NEEDS-ACTION
    (native iCalendar props only), for writing a completion back over CalDAV."""
    cal = Calendar.from_ical(ics_text)
    for comp in cal.walk("VTODO"):
        for k in ("STATUS", "COMPLETED", "PERCENT-COMPLETE"):
            comp.pop(k, None)
        if completed:
            comp.add("STATUS", "COMPLETED")
            comp.add("PERCENT-COMPLETE", 100)
            comp.add("COMPLETED", datetime.now(_tz.utc))
        else:
            comp.add("STATUS", "NEEDS-ACTION")
    return cal.to_ical().decode()


# -- sync engine -----------------------------------------------------------
def sync_events(conn, client):
    """Reconcile every VEVENT (calendar) collection into the local cache. iCloud
    stays the source of truth; the cache is what the dashboard renders. Returns
    a small summary. Each collection's color (Strategy B) is denormalized onto
    its events so the UI colors them per category."""
    from . import db
    summary = {"collections": 0, "upserted": 0, "removed": 0}
    stored = {c["url"]: c for c in db.caldav_collections(conn)}
    for col in client.discover():
        if col.kind != "VEVENT":
            continue
        summary["collections"] += 1
        prev = stored.get(col.url, {}).get("sync_token")
        new_token, changed, removed = client.sync(col.url, prev)
        for href, (etag, ics) in client.multiget(col.url, changed).items():
            evs = parse_events(ics)
            master = next((e for e in evs if not e.get("recurrence_id")), evs[0] if evs else None)
            if master:
                master["raw_ics"] = ics
                db.upsert_caldav_event(conn, col.url, href, etag, col.color, master)
                summary["upserted"] += 1
        for href in removed:
            db.delete_caldav_event(conn, href)
            summary["removed"] += 1
        db.upsert_caldav_collection(conn, col.url, col.kind, col.display_name,
                                    col.color, col.ctag, new_token)
    return summary


def sync_todos(conn, client):
    """Reconcile every VTODO (reminders) collection into the local cache (F2)."""
    from . import db
    summary = {"collections": 0, "upserted": 0, "removed": 0}
    stored = {c["url"]: c for c in db.caldav_collections(conn)}
    for col in client.discover():
        if col.kind != "VTODO":
            continue
        summary["collections"] += 1
        prev = stored.get(col.url, {}).get("sync_token")
        new_token, changed, removed = client.sync(col.url, prev)
        for href, (etag, ics) in client.multiget(col.url, changed).items():
            for td in parse_todos(ics):
                td["raw_ics"] = ics
                db.upsert_caldav_todo(conn, col.url, href, etag, col.color, col.display_name, td)
                summary["upserted"] += 1
        for href in removed:
            db.delete_caldav_todo(conn, href)
            summary["removed"] += 1
        db.upsert_caldav_collection(conn, col.url, col.kind, col.display_name,
                                    col.color, col.ctag, new_token)
    return summary


def client_from_env(env):
    """Build a client from ICLOUD_EMAIL + ICLOUD_APP_PASSWORD (+ optional
    ICLOUD_CALDAV_BASE), or None if the bot account isn't configured — so the
    calendar/reminders features degrade to 'not configured' instead of erroring
    (mock-only until the app-specific password is supplied)."""
    email = env.get("ICLOUD_EMAIL")
    pw = env.get("ICLOUD_APP_PASSWORD")
    if not email or not pw:
        return None
    return CalDAVClient(base_url=env.get("ICLOUD_CALDAV_BASE", ICLOUD_BASE),
                        username=email, password=pw)
