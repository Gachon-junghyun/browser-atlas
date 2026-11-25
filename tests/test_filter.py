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


class FakeTab:
    def __init__(self, texts):
        self.texts, self.calls = texts, []

    def eval(self, js):
        self.calls.append(js)
        if js.startswith("(() =>"):
            return self.texts
        if js.startswith("((hits)"):
            return 1
        return None

    def cut(self):
        js = self.calls[-1]
        return json.loads(js[js.rindex("})(") + 3:-1])


def pset(words, mode, up=None):
    d = {"mode": mode, "words": words}
    if up is not None:
        d["up"] = up
    return d


class FilterPruneTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_HOME"] = self.home

    def test_bare_word_map_is_a_swap_set(self):
        write_cfg(self.home, {"on": ["s"], "sets": {"s": {"홍길동": "사람A"}}})
        tab = FakeTab(["홍길동"])
        self.assertEqual(_filter.prune(tab), 0)
        self.assertEqual(tab.calls, [])
        self.assertEqual(_filter.sub("담당 홍길동"), "담당 사람A")

    def test_swap_set_never_touches_the_page(self):
        write_cfg(self.home, {"on": ["s"], "sets": {"s": pset({"홍길동": "사람A"}, "swap", 3)}})
        tab = FakeTab(["홍길동"])
        self.assertEqual(_filter.prune(tab), 0)
        self.assertEqual(tab.calls, [])

    def test_prune_set_defaults_to_one_level_up(self):
        write_cfg(self.home, {"on": ["s"], "sets": {"s": pset({"홍길동": "사람A"}, "prune")}})
        tab = FakeTab(["안녕", "담당 홍길동", "끝"])
        self.assertEqual(_filter.prune(tab), 1)
        self.assertEqual(tab.cut(), [[1, 1]])
        self.assertTrue(all("홍길동" not in js for js in tab.calls))

    def test_prune_still_swaps_what_survives(self):
        write_cfg(self.home, {"on": ["s"], "sets": {"s": pset({"홍길동": "사람A"}, "prune")}})
        self.assertEqual(_filter.sub("제목 홍길동"), "제목 사람A")

    def test_each_set_keeps_its_own_character(self):
        write_cfg(self.home, {"on": ["p", "w", "q"], "sets": {
            "p": pset({"홍길동": "사람A"}, "prune", 0),
            "w": pset({"가나회사": "회사A"}, "swap"),
            "q": pset({"비밀": "■", "홍길": "X"}, "prune", 2)}})
        tab = FakeTab(["홍길동 님", "가나회사 소개", "비밀 문서", "아무것"])
        _filter.prune(tab)
        self.assertEqual(tab.cut(), [[0, 2], [2, 2]])     # 0: both p and q hit, larger up wins

    def test_old_global_prune_up_still_reads(self):
        write_cfg(self.home, {"on": ["s"], "sets": {"s": {"홍길동": "사람A"}}, "prune_up": 2})
        tab = FakeTab(["담당 홍길동"])
        _filter.prune(tab)
        self.assertEqual(tab.cut(), [[0, 2]])

    def test_no_hit_sends_no_cut(self):
        write_cfg(self.home, {"on": ["s"], "sets": {"s": pset({"홍길동": "사람A"}, "prune")}})
        tab = FakeTab(["안녕"])
        self.assertEqual(_filter.prune(tab), 0)
        self.assertFalse(any(js.startswith("((hits)") for js in tab.calls))

    def test_mode_cli_moves_old_files_to_the_new_form(self):
        write_cfg(self.home, {"on": ["a", "b"], "sets": {"a": {"홍길동": "사람A"}, "b": {"가나": "B"}},
                              "prune_up": 2})
        self.assertEqual(_filter.cli(["mode", "a", "prune", "1"]), 0)
        cfg = json.load(open(os.path.join(self.home, "filters.json"), encoding="utf-8"))
        self.assertNotIn("prune_up", cfg)
        self.assertEqual(cfg["sets"]["a"], {"mode": "prune", "up": 1, "words": {"홍길동": "사람A"}})
        self.assertEqual(cfg["sets"]["b"], {"mode": "prune", "up": 2, "words": {"가나": "B"}})
        self.assertEqual(_filter.cli(["mode", "b", "swap"]), 0)
        st = _filter.status()["sets"]
        self.assertEqual(st["b"], {"words": 1, "on": True, "mode": "swap"})
        self.assertEqual(st["a"]["up"], 1)
        self.assertEqual(_filter.cli(["mode", "a", "cut"]), 2)
