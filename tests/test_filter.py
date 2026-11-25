import json
import os
import tempfile
import time
import unittest

from browser_atlas import _filter, _graph, _memory


def write_cfg(home, cfg):
    p = os.path.join(home, "filters.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    st = os.stat(p)                          # a rewrite within one tick must still count as a change
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + int(time.time_ns() % 1000) + 1))


class FilterStorageTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_HOME"] = self.home
        write_cfg(self.home, {"on": ["s"], "sets": {"s": {"홍길동": "사람A"}}})
        self.con = _memory.connect(os.path.join(self.home, "m.sqlite3"))

    def test_mask_reaches_nested_strings_and_keys(self):
        got = _filter.mask({"홍길동": ["안녕 홍길동", ("홍길동",)], "n": 3})
        self.assertEqual(got, {"사람A": ["안녕 사람A", ("사람A",)], "n": 3})

    def test_page_title_is_stored_masked_and_stays_masked_after_off(self):
        _graph.page("https://ex.com/a", title="홍길동 시험 페이지", con=self.con)
        raw = self.con.execute("select title from g_pages").fetchone()["title"]
        self.assertEqual(raw, "사람A 시험 페이지")
        write_cfg(self.home, {"on": [], "sets": {"s": {"홍길동": "사람A"}}})
        self.assertEqual(_graph.host_map("ex.com", con=self.con)["top"][0]["title"], "사람A 시험 페이지")

    def test_lookup_by_listed_word_finds_the_masked_row(self):
        _graph.control("https://ex.com/a", "button", "홍길동 보기", con=self.con)
        self.assertIsNotNone(self.con.execute(
            "select 1 from g_controls where name=?", ("홍길동 보기",)).fetchone())
        self.assertEqual(self.con.execute("select name from g_controls").fetchone()["name"], "사람A 보기")

    def test_memory_goal_is_stored_masked(self):
        _memory.record("홍길동 성적 찾기", "ex", [{"url": "https://ex.com/a"}], "success", 0.9, con=self.con)
        self.assertEqual(self.con.execute("select goal from experience").fetchone()["goal"], "사람A 성적 찾기")

    def test_no_filter_stores_as_is(self):
        write_cfg(self.home, {"on": [], "sets": {"s": {"홍길동": "사람A"}}})
        _graph.page("https://ex.com/b", title="홍길동", con=self.con)
        self.assertEqual(self.con.execute("select title from g_pages").fetchone()["title"], "홍길동")
