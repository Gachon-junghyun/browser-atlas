import json
import os
import tempfile
import unittest

from browser_atlas import _login

SECRET = "s3cret!Pw-never-show"


class FakeCDP:
    """A login page whose fields never keep what is typed (like a keyboard-security field)."""
    target = {"id": "fake"}
    port = 9555

    def __init__(self):
        self.calls, self.dialogs, self.url = [], [], "https://login.example.com/"

    def eval(self, js, timeout=30):
        if js == "location.href":
            return self.url
        if "elementFromPoint" in js:
            return {"x": 50, "y": 20, "w": 200, "h": 30, "vw": 800, "vh": 600,
                    "name": "password", "role": "textbox", "search": False}
        if "getBoundingClientRect" in js:
            return True
        if "readyState" in js:
            return True
        return ""                                   # the field stays empty

    def until(self, js, timeout=20):
        return True

    def goto(self, url, until=None, timeout=30):
        self.url = url
        return {"url": url}

    def call(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        return {}


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["BROWSER_ATLAS_HOME"] = self.home
        os.environ["BROWSER_ATLAS_CREDS"] = tempfile.mkdtemp()
        os.environ["DEMO_PW"] = SECRET
        with open(os.path.join(os.environ["BROWSER_ATLAS_CREDS"], "sites.json"), "w", encoding="utf-8") as f:
            json.dump({"_note": "comment",
                       "demo": {"mode": "auto", "login_url": "https://login.example.com/",
                                "check_url": "https://login.example.com/me",
                                "logged_in_when": {"url_not_contains": "/login"},
                                "steps": [{"action": "type", "css": "#pw", "value": "@env:DEMO_PW"}]},
                       "bigsite": {"mode": "manual", "check_url": "https://big.example.com/me",
                                   "logged_in_when": {"css": "a.logout"}},
                       "needs_cred": {"mode": "auto", "steps": [
                           {"action": "type", "css": "#id", "value": "@cred:nobody_id"}]}}, f)

    def tearDown(self):
        for k in ("BROWSER_ATLAS_CREDS", "DEMO_PW"):
            os.environ.pop(k, None)

    def test_set_credential_rejects_failed_paste(self):
        with self.assertRaises(ValueError):
            _login.set_credential("x", "\x16")
        with self.assertRaises(ValueError):
            _login.set_credential("x", "ab")

    def test_summary_names_only(self):
        rows = {r["site"]: r for r in _login.summary()}
        self.assertEqual(rows["bigsite"]["mode"], "manual")
        self.assertEqual(rows["needs_cred"]["missing"], ["nobody_id"])
        self.assertNotIn(SECRET, json.dumps(rows))

    def test_overlay_recipe_wins(self):
        with open(os.path.join(self.home, "sites.local.json"), "w", encoding="utf-8") as f:
            json.dump({"demo": {"login": {"mode": "manual"}}}, f)
        self.assertEqual(_login.recipe("demo")["mode"], "manual")

    def test_manual_site_asks_human_without_typing(self):
        c = FakeCDP()
        r = _login.login(c, "bigsite")
        self.assertTrue(r["needs_human"])
        self.assertFalse(any(m.startswith("Input.") for m, _ in c.calls))

    def test_failed_login_never_leaks_the_secret(self):
        c = FakeCDP()
        r = _login.login(c, "demo")
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_human"])
        self.assertNotIn(SECRET, json.dumps(r))
        self.assertIn("step[0] type", r["reason"])

    def test_missing_credential_says_which(self):
        r = _login.login(FakeCDP(), "needs_cred")
        self.assertIn("nobody_id", r["reason"])


if __name__ == "__main__":
    unittest.main()
