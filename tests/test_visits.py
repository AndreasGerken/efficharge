"""Anonymous visit counts: numbers per day, nothing about the visitor."""

from __future__ import annotations

from datetime import UTC, datetime

from efficharge.web.visits import DEMO_STARTS, TOTAL, VisitCounter, is_bot
from tests.test_admin import ADMIN, beta, log_in, make_admin  # noqa: F401  (a fixture)

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15"
NOON = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)


def test_a_visitor_counts_once_a_day():
    v = VisitCounter("Europe/Berlin")
    v.page_view(NOON, "/", "203.0.113.5", FIREFOX)
    v.page_view(NOON, "/", "203.0.113.5", FIREFOX)  # reload
    v.page_view(NOON, "/about", "203.0.113.5", FIREFOX)
    v.page_view(NOON, "/", "198.51.100.7", PHONE)  # someone else
    v.page_view(NOON, "/", "203.0.113.5", "Googlebot/2.1 (+http://www.google.com/bot.html)")
    v.event(NOON, DEMO_STARTS)
    day = "2026-11-21"
    assert v.views[day, TOTAL] == 4 and v.visitors[day, TOTAL] == 2
    assert v.views[day, "/"] == 3 and v.visitors[day, "/"] == 2
    assert v.visitors[day, "/about"] == 1
    assert v.views[day, DEMO_STARTS] == 1

    key = v.key  # the next day (local time: 00:30 in Berlin) starts from scratch
    v.page_view(datetime(2026, 11, 21, 23, 30, tzinfo=UTC), "/", "203.0.113.5", FIREFOX)
    assert v.visitors["2026-11-22", TOTAL] == 1 and v.key != key
    assert len(v.seen) == 2  # only today's: the total and the page


def test_bots_and_scripts_are_not_counted():
    assert is_bot("") and is_bot("curl/8.5.0") and is_bot("python-httpx/0.28")
    assert not is_bot(FIREFOX) and not is_bot(PHONE)


def test_admin_sees_the_counts_but_is_not_counted(beta):  # noqa: F811
    c, url = beta
    make_admin(c, url)  # its /login before logging in counts: 1 visitor, 1 view
    c.cookies.clear()  # now as visitors
    for ua in (FIREFOX, FIREFOX, PHONE, "Googlebot/2.1"):
        c.get("/login", headers={"user-agent": ua})
    c.get("/register", headers={"user-agent": PHONE})
    c.get("/login", headers={"user-agent": PHONE, "hx-request": "true"})  # an update, no page
    c.get("/nothing-here", headers={"user-agent": PHONE})  # 404
    assert log_in(c, ADMIN, "admin password") == 303
    for _ in range(2):  # the admin's views count for nothing, and the flushes add up
        page = c.get("/admin", headers={"user-agent": FIREFOX}).text
    # three visitors (the test client, Firefox, phone); /login: twice the test client while
    # logged out, Firefox twice, the phone once
    assert "Last 7 days: 3 visitors, 6 page views, 0 demos started." in page
    assert "<code>/login</code></td><td>5</td><td>3</td>" in page
    assert "<code>/register</code></td><td>1</td><td>1</td>" in page
    assert "<code>/admin</code>" not in page and "/nothing-here" not in page
    assert page.count("<tr><td>") == 1 + 2  # one day (counting began today), two pages
