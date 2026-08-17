"""CalDAV client + sync engine, verified against a FAKE iCloud-shaped server.

Mock-only: no real account. The fake models the iCloud response shapes
(current-user-principal -> calendar-home-set -> Depth:1 enumerate, sync-collection
REPORT, calendar-multiget REPORT, PUT/DELETE) so the whole discover -> sync ->
cache flow is exercised end to end. Live verification against the bot account
follows once its app-specific password is available.
"""
from house_climate import caldav, db

PRINCIPAL = "/123/principal/"
HOME = "/123/calendars/"
CAL = "/123/calendars/family/"        # VEVENT category calendar (Strategy B)
REM = "/123/calendars/reminders/"     # VTODO list

_EVENT_ICS = (
    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:ev1\r\nSUMMARY:Soccer game\r\n"
    "DTSTART:20260817T230000Z\r\nDTEND:20260818T003000Z\r\nLOCATION:Field 3\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n")
_TODO_ICS = (
    "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:td1\r\nSUMMARY:Buy milk\r\n"
    "DUE:20260818T000000Z\r\nSTATUS:NEEDS-ACTION\r\nPRIORITY:1\r\n"
    "END:VTODO\r\nEND:VCALENDAR\r\n")


class _Resp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.content = text.encode("utf-8")
        self.headers = headers or {}


def _ms(inner, extra_ns=""):
    return (f'<?xml version="1.0"?><multistatus xmlns="DAV:" '
            f'xmlns:c="urn:ietf:params:xml:ns:caldav" '
            f'xmlns:cs="http://calendarserver.org/ns/" '
            f'xmlns:ical="http://apple.com/ns/ical/"{extra_ns}>{inner}</multistatus>')


class FakeCalDAV:
    """A tiny in-memory iCloud-shaped CalDAV server bound to a requests-style
    .request() interface. Holds resources per collection."""
    def __init__(self):
        self.auth = None
        self.resources = {
            CAL: {CAL + "ev1.ics": ('"e1"', _EVENT_ICS)},
            REM: {REM + "td1.ics": ('"t1"', _TODO_ICS)},
        }
        self.put_calls = []
        self.reject_writes = False   # when True, PUT returns 412 (stale If-Match)

    def request(self, method, url, data=None, headers=None, timeout=None):
        path = url.split("caldav.icloud.com", 1)[-1] if "caldav.icloud.com" in url else url
        body = (data.decode() if isinstance(data, bytes) else data) or ""
        if method == "PROPFIND":
            if "current-user-principal" in body:
                return _Resp(207, _ms(
                    f"<response><href>/</href><propstat><prop>"
                    f"<current-user-principal><href>{PRINCIPAL}</href></current-user-principal>"
                    f"</prop><status>HTTP/1.1 200 OK</status></propstat></response>"))
            if "calendar-home-set" in body:
                return _Resp(207, _ms(
                    f"<response><href>{PRINCIPAL}</href><propstat><prop>"
                    f"<c:calendar-home-set><href>{HOME}</href></c:calendar-home-set>"
                    f"</prop><status>HTTP/1.1 200 OK</status></propstat></response>"))
            if "supported-calendar-component-set" in body:   # enumerate (Depth:1 on home)
                home_resp = (f"<response><href>{HOME}</href><propstat><prop>"
                             f"<resourcetype><collection/></resourcetype></prop>"
                             f"<status>HTTP/1.1 200 OK</status></propstat></response>")
                cal_resp = self._collection_response(CAL, "Family", "VEVENT", "#FF5733FF", "ctag-c", "tok-c")
                rem_resp = self._collection_response(REM, "Reminders", "VTODO", "#33A1FFFF", "ctag-r", "tok-r")
                return _Resp(207, _ms(home_resp + cal_resp + rem_resp))
            if "getetag" in body:                            # CTag fallback listing (Depth:1)
                res = self.resources.get(path, {})
                rows = "".join(
                    f"<response><href>{h}</href><propstat><prop><getetag>{etag}</getetag></prop>"
                    f"<status>HTTP/1.1 200 OK</status></propstat></response>"
                    for h, (etag, _) in res.items())
                return _Resp(207, _ms(rows))
        if method == "REPORT":
            res = self.resources.get(path, {})
            if "sync-collection" in body:
                rows = "".join(
                    f"<response><href>{h}</href><propstat><prop><getetag>{etag}</getetag></prop>"
                    f"<status>HTTP/1.1 200 OK</status></propstat></response>"
                    for h, (etag, _) in res.items())
                return _Resp(207, _ms(rows + "<sync-token>tok-next</sync-token>"))
            if "calendar-multiget" in body:
                import re
                hrefs = re.findall(r"<d:href>([^<]+)</d:href>", body)
                rows = ""
                for h in hrefs:
                    if h in res:
                        etag, ics = res[h]
                        rows += (f"<response><href>{h}</href><propstat><prop>"
                                 f"<getetag>{etag}</getetag>"
                                 f"<c:calendar-data>{ics}</c:calendar-data></prop>"
                                 f"<status>HTTP/1.1 200 OK</status></propstat></response>")
                return _Resp(207, _ms(rows))
        if method == "PUT":
            self.put_calls.append((path, body))
            if self.reject_writes:
                return _Resp(412)          # Precondition Failed (stale If-Match)
            return _Resp(204, headers={"ETag": '"new"'})
        if method == "DELETE":
            return _Resp(204)
        return _Resp(400)

    @staticmethod
    def _collection_response(url, name, comp, color, ctag, tok):
        return (f"<response><href>{url}</href><propstat><prop>"
                f"<resourcetype><collection/><c:calendar/></resourcetype>"
                f"<displayname>{name}</displayname>"
                f'<c:supported-calendar-component-set><c:comp name="{comp}"/>'
                f"</c:supported-calendar-component-set>"
                f"<ical:calendar-color>{color}</ical:calendar-color>"
                f"<cs:getctag>{ctag}</cs:getctag><sync-token>{tok}</sync-token>"
                f"</prop><status>HTTP/1.1 200 OK</status></propstat></response>")


def _client():
    return caldav.CalDAVClient(base_url="https://caldav.icloud.com",
                               username="bot@icloud.com", password="app-pw",
                               session=FakeCalDAV())


def test_discover_returns_category_collections():
    cols = _client().discover()
    by_kind = {c.kind: c for c in cols}
    assert set(by_kind) == {"VEVENT", "VTODO"}
    assert by_kind["VEVENT"].display_name == "Family"
    assert by_kind["VEVENT"].color == "#FF5733FF"     # Strategy B category color
    assert by_kind["VTODO"].display_name == "Reminders"


def test_sync_and_multiget():
    c = _client()
    token, changed, removed = c.sync(CAL, None)
    assert changed == [CAL + "ev1.ics"] and removed == []
    got = c.multiget(CAL, changed)
    assert "BEGIN:VEVENT" in got[CAL + "ev1.ics"][1]


def test_sync_events_populates_cache(conn):
    c = _client()
    summ = caldav.sync_events(conn, c)
    assert summ["collections"] == 1 and summ["upserted"] == 1
    from datetime import datetime, timezone
    evs = db.upcoming_events(conn, datetime(2026, 8, 1, tzinfo=timezone.utc),
                             datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert len(evs) == 1
    assert evs[0]["summary"] == "Soccer game" and evs[0]["color"] == "#FF5733FF"
    assert evs[0]["location"] == "Field 3"


def test_ctag_fallback_when_sync_collection_fails():
    # A server that 500s on sync-collection but serves the PROPFIND listing.
    c = _client()
    orig = c.session.request

    def flaky(method, url, **kw):
        body = (kw.get("data") or b"")
        if method == "REPORT" and b"sync-collection" in (body if isinstance(body, bytes) else body.encode()):
            from house_climate.caldav import CalDAVError  # noqa
            return _Resp(500)
        return orig(method, url, **kw)

    c.session.request = flaky
    token, changed, removed = c.sync(CAL, None)
    assert CAL + "ev1.ics" in changed        # fell back to the CTag listing


def test_build_calendar_agenda(conn):
    from datetime import datetime, timezone
    from house_climate.config import load_config
    from house_climate.web import api
    from conftest import CFG_PATH
    cfg = load_config(CFG_PATH)
    caldav.sync_events(conn, _client())
    out = api.build_calendar(conn, cfg, now=datetime(2026, 8, 17, 12, tzinfo=timezone.utc))
    assert out["configured"] is True
    titles = [e["summary"] for d in out["days"] for e in d["events"]]
    assert "Soccer game" in titles


def test_sync_todos_and_toggle(conn):
    c = _client()
    caldav.sync_todos(conn, c)
    todos = db.open_todos(conn)
    assert len(todos) == 1 and todos[0]["summary"] == "Buy milk" and todos[0]["status"] == "NEEDS-ACTION"
    href = todos[0]["href"]
    assert caldav.toggle_todo(conn, c, href, True) is True
    assert db.open_todos(conn)[0]["status"] == "COMPLETED"
    assert any("STATUS:COMPLETED" in body for _, body in c.session.put_calls)   # wrote back


def test_filtered_todos_due_soon(conn):
    from datetime import datetime, timezone
    from house_climate.web import api
    caldav.sync_todos(conn, _client())            # Buy milk, due 2026-08-18
    api.set_todos_filter(conn, "due_soon")
    r_in = api.build_filtered_todos(conn, now=datetime(2026, 8, 16, tzinfo=timezone.utc))
    assert any(t["summary"] == "Buy milk" for t in r_in["todos"])       # within 3 days
    r_out = api.build_filtered_todos(conn, now=datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert not any(t["summary"] == "Buy milk" for t in r_out["todos"])  # >3 days out


def test_filtered_todos_high_excludes_low_priority(conn):
    # The 'high' filter must EXCLUDE low-priority items, not merely include high
    # ones — the original test only asserted inclusion, so a filter that let
    # everything through would have passed.
    from house_climate.web import api
    db.upsert_caldav_collection(conn, "r/", "VTODO", "R", "#fff", None, None)
    db.upsert_caldav_todo(conn, "r/", "r/hi.ics", '"h"', "#fff", "R",
                          {"uid": "hi", "summary": "Urgent", "due": None,
                           "status": "NEEDS-ACTION", "priority": 1})
    db.upsert_caldav_todo(conn, "r/", "r/lo.ics", '"l"', "#fff", "R",
                          {"uid": "lo", "summary": "Someday", "due": None,
                           "status": "NEEDS-ACTION", "priority": 9})
    api.set_todos_filter(conn, "high")
    summaries = {t["summary"] for t in api.build_filtered_todos(conn)["todos"]}
    assert "Urgent" in summaries and "Someday" not in summaries


def test_todo_toggle_rejected_write_keeps_cache_honest(conn):
    # The blocker: a rejected iCloud write (412 stale If-Match) must NOT leave
    # the local cache claiming the item is done — that lie never self-corrects.
    # The write is surfaced (CalDAVError) and the cached status stays put.
    import pytest
    fake = FakeCalDAV()
    fake.reject_writes = True
    c = caldav.CalDAVClient(base_url="https://caldav.icloud.com",
                            username="bot@icloud.com", password="app-pw", session=fake)
    caldav.sync_todos(conn, c)
    href = db.open_todos(conn)[0]["href"]
    with pytest.raises(caldav.CalDAVError):
        caldav.toggle_todo(conn, c, href, True)
    assert db.open_todos(conn)[0]["status"] == "NEEDS-ACTION"   # cache stayed honest


def test_all_day_event_keeps_its_local_day(conn):
    # Regression for the day-shift: an all-day event stored at midnight UTC and
    # rendered in a timezone behind UTC rolled onto the previous day. Anchored
    # at noon UTC, it stays on its own calendar day.
    from datetime import datetime, timezone
    from house_climate.config import load_config
    from house_climate.web import api
    from conftest import CFG_PATH
    cfg = load_config(CFG_PATH)          # America/Los_Angeles — behind UTC
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:allday1\r\nSUMMARY:Trash day\r\n"
           "DTSTART;VALUE=DATE:20260820\r\nDTEND;VALUE=DATE:20260821\r\n"
           "END:VEVENT\r\nEND:VCALENDAR")
    ev = caldav.parse_events(ics)[0]
    assert ev["all_day"] is True
    db.upsert_caldav_collection(conn, "c/", "VEVENT", "Cal", "#fff", None, None)
    db.upsert_caldav_event(conn, "c/", "c/allday.ics", '"e"', "#fff", ev)
    out = api.build_calendar(conn, cfg, now=datetime(2026, 8, 19, 12, tzinfo=timezone.utc))
    assert "2026-08-20" in [d["date"] for d in out["days"]]        # its own day
    ev_out = next(e for d in out["days"] if d["date"] == "2026-08-20" for e in d["events"])
    assert ev_out["all_day"] is True and ev_out["time"] is None
