import base64
import random
import unittest

from browser_atlas import _search


class SearchTest(unittest.TestCase):
    def test_unwrap_trackers(self):
        real = "https://example.com/a?b=1"
        enc = base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
        self.assertEqual(_search.unwrap(f"https://www.bing.com/ck/a?!&&p=x&u=a1{enc}&ntb=1"), real)
        self.assertEqual(_search.unwrap("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=x"),
                         real)
        self.assertEqual(_search.unwrap("https://www.google.com/url?q=https://example.com/a?b=1&sa=U"),
                         "https://example.com/a?b=1")

    def test_engine_pages_and_ads_are_not_sources(self):
        for u in ("https://www.google.com/search?q=x", "https://support.google.com/", "https://adcr.naver.com/x",
                  "https://search.naver.com/search.naver?query=y", "https://www.bing.com/images", "javascript:void(0)"):
            self.assertFalse(_search.is_source(u), u)
        for u in ("https://blog.naver.com/a/1", "https://n.news.naver.com/article/1", "https://www.reuters.com/x"):
            self.assertTrue(_search.is_source(u), u)

    def test_google_goto_and_engine_furniture(self):
        links = [["삼성 HBM4 양산 첫발 베타뉴스 https://www.betanews.net › view › beta1",
                  "https://www.google.com/goto?url=CAESabc"],
                 ["짧은 동영상", "https://www.google.com/search?udm=short_vids"],
                 ["쇼핑 새 창 열림", "https://search.shopping.naver.com/x"], ["어학사전 새창", "https://dic.daum.net/"],
                 ["네이버 블로그 글 제목입니다", "https://blog.naver.com/a/1"],
                 ["뉴시스 https://www.newsis.com › view", "https://www.newsis.com/view/1"],
                 ["HBM4 양산 일정 기사 제목", "https://www.newsis.com/view/1"]]
        r = _search.results(links, per_engine=10)
        self.assertEqual([x["domain"] for x in r], ["betanews.net", "blog.naver.com", "newsis.com"])
        self.assertTrue(r[0]["url"].startswith("https://www.google.com/goto"))     # still opens the article
        self.assertEqual(r[0]["title"], "삼성 HBM4 양산 첫발 베타뉴스")
        self.assertEqual(r[2]["title"], "HBM4 양산 일정 기사 제목")                 # heading over citation line

    def test_results_dedupe_and_limit(self):
        links = [["Reuters story one", "https://www.reuters.com/a"], ["Reuters story one", "https://www.reuters.com/a#top"],
                 ["Google help page", "https://support.google.com/x"], ["Blog post title", "https://blog.naver.com/p/1"],
                 ["Third source here", "https://example.org/3"]]
        r = _search.results(links, per_engine=2)
        self.assertEqual([x["domain"] for x in r], ["reuters.com", "blog.naver.com"])

    def test_spread_interleaves_engines_and_limits_domains(self):
        by = {"naver": [{"title": "a", "url": "https://x.com/1", "domain": "x.com"},
                        {"title": "b", "url": "https://x.com/2", "domain": "x.com"},
                        {"title": "c", "url": "https://n.com/1", "domain": "n.com"}],
              "google": [{"title": "d", "url": "https://x.com/3", "domain": "x.com"},
                         {"title": "e", "url": "https://g.com/1", "domain": "g.com"}]}
        out = _search.spread(by, per_domain=1, familiar={"g.com": 4}, rng=random.Random(0))
        domains = [o["domain"] for o in out]
        self.assertEqual(len(domains), len(set(domains)))               # one per domain
        self.assertEqual(set(domains), {"x.com", "n.com", "g.com"})
        self.assertEqual(set(out[0]["engines"]) if out[0]["domain"] == "x.com" else {"google", "naver"},
                         {"google", "naver"})                          # agreement recorded
        self.assertEqual(next(o for o in out if o["domain"] == "g.com")["familiar"], 4)
        first_two = {out[0]["from_engine"], out[1]["from_engine"]}
        self.assertEqual(first_two, {"naver", "google"})                # engines take turns


if __name__ == "__main__":
    unittest.main()
