"""go stops where a choice is needed, follows new tabs, and doubts what it arrived at."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from browser_atlas import _cdp, mcp_server

H = "https://stats.example.org"


def _plan(page, steps, **extra):
    return {"found": True, "host": "stats.example.org", "page": page, "title": "t", "target": "x",
            "steps": steps, "others": ["/other"], **extra}


class _Browser:
    """A fake _browser: one URL per tab, page texts by URL, clicks that move or open a tab."""

    def __init__(self, texts=None, clicks=None):
        self.url = {"T": H + "/"}
        self.texts = texts or {}
        self.clicks = clicks or {}
        self.later = {}                               # tab -> addresses it shows on the next waits (redirects)
        self.calls = []

    def __call__(self, name, args):
        tab = args.get("tab")
        self.calls.append((name, tab, args))
        if tab:
            self.url.setdefault(tab, H + "/")
        if name == "browser_open":
            self.url["T"] = args["url"]
            return {"tab": "T", "url": args["url"], "title": "x"}
        if name == "browser_goto":
            self.url[tab] = args["url"]
            return {"url": args["url"], "title": "x"}
        if name == "browser_click":
            hit = self.clicks.get(args.get("name"), {})
            if "new_tab" in hit:
                self.url[hit["new_tab"]["tab"]] = hit.get("lands", hit["new_tab"]["url"])
                if "then" in hit:
                    self.later[hit["new_tab"]["tab"]] = list(hit["then"])
                return {"url": self.url[tab], "new_tab": dict(hit["new_tab"])}
            self.url[tab] = hit.get("url", self.url[tab])
            return {"url": self.url[tab]}
        if name == "browser_wait":
            if self.later.get(tab):
                self.url[tab] = self.later[tab].pop(0)
            if args.get("wait_text") and args["wait_text"] not in self.texts.get(self.url[tab], ""):
                raise TimeoutError("not met in 15s")
            return {"met": True, "url": self.url[tab]}
        if name == "browser_read":
            return {"url": self.url[tab], "untrusted_page_content": [self.texts.get(self.url[tab], "")]}
        if name == "browser_close":
            return {"closed": True}
        return {"url": self.url[tab]}


class GoTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        os.environ.pop("BROWSER_ATLAS_MAP", None)
        mcp_server.REC.open.clear()
        mcp_server._FULL_URL.clear()
        mcp_server._CLICK_KNOWN.clear()
        for p in (mock.patch.object(_cdp, "tabs", return_value=[]),          # never reach a real Chrome
                  mock.patch.object(mcp_server, "_opened", return_value={"T", "N"}),
                  mock.patch("time.sleep")):
            p.start()
            self.addCleanup(p.stop)

    def _go(self, plan, steps, browser=None):
        b = browser or _Browser()
        seen = {}

        def fake_plan(start, target, host=None, con=None, picks=None):
            seen["picks"] = picks
            return json.loads(json.dumps(plan))
        with mock.patch.object(mcp_server, "_browser", side_effect=b), \
                mock.patch.object(mcp_server._graph, "plan", side_effect=fake_plan):
            mcp_server.call("browser_open", {"url": H + "/", "goal": "x"})
            r = mcp_server.call("browser_run", {"tab": "T", "steps": steps})
        return r, b, seen

    # ── choice points ──
    def test_a_thing_choice_at_zero_does_nothing_and_says_what_to_answer(self):
        ch = {"kind": "thing", "at": 0, "candidates": ["JPM", "NVDA.O"], "picked": "NVDA.O",
              "read": "which thing", "answer": 'picks={"thing": "<name>"}'}
        plan = _plan("/stock/NVDA.O/finance", [["goto", H + "/stock/NVDA.O/finance"]], choose=ch,
                     ambiguous={"candidates": ["JPM", "NVDA.O"], "picked": "NVDA.O"})
        r, b, _ = self._go(plan, [["go", "financials"], ["read", "text"]])
        self.assertEqual(len(r["steps"]), 1)
        err = r["steps"][0]["error"]
        self.assertTrue(err.startswith("choose: "))
        payload = json.loads(err[len("choose: "):])
        self.assertEqual((payload["choose"]["kind"], payload["target"]), ("thing", "x"))
        self.assertIn("ambiguous", payload)
        self.assertEqual(r["choose"], ch)
        self.assertEqual((r["completed"], r["of"]), (0, 2))
        self.assertNotIn("browser_goto", [c[0] for c in b.calls])

    def test_a_choice_further_on_runs_the_safe_steps_first(self):
        ch = {"kind": "toggle", "at": 1, "candidates": [["tab", "Quarterly"]], "picked": None}
        plan = _plan("/fin", [["goto", H + "/fin"], ["click", "tab", "Quarterly"]], choose=ch)
        r, b, _ = self._go(plan, [["go", "financials"]])
        names = [c[0] for c in b.calls]
        self.assertIn("browser_goto", names)
        self.assertNotIn("browser_click", names)
        self.assertEqual([s["step"] for s in r["steps"]], [0, "0.0", 0])
        self.assertTrue(r["steps"][-1]["error"].startswith("choose: "))
        self.assertNotIn("ambiguous", json.loads(r["steps"][-1]["error"][8:]))
        self.assertEqual((r["completed"], r["choose"]["at"]), (0, 1))

    def test_a_toggle_choice_at_the_end_arrives_then_stops(self):
        ch = {"kind": "toggle", "at": 1, "candidates": [["radio", "Annual"], ["radio", "Quarterly"]]}
        r, b, _ = self._go(_plan("/fin", [["goto", H + "/fin"]], choose=ch), [["go", "financials"]])
        self.assertEqual(b.url["T"], H + "/fin")
        self.assertTrue(r["steps"][-1]["error"].startswith("choose: "))
        self.assertEqual(r["choose"]["kind"], "toggle")

    def test_picks_reach_the_plan_in_either_slot(self):
        plan = _plan("/fin", [["goto", H + "/fin"]])
        _, _, seen = self._go(plan, [["go", "financials", {"thing": "JPM"}]])
        self.assertEqual(seen["picks"], {"thing": "JPM"})
        b = _Browser(texts={H + "/fin": "Revenue"})
        r, _, seen = self._go(plan, [["go", "financials", "Revenue", {"toggle": ""}]], b)
        self.assertEqual(seen["picks"], {"toggle": ""})
        self.assertEqual(r["steps"][0]["result"]["checked"], {"fresh": None, "wait": True})

    # ── new tabs ──
    def test_tab_step_follows_the_popup_and_go_arrives_there(self):
        b = _Browser(texts={H + "/table": "Producer price index"},
                     clicks={"PPI table": {"new_tab": {"tab": "N", "url": H + "/table"}}})
        plan = _plan("/table", [["click", "link", "PPI table"], ["tab", "new"]])
        r, b, _ = self._go(plan, [["go", "price index table"], ["read", "text"]], b)
        self.assertTrue(all(s["ok"] for s in r["steps"]), r["steps"])
        self.assertEqual(r["tab"], "N")
        tab_step = next(s for s in r["steps"] if s["op"] == "tab")
        self.assertEqual((tab_step["result"]["tab"], tab_step["result"]["from"]), ("N", "T"))
        self.assertEqual(b.calls[-1][:2], ("browser_read", "N"))       # later steps act in the new tab
        self.assertEqual(b.url["T"], H + "/")                             # the clicked tab stays

    def test_only_the_latest_click_can_be_followed_by_tab_new(self):
        # review 2: a click that opened nothing left the earlier click's popup as the "new" tab
        b = _Browser(clicks={"first": {"new_tab": {"tab": "N", "url": H + "/table"}}})
        with mock.patch.object(mcp_server, "_late_tab", return_value=None):
            r, _, _ = self._go(_plan("/", []), [["click", "link", "first"], ["click", "button", "second"],
                                                ["tab", "new"]], b)
        self.assertIn("no new tab opened", r["steps"][-1]["error"])
        self.assertEqual((r["completed"], r["tab"]), (2, "T"))

    def test_a_popup_that_ends_on_a_login_page_is_arrived_but_elsewhere(self):
        # review 3: the address a popup opened at only teaches the path; where it ended decides
        b = _Browser(clicks={"PPI table": {"new_tab": {"tab": "N", "url": H + "/table?id=1"}, "lands": H + "/login"}})
        plan = _plan("/table?id=1", [["click", "link", "PPI table"], ["tab", "new"]])
        r, _, _ = self._go(plan, [["go", "price index table"]], b)
        err = r["steps"][-1]["error"]
        self.assertTrue(err.startswith("arrived_but: "), err)
        self.assertEqual(json.loads(err[len("arrived_but: "):]),
                         {"why": "elsewhere", "url": H + "/login", "page": "/table?id=1", "others": ["/other"],
                          "next": "site_rank"})
        self.assertEqual(r["completed"], 0)

    def test_a_popup_seen_on_its_opening_address_first_is_not_arrived_yet(self):
        # live recheck: the first look caught the popup before its redirect to a login page
        b = _Browser(clicks={"PPI table": {"new_tab": {"tab": "N", "url": H + "/table?id=1"},
                                           "then": [H + "/table?id=1", H + "/login"]}})
        plan = _plan("/table?id=1", [["click", "link", "PPI table"], ["tab", "new"]])
        r, _, _ = self._go(plan, [["go", "price index table"]], b)
        self.assertIn('"why": "elsewhere"', r["steps"][-1]["error"])
        self.assertIn(H + "/login", r["steps"][-1]["error"])

    def test_a_popup_redirect_gets_time_and_a_wait_text_can_vouch_for_another_address(self):
        hop = {"new_tab": {"tab": "N", "url": H + "/table?id=1"}, "lands": H + "/sso/hop"}
        plan = _plan("/table?id=1", [["click", "link", "PPI table"], ["tab", "new"]])
        b = _Browser(clicks={"PPI table": {**hop, "then": [H + "/table?id=1"]}})        # the hop finishes
        r, _, _ = self._go(plan, [["go", "price index table"]], b)
        self.assertTrue(all(s["ok"] for s in r["steps"]), r["steps"])
        self.assertNotIn("landed", r["steps"][0]["result"])
        b = _Browser(texts={H + "/stat/view": "Producer price index"},
                     clicks={"PPI table": {**hop, "then": [H + "/stat/view"]}})         # settles elsewhere
        r, _, _ = self._go(plan, [["go", "price index table", "Producer price index"]], b)
        self.assertTrue(all(s["ok"] for s in r["steps"]), r["steps"])
        self.assertEqual(r["steps"][0]["result"]["landed"], H + "/stat/view")
        b = _Browser(clicks={"Grades": {"url": H + "/sso/hop"}})                # a plain click still has to land
        r, _, _ = self._go(_plan("/grades", [["click", "link", "Grades"]]), [["go", "grades"]], b)
        self.assertIn("landed on", r["steps"][-1]["error"])

    def test_browser_plan_passes_picks(self):
        seen = {}

        def fake_plan(start, target, host=None, con=None, picks=None):
            seen["picks"] = picks
            return {"found": False}
        with mock.patch.object(mcp_server._graph, "plan", side_effect=fake_plan):
            mcp_server.call("browser_plan", {"target": "financials", "url": H + "/", "picks": {"thing": "JPM"}})
        self.assertEqual(seen["picks"], {"thing": "JPM"})

    def test_tab_new_without_a_new_tab_fails(self):
        with mock.patch.object(mcp_server, "_late_tab", return_value=None):
            r, _, _ = self._go(_plan("/", []), [["tab", "new"], ["read", "text"]])
        self.assertIn("no new tab opened", r["steps"][0]["error"])
        self.assertEqual((r["completed"], r["tab"]), (0, "T"))

    def test_tab_by_id_only_moves_to_our_own_tabs(self):
        r, _, _ = self._go(_plan("/", []), [["tab", "Z"]])
        self.assertIn("did not open", r["steps"][0]["error"])
        r, _, _ = self._go(_plan("/", []), [["tab", "N"]])
        self.assertEqual(r["tab"], "N")

    # ── arrival checks ──
    def test_wait_text_that_never_shows_is_arrived_but_missing(self):
        b = _Browser(texts={H + "/news": "Loading"})
        r, _, _ = self._go(_plan("/news", [["goto", H + "/news"]]), [["go", "news", "Headlines"]], b)
        err = r["steps"][-1]["error"]
        self.assertTrue(err.startswith("arrived_but: "))
        self.assertEqual(json.loads(err[len("arrived_but: "):]),
                         {"why": "missing", "wait": "Headlines", "page": "/news", "others": ["/other"],
                          "next": "site_rank"})
        self.assertEqual(r["completed"], 0)

    def test_delayed_news_rows_override_the_stock_header(self):
        header = "배당 2026. 10. 31.\n2026. 10. 15. 기준\n2026.09. / 2026.10.\n미국 10. 26. 12:15 실시간\n한국 10. 27. 01:15"
        rows = ["2025. 01. 06.", "2024. 07. 10.", "2024. 04. 10."]
        items = [f"가상 소식 {i}\n{d}\n새로운 사업에 관한 예시 요약.\n예시 통신" for i, d in enumerate(rows)]
        samples = ["Loading", "Loading", header, header + "\n" + "\n".join(items)]
        reads = []
        browser = _Browser()

        def read(name, args):
            if name != "browser_read":
                return browser(name, args)
            index = min(len(reads), len(samples) - 1)
            reads.append(args)
            return {"untrusted_page_content": samples[index], "list_dates": rows if index == 3 else []}

        plan = _plan("/news", [["goto", H + "/news"]], checks={"fresh_days": 14})
        class Clock(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 10, 27)

        with mock.patch.object(mcp_server, "datetime", Clock):
            r, _, _ = self._go(plan, [["go", "news"]], read)
        payload = json.loads(r["steps"][-1]["error"][len("arrived_but: "):])
        self.assertEqual((payload["why"], payload["newest"]), ("stale", "2025-01-06"))
        self.assertEqual(r["steps"][0]["result"]["checked"],
                         {"fresh": "2025-01-06", "from": "list", "wait": None})
        self.assertEqual(len(reads), 5)

    def test_freshness_polling_is_bounded_without_dates(self):
        r, b, _ = self._go(_plan("/news", [["goto", H + "/news"]], checks={"fresh_days": 14}), [["go", "news"]])
        self.assertEqual(sum(c[0] == "browser_read" for c in b.calls), 13)

    def test_text_read_passes_list_dates_to_the_arrival_check(self):
        rows = ["2025. 01. 06.", "2024. 07. 10.", "2024. 04. 10."]
        c = mock.Mock()
        c.eval.side_effect = ["Example news list", H + "/news", rows]
        result = mcp_server._read(c, {"mode": "text", "list_dates": True})
        self.assertEqual(result["list_dates"], rows)
        self.assertEqual(c.eval.call_args, mock.call(mcp_server._LIST_DATES_JS))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the DOM fixture")
    def test_pinned_dates_are_excluded_from_regular_news(self):
        fixture = r"""
const nodes = [];
function el(text, children = [], cls = '', aria = '') {
  const node = {tagName: 'DIV', className: cls, children,
    innerText: text || children.map(c => c.innerText).join('\n'),
    getAttribute: name => name === 'aria-label' ? aria : '',
    querySelectorAll: () => children};
  for (const child of children) child.parentElement = node;
  nodes.push(node);
  return node;
}
function row(date, marker) {
  return el('', [el('Fictional company announces research results and describes its progress in detail.'),
                 el(date), el(marker)], 'row');
}
const rows = [row('2026-10-27', 'Pinned notice'), row('2025-01-01', ''), row('2025-01-02', '')];
el('', rows);
const document = {querySelectorAll: () => nodes};
"""
        result = subprocess.run([shutil.which("node"), "-e", fixture +
                                 "console.log(JSON.stringify(" + mcp_server._LIST_DATES_JS + "));"],
                                check=True, capture_output=True, encoding="utf-8")
        rows = json.loads(result.stdout)
        self.assertEqual(rows, ["2025-01-01", "2025-01-02"])
        self.assertEqual(mcp_server._newest_date("\n".join(rows), date(2026, 10, 27)), date(2025, 1, 2))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the DOM fixture")
    def test_metric_rows_above_stale_news_are_not_list_items(self):
        # A small DOM fixture runs the actual extraction script without a browser.
        fixture = r"""
const nodes = [];
function el(text, children = [], cls = '') {
  const node = {tagName: 'DIV', className: cls, children,
                innerText: text || children.map(c => c.innerText).join('\n')};
  for (const child of children) child.parentElement = node;
  nodes.push(node);
  return node;
}
function row(parts) { return el('', parts.map(t => el(t)), 'row'); }
const metrics = el('', [row(['배당일', '2026. 10. 31.']),
                        row(['배당락일', '2026. 10. 06.']),
                        row(['PER', '2026.09.', '13.90배'])]);
const news = el('', ['2025. 01. 06.', '2024. 07. 10.', '2024. 04. 10.'].map(d =>
  row(['Example company announces a new research project', d,
       'This fictional report describes the research project and its expected progress over the coming year.'])));
el('', [metrics, news]);
const document = {querySelectorAll: () => nodes};
"""
        result = subprocess.run([shutil.which("node"), "-e", fixture +
                                 "console.log(JSON.stringify(" + mcp_server._LIST_DATES_JS + "));"],
                                check=True, capture_output=True, encoding="utf-8")
        rows = json.loads(result.stdout)
        self.assertEqual(rows, ["2025. 01. 06.", "2024. 07. 10.", "2024. 04. 10."])
        class Clock(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 10, 27)

        with mock.patch.object(mcp_server, "datetime", Clock), \
                mock.patch.object(mcp_server, "_tracked", return_value={"list_dates": rows}):
            checked, failure = mcp_server._check_arrival("T", {"checks": {"fresh_days": 14}}, None, None)
        self.assertEqual(checked["fresh"], "2025-01-06")
        self.assertEqual((failure["why"], failure["newest"]), ("stale", "2025-01-06"))

    def test_quarterly_filing_passes_120_days_but_fails_14(self):
        class Clock(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 10, 27)

        for days in (120, 14):
            with self.subTest(days=days), mock.patch.object(mcp_server, "datetime", Clock), \
                    mock.patch.object(mcp_server, "_tracked", return_value={"list_dates": ["2026-08-26"]}):
                checked, failure = mcp_server._check_arrival("T", {"checks": {"fresh_days": days}}, None, None)
            self.assertEqual(checked["fresh"], "2026-08-26")
            if days == 120:
                self.assertIsNone(failure)
            else:
                self.assertEqual(failure["why"], "stale")

    def test_recent_rows_report_list_as_the_source(self):
        with mock.patch.object(mcp_server, "_tracked", return_value={
                "untrusted_page_content": "News", "list_dates": [f"{date.today():%Y-%m-%d}"]}):
            checked, failure = mcp_server._check_arrival("T", {"checks": {"fresh_days": 14}}, None, None)
        self.assertIsNone(failure)
        self.assertEqual(checked, {"fresh": date.today().isoformat(), "from": "list", "wait": None})

    def test_an_old_dated_list_is_arrived_but_stale(self):
        b = _Browser(texts={H + "/news": "Notices\n2024.01.14 Holiday hours\n2023.12.02 Moved"})
        plan = _plan("/news", [["goto", H + "/news"]], checks={"fresh_days": 14})
        r, _, _ = self._go(plan, [["go", "news"]], b)
        payload = json.loads(r["steps"][-1]["error"][len("arrived_but: "):])
        self.assertEqual((payload["why"], payload["newest"], payload["fresh_days"], payload["next"]),
                         ("stale", "2024-01-14", 14, "site_rank"))
        self.assertEqual(payload["others"], ["/other"])

    def test_no_date_on_the_page_is_fresh_unknown_not_a_failure(self):
        b = _Browser(texts={H + "/news": "Notices\nHoliday hours"})
        plan = _plan("/news", [["goto", H + "/news"]], checks={"fresh_days": 14})
        r, _, _ = self._go(plan, [["go", "news"]], b)
        self.assertTrue(all(s["ok"] for s in r["steps"]))
        self.assertEqual(r["steps"][0]["result"]["checked"], {"fresh": "unknown", "from": "text", "wait": None})

    def test_a_recent_list_passes_and_picks_fresh_zero_turns_the_check_off(self):
        today = date.today()
        b = _Browser(texts={H + "/news": f"Notices {today:%Y.%m.%d} Opening {today - timedelta(days=40):%Y.%m.%d}"})
        plan = _plan("/news", [["goto", H + "/news"]], checks={"fresh_days": 14})
        r, _, _ = self._go(plan, [["go", "news", "Notices"]], b)
        self.assertEqual(r["steps"][0]["result"]["checked"], {"fresh": today.isoformat(), "from": "text", "wait": True})
        b = _Browser(texts={H + "/news": "2001.01.01 Founded"})
        r, _, _ = self._go(plan, [["go", "news", {"fresh": 0}]], b)
        self.assertEqual(r["steps"][0]["result"]["checked"], {"fresh": None, "wait": None})


class NewestDateTest(unittest.TestCase):
    D = date(2026, 10, 27)

    def test_plain_decimal_is_not_a_date_and_ranges_are_validated(self):
        self.assertEqual(mcp_server._newest_date("Share price 10.27 USD; old news 2025-01-01", self.D),
                         date(2025, 1, 1))
        for text in ("13.27.", "10.32.", "2026-13-01", "2026-10-32", "Oct 32, 2026"):
            self.assertIsNone(mcp_server._newest_date(text, self.D), text)
        for text in ("10.27.", "10.27 09:30", "10.27 Tuesday"):
            self.assertEqual(mcp_server._newest_date(text, self.D), self.D, text)

    def test_header_clocks_consensus_and_future_dates_are_ignored(self):
        text = "2026. 10. 31. 배당\n2026. 10. 15. 기준\n미국 10. 26. 12:15 실시간\n한국 10. 27. 01:15\n2025. 01. 06. 가상 뉴스"
        self.assertEqual(mcp_server._newest_date(text, self.D), date(2025, 1, 6))

    def test_formats(self):
        for text, want in [("2026.10.21", date(2026, 10, 21)), ("2026-10-21", date(2026, 10, 21)),
                           ("2026/10/21", date(2026, 10, 21)), ("2026. 10. 21.", date(2026, 10, 21)),
                           ("2026년 10월 21일", date(2026, 10, 21)), ("공지 10.21. 휴무", date(2026, 10, 21)),
                           ("Oct 21, 2026", date(2026, 10, 21)), ("October 21 2026", date(2026, 10, 21)),
                           ("21 Oct 2026", date(2026, 10, 21)), ("3시간 전", self.D), ("30분 전", self.D),
                           ("3일 전", date(2026, 10, 24)), ("어제", date(2026, 10, 26))]:
            self.assertEqual(mcp_server._newest_date(text, self.D), want, text)

    def test_newest_wins_and_future_and_nonsense_are_dropped(self):
        f = mcp_server._newest_date
        self.assertEqual(f("2025.01.14 a\n2026.09.30 b\n2024-12-01 c", self.D), date(2026, 9, 30))
        self.assertEqual(f("마감 2026.12.31 / 접수 2026.10.01", self.D), date(2026, 10, 1))
        self.assertIsNone(f("2026.13.45, 15일 전후, 1588-1234", self.D))
        self.assertIsNone(f("no dates here", self.D))

    def test_a_full_date_is_not_read_again_as_this_years_month_day(self):
        # review 4: "2024. 10.21." came back as 2026-10-21
        f = mcp_server._newest_date
        self.assertEqual(f("게시일 2024. 10.21.", self.D), date(2024, 10, 21))
        self.assertIsNone(f("2027. 10.21.", self.D))
        self.assertEqual(f("2024. 10.21. / 10.20.", self.D), date(2026, 10, 20))

    def test_a_month_day_after_today_is_last_year(self):
        self.assertEqual(mcp_server._newest_date("공지 12.30.", date(2027, 1, 5)), date(2026, 12, 30))

    def test_hours_ago_crosses_midnight_with_a_datetime(self):
        self.assertEqual(mcp_server._newest_date("30분 전", datetime(2026, 10, 27, 0, 10)), date(2026, 10, 26))


class FakeCDP:
    """Just enough of a tab for browser_click. The click may open N (with this tab's Page.windowOpen),
    now or a few looks later, and may or may not change the DOM."""
    def __init__(self, opener="T", url=H + "/table", event=True, changes=1, late=0):
        self.target = {"id": "T"}
        self.infos = [{"targetId": "T", "type": "page", "url": H + "/search"},
                      {"targetId": "P", "type": "page", "url": H + "/elsewhere"}]
        self.new = {"targetId": "N", "type": "page", "openerId": opener, "url": url} if opener else None
        self.event, self.changes, self.late = event, changes, late
        self.window_opens = []
        self.countdown = None

    def __call__(self, port, tab):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def call(self, method, params=None, timeout=30):
        assert method == "Target.getTargets"
        if self.countdown is not None:
            self.countdown -= 1
            if self.countdown <= 0:
                self.countdown = None
                self.open()
        return {"targetInfos": list(self.infos)}

    def eval(self, js, timeout=30):
        if js == "location.href":
            return H + "/search"
        if js == "document.readyState":
            return "complete"
        if js == mcp_server._WATCH_JS:
            return True
        if js == mcp_server._QUIET_JS:
            return self.changes
        raise AssertionError(js)

    def open(self):
        if self.new:
            self.infos.append(self.new)
        if self.event:
            self.window_opens.append({"url": (self.new or {}).get("url", ""), "windowName": "_blank"})

    def click(self):
        if self.late:
            self.countdown = self.late
        else:
            self.open()


class ClickTest(unittest.TestCase):
    def setUp(self):
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        mcp_server._remember_tab("T", 9555)

    def _click(self, fake):
        class Pointer:
            def __init__(self, c, pace="human"):
                pass

            def click_control(self, role, name, index=None):
                fake.click()
                return {"role": role, "name": name, "same_name_controls": 1}
        with mock.patch.object(_cdp, "CDP", fake), mock.patch("browser_atlas._pointer.Pointer", Pointer):
            return mcp_server._browser("browser_click", {"tab": "T", "role": "link", "name": "PPI table"})

    def test_a_click_that_opens_a_tab_reports_it_and_makes_it_ours(self):
        r = self._click(FakeCDP())
        self.assertEqual(r["new_tab"], {"tab": "N", "url": H + "/table"})
        self.assertEqual(r["url"], H + "/search")                         # the clicked tab stays
        self.assertIn("N", mcp_server._opened())
        self.assertEqual(mcp_server._tab_port("N"), 9555)
        with mock.patch.object(_cdp, "tabs", return_value=[{"id": "T"}, {"id": "N"}]), \
                mock.patch.object(_cdp, "close_tab", return_value=True) as close:
            mcp_server._browser("browser_close_mine", {"port": 9555})
        self.assertIn(mock.call("N", 9555), close.call_args_list)

    def test_a_tab_someone_else_opened_is_not_this_clicks(self):
        r = self._click(FakeCDP(opener="P", event=False))
        self.assertNotIn("new_tab", r)
        self.assertNotIn("N", mcp_server._opened())

    def test_an_earlier_popup_that_moves_on_its_own_is_not_this_clicks(self):
        # review 1: a popup this tab opened before changed address during an unrelated click
        fake = FakeCDP(event=False)
        fake.infos.append({"targetId": "N", "type": "page", "openerId": "T", "url": H + "/old"})
        fake.new = None

        def click():                                  # no window asked for; N redirects by itself
            fake.infos[-1] = {**fake.infos[-1], "url": H + "/redirect"}
        fake.click = click
        r = self._click(fake)
        self.assertNotIn("new_tab", r)
        self.assertNotIn("N", mcp_server._opened())

    def test_a_late_open_from_a_click_that_changed_nothing_is_caught(self):
        # review 5: a listener opens the window 500 ms after the click, with nothing in the DOM to say so
        r = self._click(FakeCDP(changes=0, late=5))
        self.assertEqual(r.get("new_tab"), {"tab": "N", "url": H + "/table"})

    def test_a_click_that_changed_the_page_is_not_held_up(self):
        import time
        t0 = time.perf_counter()
        r = self._click(FakeCDP(opener=None, event=False, changes=3))
        self.assertNotIn("new_tab", r)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_planned_browser_click_waits_before_sending_input(self):
        events = []
        fake = FakeCDP(opener=None, event=False)
        class Pointer:
            def __init__(self, c, pace="human"):
                pass

            def wait_control(self, role, name, index=None):
                events.append(("wait", role, name, index))
                return 375

            def click_control(self, role, name, index=None):
                events.append(("click", role, name, index))
                return {"role": role}

        with mock.patch.object(_cdp, "CDP", fake), \
                mock.patch("browser_atlas._pointer.Pointer", Pointer), mock.patch("time.sleep"):
            result = mcp_server._browser("browser_click", {"tab": "T", "role": "radio", "name": "Quarter",
                                                          "index": 2, "_planned": True})
        self.assertEqual(events, [("wait", "radio", "Quarter", 2), ("click", "radio", "Quarter", 2)])
        self.assertEqual(result["control_ms"], 375)

    def test_toggle_skips_grace_and_quiet_hold_but_keeps_window_events(self):
        for changes, opener in ((0, None), (3, None), (0, "T")):
            with self.subTest(changes=changes, opener=opener):
                fake = FakeCDP(opener=opener, event=bool(opener), changes=changes)
                class Pointer:
                    def __init__(self, c, pace="human"):
                        pass

                    def click_control(self, role, name, index=None):
                        fake.click()
                        return {"role": role}

                with mock.patch.object(_cdp, "CDP", fake), \
                        mock.patch("browser_atlas._pointer.Pointer", Pointer), \
                        mock.patch("time.sleep") as sleep, \
                        mock.patch.object(_cdp, "opened_by", wraps=_cdp.opened_by) as opened:
                    result = mcp_server._browser("browser_click", {"tab": "T", "role": "radio", "name": "Quarter"})
                self.assertEqual(sleep.call_args_list, [mock.call(0)])
                self.assertEqual(opened.call_args.args[2], 0)
                self.assertEqual("new_tab" in result, bool(opener))

    def test_opened_by_needs_the_window_open_event_and_waits_for_an_address(self):
        known = {"T": H + "/search", "P": H + "/elsewhere"}
        fake = FakeCDP(url="about:blank", event=False)
        fake.open()                                   # a new page, but this tab never asked for a window
        with mock.patch("time.sleep"):
            self.assertIsNone(_cdp.opened_by(fake, known, wait=0, since=0))
        fake = FakeCDP(url="about:blank")
        fake.open()
        seq = iter([None, H + "/table"])

        def call(method, params=None, timeout=30):
            u = next(seq, H + "/table")
            if u:
                fake.new["url"] = u
            return {"targetInfos": list(fake.infos)}
        fake.call = call
        with mock.patch("time.sleep"):
            t = _cdp.opened_by(fake, known, wait=0, since=0)
        self.assertEqual((t["targetId"], t["url"]), ("N", H + "/table"))
        self.assertIsNone(_cdp.opened_by(fake, {**known, "N": H + "/table"}, wait=0, since=0))

    def test_tab_new_names_an_earlier_popup_when_nothing_new_came(self):
        fake = FakeCDP()
        fake.open()                                   # N was opened by an earlier click and is still there
        mcp_server._remember_tab("N", 9555)
        mcp_server._CLICK_KNOWN["T"] = {"T": H + "/search", "P": H + "/elsewhere", "N": H + "/table"}
        with mock.patch.object(_cdp, "CDP", fake), mock.patch.object(mcp_server, "TAB_WAIT", 0):
            with self.assertRaisesRegex(LookupError, r'no new tab opened; tab N .*\["tab", "N"\]'):
                mcp_server._late_tab("T", 9555)


class TimingTest(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.sleeps = []
        for patch in (mock.patch("time.monotonic", side_effect=lambda: self.clock),
                      mock.patch("time.sleep", side_effect=self.sleep),
                      mock.patch("random.uniform", return_value=0.25)):
            patch.start()
            self.addCleanup(patch.stop)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    def run_plan(self, plan, tracked, steps=None):
        with mock.patch.object(mcp_server, "_tracked", side_effect=tracked), \
                mock.patch.object(mcp_server, "_here", return_value=H + "/"), \
                mock.patch.object(mcp_server._graph, "plan", return_value=plan):
            return mcp_server._run({"tab": "T", "steps": steps or [["go", "x"]]})

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the DOM fixture")
    def test_short_tables_and_metric_tables(self):
        fixture = r"""
const nodes = [];
function el(tagName, text = '', children = []) {
  const node = {tagName, className: '', children,
    innerText: text || children.map(c => c.innerText).join('\n')};
  for (const child of children) child.parentElement = node;
  if (tagName === 'TR') node.cells = children;
  if (tagName === 'TABLE') node.rows = children;
  nodes.push(node);
  return node;
}
function row(parts) { return el('TR', '', parts.map(t => el('TD', t))); }
const dates = ['2025-01-01', '2025-01-02', '2025-01-03'];
const table = el('TABLE', '', dates.map(d => row(PARTS)));
const news = dates.map(d => el('DIV', '', [
  el('DIV', 'Fictional company reports research results with a detailed account of progress.'),
  el('DIV', d)]));
if (METRIC) el('DIV', '', news);
const document = {querySelectorAll: () => nodes};
"""
        for metric in (False, True):
            with self.subTest(metric=metric):
                script = fixture.replace('PARTS', "['As of', '2026-10-27']" if metric else
                                         "['10-Q', 'Quarterly report', d, '001-12345']")
                script = script.replace('METRIC', str(metric).lower())
                result = subprocess.run([shutil.which("node"), "-e", script +
                                         "console.log(JSON.stringify(" + mcp_server._LIST_DATES_JS + "));"],
                                        check=True, capture_output=True, encoding="utf-8")
                rows = json.loads(result.stdout)
                self.assertEqual(rows, ['2025-01-01', '2025-01-02', '2025-01-03'])
                self.clock = 0
                reads = []
                def tracked(name, args):
                    reads.append(self.clock)
                    return {"untrusted_page_content": "2026-10-27", "list_dates": rows}
                with mock.patch.object(mcp_server, "_tracked", side_effect=tracked):
                    checked, failure = mcp_server._check_arrival(
                        "T", {"checks": {"fresh_days": 14}}, None, None)
                self.assertEqual(reads, [0, 0.3])
                self.assertEqual(checked["from"], "list")
                self.assertEqual(failure["why"], "stale")

    def test_complete_text_fallback_waits_for_three_stable_looks(self):
        reads = []
        def tracked(name, args):
            reads.append(self.clock)
            return {"untrusted_page_content": "2025-01-01", "list_dates": [],
                    "ready_state": "complete"}
        with mock.patch.object(mcp_server, "_tracked", side_effect=tracked):
            checked, failure = mcp_server._check_arrival(
                "T", {"checks": {"fresh_days": 14}}, None, None)
        self.assertAlmostEqual(reads[-1], 1.2)
        self.assertGreaterEqual(len(reads), 3)
        self.assertEqual(checked["from"], "text")
        self.assertEqual(failure["why"], "stale")

    def test_text_fallback_restarts_when_dates_change_at_same_length(self):
        def tracked(name, args):
            return {"untrusted_page_content": "2025-01-01" if self.clock < 0.9 else "2025-01-02",
                    "list_dates": [], "ready_state": "complete"}
        with mock.patch.object(mcp_server, "_tracked", side_effect=tracked):
            checked, failure = mcp_server._check_arrival(
                "T", {"checks": {"fresh_days": 14}}, None, None)
        self.assertGreaterEqual(self.clock, 2.1)
        self.assertEqual(checked["fresh"], "2025-01-02")
        self.assertEqual(failure["why"], "stale")

    def test_freshness_read_includes_document_state(self):
        c = mock.Mock()
        c.eval.side_effect = [{"text": "2025-01-01", "ready_state": "complete"}, H, []]
        result = mcp_server._read(c, {"mode": "text", "list_dates": True})
        self.assertEqual(result["ready_state"], "complete")
        self.assertEqual(result["untrusted_page_content"], "2025-01-01")
        self.assertIn("document.readyState", c.eval.call_args_list[0].args[0])

    def test_loading_text_fallback_keeps_cap(self):
        for state, marker in [('loading', ''), ('complete', 'Loading'),
                              ('complete', '\uB85C\uB529'), ('complete', '\uBD88\uB7EC\uC624\uB294 \uC911'),
                              ('complete', 'spinner')]:
            with self.subTest(state=state, marker=marker):
                self.clock = 0
                with mock.patch.object(mcp_server, "_tracked", return_value={
                        "untrusted_page_content": "2025-01-01 " + marker,
                        "list_dates": [], "ready_state": state}):
                    mcp_server._check_arrival("T", {"checks": {"fresh_days": 14}}, None, None)
                self.assertAlmostEqual(self.clock, 6.0)

    def test_loading_header_waits_for_stable_dated_rows(self):
        reads = []
        def tracked(name, args):
            reads.append(self.clock)
            return {"untrusted_page_content": date.today().isoformat() + " Loading news...",
                    "list_dates": ["2025-01-01", "2025-01-02"] if self.clock >= 2 else []}
        with mock.patch.object(mcp_server, "_tracked", side_effect=tracked):
            checked, failure = mcp_server._check_arrival("T", {"checks": {"fresh_days": 14}}, None, None)
        self.assertGreaterEqual(reads[-1], 2.3)
        self.assertEqual(checked["from"], "list")
        self.assertEqual(failure["why"], "stale")

    def test_popup_polls_share_the_redirect_and_final_wait_budget(self):
        def tracked(name, args):
            self.clock += args["timeout"]
            return {"met": False}
        with mock.patch.object(mcp_server, "_here", return_value=H + "/wrong"), \
                mock.patch.object(mcp_server, "_tracked", side_effect=tracked):
            self.assertIsNone(mcp_server._popup_arrival("T", _plan("/wanted", []), "Missing"))
        self.assertLessEqual(self.clock, mcp_server.REDIRECT_WAIT + 5)
        self.assertAlmostEqual(self.clock, mcp_server.REDIRECT_WAIT + 5)

    def test_timing_and_only_interactions_get_one_pace(self):
        def tracked(name, args):
            self.clock += 0.05
            return {}
        plan = _plan("/", [["goto", H + "/"], ["read"], ["wait_css", "body"],
                           ["type", "textbox", "Search", "x"], ["click", "button", "Search"]])
        result = self.run_plan(plan, tracked, [["go", "x"], ["read"]])
        self.assertEqual(self.sleeps, [0.25, 0.25])
        self.assertEqual(result["steps"][0]["ms"], 750)
        self.assertEqual(result["steps"][0]["waits_ms"],
                         dict(arrive=0, popup_settle=0, fresh_poll=0, retry=0, pace=500))
        self.assertEqual([row["ms"] for row in result["steps"][1:]], [50, 50, 50, 300, 300, 50])

    def test_arrival_exits_on_first_matching_poll(self):
        addresses = iter([H + "/", H + "/", H + "/table"])
        with mock.patch.object(mcp_server, "_here", side_effect=lambda tab: next(addresses)), \
                mock.patch.object(mcp_server, "_tracked") as tracked:
            self.assertTrue(mcp_server._arrived("T", _plan("/table", [])))
        self.assertEqual(self.sleeps, [0.2, 0.2])
        self.assertEqual(tracked.call_count, 2)

    def test_popup_redirect_resets_half_second_stability(self):
        addresses = iter([H + "/start.jsp", H + "/sso", H + "/statHtml.do",
                          H + "/statHtml.do", H + "/statHtml.do", H + "/statHtml.do"])
        with mock.patch.object(mcp_server, "_here", side_effect=lambda tab: next(addresses)), \
                mock.patch.object(mcp_server, "_tracked", return_value={"met": True}):
            landed = mcp_server._popup_arrival("T", _plan("/start.jsp", []), "Table")
        self.assertEqual(landed, H + "/statHtml.do")
        self.assertEqual(self.clock, 1.0)

    def test_fresh_dates_need_only_two_looks_three_tenths_apart(self):
        reads = []
        waits = dict(arrive=0, popup_settle=0, fresh_poll=0, retry=0, pace=0)
        def tracked(name, args):
            reads.append(self.clock)
            return {"untrusted_page_content": date.today().isoformat()}
        with mock.patch.object(mcp_server, "_tracked", side_effect=tracked):
            checked, failure = mcp_server._check_arrival("T", {"checks": {"fresh_days": 14}}, None, None, waits)
        self.assertIsNone(failure)
        self.assertEqual(reads, [0, 0.3])
        self.assertEqual(waits["fresh_poll"], 300)

    def test_planned_control_appears_without_retry_or_extra_clicks(self):
        from browser_atlas._pointer import Pointer, NoControl
        polls, clicks = [], []

        class DrawingPointer:
            wait_control = Pointer.wait_control

            def _control(self, role, name, index=None):
                polls.append(index)
                if len(polls) < 4:
                    raise NoControl(role, name, [])
                return "#quarter"

            def target(self, css):
                return {"x": 10, "y": 20}

        def tracked(name, args):
            if name == "browser_click":
                self.assertTrue(args["_planned"])
                elapsed = DrawingPointer().wait_control(args["role"], args["name"], index=args.get("index"))
                clicks.append(self.clock)
                return {"control_ms": elapsed}
            return {}

        result = self.run_plan(_plan("/", [["goto", H], ["click", "radio", "Quarter", 1]]), tracked)
        self.assertTrue(all(row["ok"] for row in result["steps"]))
        self.assertEqual(polls, [1, 1, 1, 1])
        self.assertEqual(len(clicks), 1)
        self.assertEqual(result["steps"][0]["waits_ms"]["control"], 375)
        self.assertEqual(result["steps"][0]["waits_ms"]["retry"], 0)
        self.assertEqual(result["steps"][0]["waits_ms"]["pace"], 250)

    def test_control_wait_hit_tests_and_stops_at_budget(self):
        from browser_atlas._pointer import Pointer
        pointer = mock.Mock()
        pointer._control.return_value = "#quarter"
        pointer.target.side_effect = RuntimeError("covered: loader")
        with self.assertRaisesRegex(TimeoutError, "control not clickable") as error:
            Pointer.wait_control(pointer, "radio", "Quarter")
        self.assertEqual(self.clock, 2)
        self.assertEqual(error.exception.control_ms, 2000)
        self.assertEqual(pointer.target.call_count, 17)

    def test_control_wait_returns_as_soon_as_cover_clears(self):
        from browser_atlas._pointer import Pointer
        pointer = mock.Mock()
        pointer._control.return_value = "#quarter"
        pointer.target.side_effect = [RuntimeError("covered: loader"), RuntimeError("covered: loader"), {}]
        elapsed = Pointer.wait_control(pointer, "radio", "Quarter")
        self.assertEqual(elapsed, 250)
        self.assertEqual(self.sleeps, [0.125, 0.125])

    def test_exhausted_control_wait_is_accounted_without_retry(self):
        def tracked(name, args):
            if name == "browser_click":
                self.sleep(2)
                error = TimeoutError("control not clickable: no control: Quarter")
                error.control_ms = 2000
                raise error
            return {}
        result = self.run_plan(_plan("/", [["goto", H], ["click", "radio", "Quarter"]]), tracked)
        self.assertFalse(result["steps"][-1]["ok"])
        self.assertEqual(result["steps"][0]["waits_ms"]["control"], 2000)
        self.assertEqual(result["steps"][0]["waits_ms"]["retry"], 0)

    def test_control_wait_does_not_swallow_other_errors(self):
        from browser_atlas._pointer import Pointer
        pointer = mock.Mock()
        pointer._control.side_effect = RuntimeError("blocked site")
        with self.assertRaisesRegex(RuntimeError, "blocked site"):
            Pointer.wait_control(pointer, "radio", "Quarter")
        self.assertEqual(self.sleeps, [])

    def test_genuinely_covered_click_has_only_one_retry(self):
        clicks = []
        def tracked(name, args):
            if name == "browser_click":
                clicks.append(self.clock)
                raise RuntimeError("covered: overlay")
            return {}
        result = self.run_plan(_plan("/", [["goto", H], ["click", "button", "Table"]]), tracked)
        self.assertFalse(result["steps"][-1]["ok"])
        self.assertEqual(len(clicks), 2)
        self.assertEqual(result["steps"][0]["waits_ms"]["retry"], 200)

    def test_retry_polls_without_stacking_pace(self):
        clicks = []
        def tracked(name, args):
            if name == "browser_click":
                clicks.append(self.clock)
                if len(clicks) < 3:
                    raise RuntimeError("no control")
            return {}
        plan = _plan("/", [["goto", H + "/"], ["click", "button", "Table"]])
        result = self.run_plan(plan, tracked)
        self.assertTrue(all(row["ok"] for row in result["steps"]))
        self.assertEqual(clicks, [0.25, 0.45, 0.65])
        self.assertEqual(result["steps"][-1]["ms"], 650)
        self.assertEqual(result["steps"][0]["waits_ms"]["retry"], 400)
        self.assertEqual(result["steps"][0]["waits_ms"]["pace"], 250)

    def test_reads_and_tab_switches_do_not_sleep_but_later_action_does(self):
        with mock.patch.object(mcp_server, "_switch", return_value={"tab": "T"}):
            result = self.run_plan({}, lambda *a: {}, [["read"], ["tab", "T"], ["goto", H]])
        self.assertTrue(all(row["ok"] for row in result["steps"]))
        self.assertEqual(self.sleeps, [0.25])

    def test_freshness_keeps_six_second_cap_while_dated_text_changes(self):
        reads = []
        def tracked(name, args):
            reads.append(self.clock)
            return {"untrusted_page_content": date.today().isoformat(), "total_chars": len(reads)}
        with mock.patch.object(mcp_server, "_tracked", side_effect=tracked):
            mcp_server._check_arrival("T", {"checks": {"fresh_days": 14}}, None, None)
        self.assertAlmostEqual(self.clock, 6.0)
        self.assertEqual(len(reads), 21)

    def test_synthetic_wait_step_records_its_own_wall_time(self):
        def tracked(name, args):
            self.clock += 0.1 if name == "browser_wait" else 0.05
            return {"met": True}
        result = self.run_plan(_plan("/", [["goto", H]]), tracked, [["go", "x", "Table"]])
        self.assertEqual(result["steps"][0]["ms"], 150)
        self.assertEqual(result["steps"][-1]["ms"], 100)

    def test_failed_and_choice_steps_have_timing(self):
        result = self.run_plan(_plan("/", [], choose={"kind": "thing", "at": 0}), lambda *a: {})
        self.assertIn("ms", result["steps"][0])
        self.assertEqual(set(result["steps"][0]["waits_ms"]),
                         {"arrive", "popup_settle", "fresh_poll", "retry", "pace"})
        result = self.run_plan({}, lambda *a: {}, [["unknown"]])
        self.assertIn("ms", result["steps"][0])


if __name__ == "__main__":
    unittest.main()
