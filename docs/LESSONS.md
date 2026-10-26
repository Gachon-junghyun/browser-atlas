# Lessons from driving real sites

Field notes from an agent driving a real Chrome over the DevTools Protocol. Everything here was
observed on live sites (mostly 2026-08 to 2026-10); nothing is a guess. Where a lesson names a
site, the site is the example, not the only place it happens.

Terms follow the README: `CDP` is the protocol client, `Pointer.click` is the safe click
(target must win `elementFromPoint`, then a real mouse event is sent), access levels are
`open` / `session` / `api` / `blocked`.

---

## The ten rules

1. When a click does nothing, measure before you rewrite the selector: page zoom, `document.visibilityState`, `elementFromPoint`, and a click counter.
2. After typing, read the value back. If it is empty, the form will submit nothing and you will misdiagnose it.
3. Check state in one run, click in the next. A check and a click in the same script is a log, not a check.
4. In any flow that can spend money, read `location.href`, not the page title, before pressing anything.
5. A captcha or a rate limit means stop for that source for the day.
6. Do not automate logins. Detect whether the session is alive and hand the login to a human.
7. Detect login by redirect first, semantic attributes second, and never by class names.
8. Open your own tab and work only there. Never navigate a tab you did not open.
9. Before acting in a cloud console, confirm which account and which project you are actually in.
10. HTTP 200 is not success. Read the text: rate-limit notices, "no results" pages and silent redirects all arrive as 200.

---

## Login: what is realistic

Of roughly 40 sites surveyed, automatic login was realistic on only 4. The rest are blocked by a
captcha, device verification, a magic link or a QR code. So the useful job of a browser agent is
**deciding whether the session is dead and calling a human**, not filling login forms:

```
a human logs in once, by hand, in the debug profile
  -> the agent only checks the session
  -> if it needs a human, the agent stops and says so
```

### How to detect a live session

| Rank | Signal | Why |
|---|---|---|
| 1 | URL redirect | If the server answers a protected page with 302/303 to a login page, this never breaks |
| 2 | Semantic attributes | `a[href^='logout']`, `body[data-signed-in]`, `data-testid` |
| 3 | Class names | Never. Meta, X, velog, Notion and Daangn all ship build-hashed class names that change every deploy |

Redirects confirmed on live sites:

| Site | Signal |
|---|---|
| YouTube | `/account` -> 303 -> `accounts.google.com` |
| Steam Community | `/my/` is rewritten to `/profiles/<steamid64>/` when logged in (detection and ID in one step) |
| Hugging Face | `/settings/tokens` -> 302 -> `/login?next=` |
| GitHub | `/settings/profile` -> 302 -> `/login?return_to=` |
| solved.ac | `/settings` -> 307 -> `/login?prev=` |
| Clien | `/service/mypage` -> 302 -> `/service/auth/login` |
| Saramin | `/zf_user/member/persons/main` -> `/zf_user/auth` |

Sites that render a login wall **on the same URL with no redirect**, so a CSS check is required:
Hacker News (inline form), DCInside, Ppomppu, JobKorea, Telegram Web, Investing.com, Google Colab, Quora.

### Where auto-login works (4 sites)

| Site | Why |
|---|---|
| Hacker News | No captcha, no 2FA, plain server-rendered HTML. Only limit is an IP rate limit (two 429s during the survey) |
| Hugging Face | `robots.txt` allows `/`, no captcha on the login form, form markup checked |
| Clien | Low bot defence, has a "stay signed in" box (stop if 2FA is on) |
| solved.ac | Only a UA gate: default UA gets 403, a Chrome UA gets 200. Real Chrome passes |

### Where auto-login is structurally impossible

| Blocker | Sites |
|---|---|
| No password (magic link or code) | Medium, Substack, velog, Notion (default), Slack, Perplexity |
| QR or app scan | Toss Invest, Telegram Web, Daangn |
| Device verification | GitHub (a new profile is a new device), Steam (Steam Guard), Discord |
| Captcha wall | LeetCode (reCAPTCHA v3 + v2 + Turnstile), Kaggle (reCAPTCHA Enterprise at the edge), X (Arkose), Twitch (Arkose), JobKorea (own image captcha) |
| Blocks automated browsers outright | All of Google: it fingerprints the remote-debugging transport ("This browser or app may not be secure") |

---

## Use an API instead of the browser

The browser is more expensive and breaks more often. These sites have an official route:

| Site | Use instead | Notes |
|---|---|---|
| DART (Korean filings) | OpenDART API | No login at all. Free key in the `crtfc_key` query parameter, 20,000 calls/day |
| Naver Finance | `api.finance.naver.com/siseJson.naver` | No login, no key. Its "close" is the last price including after-hours trading, not the regular-session close |
| Naver Map | Local search `openapi.naver.com/v1/search/local.json`, NCP Maps `maps.apigw.ntruss.com` | Viewing needs no login. **Collecting lists must go through the API**; scraping search results breaks the terms |
| GitHub | Public REST API, `raw.githubusercontent.com` | Unauthenticated: search 10/min, core 60/hour. `raw` is a CDN outside the rate limit, so fetch README bodies there |
| Stack Overflow | Stack Exchange API, SEDE | 300/day anonymous, 10,000/day with a key |
| Hugging Face | `huggingface_hub` with a token | The browser eats the page bucket (100 per 5 min), five times tighter than the API bucket (500) |
| Kaggle | `kaggle` CLI | Effectively required; the edge captcha stops the browser |
| Notion, Gmail, Google Drive / Colab | An existing connector or official API | A Colab notebook is just an `.ipynb` file in Drive, cell outputs included |
| YouTube | Data API v3 | 10,000 units/day free (one search costs 100) |
| Hacker News | Firebase API | Free, no key |
| Twitch, Steam, Telegram | Helix, Web API, MTProto | All free and official |
| solved.ac | `solved.ac/api/v3` | Mostly no auth |

Small API facts that cost time:
- Naver local search allows `display` up to **5** (other Naver search APIs allow 100). `mapx`/`mapy` are WGS84 degrees times 10^7; divide by `10000000`.
- NCP Maps has two hosts. Per a 2025-03-24 notice, the old `naveropenapi.apigw.ntruss.com` stopped taking new sign-ups and free quota; use `maps.apigw.ntruss.com/map-geocode/v2/geocode`. Headers on both: `x-ncp-apigw-api-key-id`, `x-ncp-apigw-api-key`. The free quota attaches only to the primary account; otherwise it bills.
- Calling the PageSpeed API (`pagespeedonline/v5`) without a key shares a public quota and fails with "Queries per day". Measure in a tab instead: `Emulation.setDeviceMetricsOverride` (390x844, mobile), `Network.setCacheDisabled`, `Network.emulateNetworkConditions` (150 ms, 1.6 Mbps). These overrides stick to that tab, so close it when done.

---

## Sites kept `blocked`

| Site | Why |
|---|---|
| claude.ai | Consumer terms forbid automated access, and this was enforced in Feb to Apr 2026 (account suspensions for third-party harnesses driving a subscription). Use the official API or CLI |
| Perplexity | Same: consumer UI automation breaks the terms, the official API is the answer. Plain GETs return 403 |
| TradingView | The strictest terms in the survey: it forbids not only scraping but "making or using tools that enable such use" |
| Toss Invest | QR-only login, and the account can send real orders |
| Blind (teamblind) | Accounts are tied to a work email identity, so a lockout is the most expensive to recover. Login paths all 404 (JS modal), so there was not even a detection signal |
| Baemin | No consumer web login; app only |
| KakaoTalk web | No official web client for ordinary users |
| instiz | Even the root returns 403. Paid membership, so money is on the account |
| Baekjoon (acmicpc.net) | Service ended 2026-04-28; all paths showed a static "grading service in preparation" page (observed 2026-08). Problem viewing came back 05-29, a takeover was announced 06-11, grading still offline. solved.ac survived but its data is frozen |

Terms risk, sharpest first:
1. **Discord**: automating a user account is an explicit violation ("Automated User Accounts"), including driving a logged-in session in a browser. Use the bot API.
2. **claude.ai**: see above; enforced.
3. **Slack**: bulk export, persistent copies and bypassing access control are explicitly forbidden.
4. **Programmers**: the terms list "crawling, scraping" by name.
5. **Saramin / JobKorea**: `robots.txt` disallows every path worth automating. There is a court ruling between the two over scraping with KRW 450 million in damages.
6. **All of Google**: automated sign-in is explicitly forbidden and technically enforced.

Telegram is the exception: it officially allows user-account automation (MTProto, TDLib, Telethon), the opposite of Discord. There is still no reason to drive it in a browser.

---

## Clicks that do nothing

Measure in this order before suspecting the selector.

**Page zoom is not 100%**
- Symptom: element found, click and typing raise no error, nothing happens.
- Cause: the debug profile had a saved zoom (`innerWidth` 3072 vs `outerWidth` 1879). Clicks and keys land at wrong coordinates. Zoom is stored in the profile and follows every session.
- Check: compare `innerWidth` and `outerWidth`.
- Fix: reset zoom (Ctrl+0, or reset it right after attaching).

**The window is occluded, so the app never mounts its popups**
- Symptom: zero errors, zero network requests, the button wins `elementFromPoint`, real CDP mouse events arrive, and dropdowns or modals still never open (React/antd apps; seen on Naver Ads).
- Cause: Chrome on Windows marks a fully covered window as occluded and sets `document.visibilityState` to `hidden`. antd does not mount portals (dropdowns, modals under `body`) while hidden. `document.hasFocus()` still says `true`, so do not trust it.
- Check: `document.visibilityState`.
- Fix: call CDP `Page.bringToFront` at the start of every task (it re-runs the occlusion check and activates the tab; `SetForegroundWindow` did not help). Or launch with `--disable-features=CalculateNativeWinOcclusion --disable-backgrounding-occluded-windows` (needs a fresh Chrome). From outside, `GET http://127.0.0.1:9222/json/activate/<targetId>` brings only your tab forward. This breaks more often when a human is at the PC looking at another window.

**A background tab never renders**
- Symptom: spinner forever, the wait condition times out (Google Ads).
- Cause: same `visibilityState === 'hidden'`.
- Fix: activate your own work tab (`/json/activate/<targetId>`), never someone else's.

**A modal or overlay intercepts the click**
- Symptom: `ElementClickIntercepted`, or no error and no effect. A "Start" button that actually opened a settings modal swallowed every later click for three minutes; text polling could not tell it from "nothing happened".
- Check: `elementFromPoint` at the target's centre. One run printed `topmost=false hit=DIV.modal fade in`.
- Fix: close the modal and **confirm it is gone** (test for its text in `body.innerText`) before continuing. Radios inside such a modal may already be in the DOM before it opens and can be preset.

**A hidden tab owns a button with the same name**
- Symptom: same as "click does nothing". A tabbed admin SPA had a `Search` button at the same spot in every tab, and hidden tabs also reported `is_displayed() == true`.
- Fix: pick only the element that is on top:
  ```js
  var h = document.elementFromPoint(cx, cy);
  if (e === h || e.contains(h) || h.contains(e)) { /* use e */ }
  ```
  Menu-tree folders are toggles: click only when `aria-expanded` is `false`.

**The covering element is a child or a ripple**
- Symptom: tab buttons ("Keywords / Negative keywords / Ads / Extensions" on Naver Ads) ignore `button.click()` and synthetic pointer/mouse events without error; the safe click refuses because a child `SPAN` covers it. The default tab's table looks identical, so you read the wrong table.
- Fix: take whatever `elementFromPoint` returns at the button centre, tag it, and click that:
  ```js
  const b = [...document.querySelectorAll('button')].find(e => e.textContent.trim() === 'Keywords');
  b.scrollIntoView({block: 'center'});
  const r = b.getBoundingClientRect();
  document.elementFromPoint(r.left + r.width/2, r.top + r.height/2).setAttribute('data-x', 'kw');
  // then: Pointer.click('[data-x=kw]')
  ```
  The same works for Material checkboxes (tag the `MATERIAL-RIPPLE`) and for date presets that are bare `<span>`s with no class.

**Custom select (ul/li) ignores JS `element.click()`**
- Symptom: the `li` is visible, the value does not change.
- Fix: a real mouse event (CDP `Input.dispatchMouseEvent` after an `elementFromPoint` check). The opener is a toggle: clicking it while open closes it and leaves zero options. One unit = open, wait, real click, confirm in the result area.

**React forms and several clicks in one script**
- Symptom: two weekday checkboxes clicked in one script; the DOM said one thing, the preview panel another.
- Cause: the second click hit a stale node during React's batched render.
- Fix: one click, wait 2 to 3 s, next click. Judge by the result area (preview, summary), not the input.

**Did the click arrive at all?**
- Check: `el.addEventListener('click', () => window.__c++, true)`, click, read `__c`.
- If `__c` is 1 and the screen did not change, the app rejected it (a precondition is unmet). No selector change will ever fix that.

**Script plumbing that looks like a missing element**
- Inside a callback, `arguments[0]` is the callback's argument, not the script's. Copy it to a variable first: `var L = arguments[0];`. A "no listbox found" diagnosis came from this.
- `execute_script` without `return` gives `None`: use `return document.body.innerHTML`.
- An id with dots (`#info.search.place.list` on Kakao Map) breaks unless escaped; it silently matched zero. Prefer a class selector.
- A link whose `innerText` spans several lines does not match a substring search; scrape `[innerText, href]` from `querySelectorAll('a')` directly.

---

## Input that does not stick

**`send_keys` leaves the field empty**
- Symptom: a visible, enabled login field had `value.length === 0` after `clear(); send_keys(...)`. No keyboard-security module was present.
- Cause: unknown; a focused click followed by typing worked, and a JS value set with `input`/`change` events worked.
- Fix: focus, type, **read the value back**, fall back to injection if empty. An auto-login that does not verify the value reports "wrong password" when it actually submitted an empty form (the server never saw it, so no lockout counter moved either).

**React controlled inputs**
- Symptom: `element not interactable` from Selenium on a visible, enabled input (Cloudflare DNS form, Resend domain form). `el.value = v` "works" but submits empty.
- Fix: the native setter, then bubbling events:
  ```js
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  set.call(el, v);
  el.dispatchEvent(new Event('input',  {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  ```
  Use `HTMLTextAreaElement.prototype` for textareas. Custom dropdowns (base-ui): click the button, then the `[role=option]`.

**Autocomplete selects ignore the native setter**
- Symptom: the value shows in a `title` attribute but the search does not run (Naver Ads keyword box).
- Fix: type with CDP `Input.insertText`, then click the suggested option by coordinates (options span several lines, so exact text match fails).

**Inline autocomplete keeps its own copy of what you typed** (GitHub repository topics, observed 2026-10)
- Symptom: the box reads `claude-code`, the value check passes, and the comma that turns it into a topic saves `claude-cod`. Saved topics came out as `browser-automatio`, `chrome-devtools-protoco`, `web-crawle`. Not every word: exact, rare words survived.
- Cause: the field has `aria-autocomplete="both"`. It fills in the rest of a suggestion and selects it, and its internal copy of "what was typed" ended one letter short of the box when the word was typed straight through. The next click into the field or the comma put that copy back. Waiting six seconds did not help; pressing End did not help.
- Fix (now in `Pointer.type`): on inline-autocomplete fields, type all but the last letter, let the suggestion land, click back into the field, type the last letter, then drop any selected suggestion tail with Delete and retype whatever of yours it covered. Six new topics in a row then came out exact. Choosing the suggestion from the dropdown by click did not select it.

**Clearing a token input pulls the last token back**
- Symptom: `clear` (Ctrl+A, Backspace) on an empty topics box produced `devtoolasjson-schem`.
- Cause: Backspace in an empty token input turns the last token back into editable text; Ctrl+A had nothing to select.
- Fix: do not `clear` a token input to start a new token; type into it as is. To remove a token, use its own remove control.

**Typing appends**
- A field that already holds text keeps it: GitHub's description became `Browser atlas public repositoryA map of the web...`. Pass `clear` when replacing a value (not in token inputs, see above).

**Virtual lists with no search box**
- Fix: scroll with `Input.dispatchMouseEvent` type `mouseWheel` over the list. For cascading region pickers, pick every level down to the last; stopping halfway silently keeps the previous results.

**Rejected input with no visible error**
- Symptom: "Next" silently does nothing (Naver Booking promo text).
- Cause: a disallowed character (here a middle dot). The error lived only in `.help-block.error`.
- Check first: `document.querySelectorAll('.error,.help-block')` before hunting for missing required fields.

**The value on screen is truncated**
- Symptom: a DNS record copied from the table never verifies (Resend shows `p=MIGfMA[...]0GCSqGSIb3DQ...`, with hidden spans in the gap).
- Fix: read the original from the copy button: `td.querySelector('[aria-label^="Copy "]').getAttribute('aria-label').slice(5)`. For long tokens and keys, search `aria-label`, `title` and `data-*` first.

---

## Before irreversible actions

**Check and click in separate runs** (the most expensive mistake recorded)
- Symptom: a booking-type radio was set and even screenshotted; in the next script the form had reverted on its own, and the "Done" click locked the wrong type ("cannot be changed after selection").
- Cause: the code printing the radio state and the "Done" click ran in the same execution, so the output was read after the click.
- Fix: check, let a human read the output, click in the next run.

**Steps that reload the page**
- After a reload step (e.g. phone verification), re-verify the whole form. Service name, promo text, owner name and website had all been reset.

**Compare values just before saving**
- A one-line guard (`if (el.value !== expected) abort`) prevents empty or truncated values from being saved silently.
- For DNS, verify after saving against a public resolver (`Resolve-DnsName <name> -Server 1.1.1.1` or `dig @1.1.1.1`). The dashboard shows what it wrote, not what the world sees.

**Reload is the cheapest undo before saving**
- Items entered in the wrong order: reload and re-enter in order instead of hunting for a delete UI.

**Do not type secrets into dashboards**
- Reading and screenshots passed, but the agent's permission layer refused scripts that open "add variable" modals. Do not work around it; tell a human which value goes where.
- Likewise, "save" on ad assets was refused as a real transaction in auto mode. Fill the form, let a human press save.

**Tabs restored by the session**
- A reused profile restored an unrelated tab holding a half-filled application form with personal data. List every tab's URL and title before touching anything, never navigate a form or payment tab, and open a new tab for your own work.

---

## Flows that spend money

**Read the URL, not the title**
- Symptom: a screen described as "turning on Analytics" was `ads.google.com/aw/campaigns/new/express`, the Google Ads smart-campaign creation flow; property creation was only a step inside it. The end of that flow is billing and serving.
- Cause: the flow traps you. `/aw/campaigns` and `/aw/billing/summary` both bounce back into it; only leaving the domain gets out. It also appeared to create an extra Analytics property on the way.
- Fix: check `location.href` before every button. "Skip" is not an exit; it moves forward (ad text, budget, payment). Before pressing anything shaped like cancel or close, confirm whether it goes forward or back.

**Ad blockers**
- Google Ads shows "Google Ads can't work when you're using an ad blocker". Tables still read; the campaign settings drawer ("Other settings": locations, languages stuck on loading) and change-history expanders do not. Tell the human to turn it off before Ads work.

**Google Ads URLs** (observed 2026-10)
- Working (need `ocid`, and `campaignId` where relevant; clicking a campaign name via JS puts `campaignId` into the URL): `/aw/campaigns`, `/aw/adgroups`, `/aw/keywords`, `/aw/keywords/searchterms`, `/aw/ads`, `/aw/locations`, `/aw/changehistory`, `/aw/assetreport/associations`.
- 404: `/aw/settings/campaign` (settings open only from the ad-group screen's drawer), `/aw/assets`, `/aw/assetreport/associations/sitelinks`, `/aw/accountlinking`.
- Tables: read `[role=row]` `innerText`; page with `chevron_right` and confirm via "1-10 of N".
- Keyword status "not eligible / low search volume" directly explains zero impressions; read every page.
- Conversions: the summary screen, then "view all conversion actions" (URL does not change). A goal card marked "misconfigured" meant the primary action was paused or only secondary actions were usable.

**Naver Ads: separate session, separate screens**
- Naver Ads (`ads.naver.com`) does not share the session with other Naver services: Smart Place opened logged in while Ads bounced to `?needLogin=true` and showed a real login form. Check each domain directly.
- The multi-dimensional report never includes today ("up to the previous day", last aggregation 00:20). Today's numbers live in campaign management (near real time). An empty "today" in the report is the spec, not an outage.
- The date picker defaults to "last 7 days (excluding today)". Presets change nothing until the popup's "OK" is pressed, and the table updates after the label does, so wait on the table, not the label.
- For "spent today / balance", the account dashboard gives everything without a date picker. Its "spent today" and campaign management's "total cost" differ by about 1 won (rounding); do not mix the two in one record.
- The URL for change history is `/sa/tool/history`. A wrong path redirects without error to the campaign list, which looks plausible. Always check `location.href` after navigating.
- "Why isn't my ad showing": do not search for your own ad (invalid clicks, cost). Use the exposure diagnosis tool, and note it has two answers: *diagnosis* says only whether the ad is blocked; *exposure view* shows what actually appears (Power Link: 10 slots on PC, 4 on mobile). They disagreed: "eligible" on both, yet absent from the 4 mobile slots. With only 4 slots, rank 4 to 5 is a cliff.
- Left menu items are collapsed antd submenus with SPA routing (no `href`); expand the submenu title and click by text. "Detail data" is a dropdown trigger (PC/mobile, weekday, hour, region). There is no daily view; "last 7 days (incl. today)" split by weekday serves as one.

**Cross-origin payment iframes**
- Advertiser identity verification runs in a cross-origin `payments.google.com` iframe that page-level `eval` cannot see. Read only: find the frame via `Page.getFrameTree`, create a context with `Page.createIsolatedWorld`, run `Runtime.evaluate` with that `contextId`.

---

## Captchas and rate limits

The rule: **a captcha or rate limit means stop for that source for the day.** Pick a different source.

**Naver Place**
- Symptom: listing pages are quiet; opening details (and even just scrolling two detail pages with no clicks) brings up the Naver security check (`ncaptcha-iframe-*`), after which every click is intercepted. In a multi-area search, plain list queries switched to the security page on the third query.
- Fix: open each business once at most. For many areas or keywords, Naver is not a candidate from the start. For hours, go to the business's own site (e.g. Kyobo Book's store pages give hours and closed days).
- To check your own business without logging in, read the public place page: what it shows is what passed review. But "reservation" in the info tab is an amenity label, not the reservation button; check the action row.
- The `pcmap` DOM ships with `div#wtm-captcha-root` already present. Do not hammer it.
- List anchors have no `href` (JS routing). Results sit in `iframe#searchIframe`, details in `iframe#entryIframe`.

**Better route for Naver places**
- `https://search.naver.com/search.naver?ssc=tab.place.all&query=<q>` needs no iframe and no place ID; `document.body.innerText` gives name, address, hours, phone, menu and prices in plain text (0.6 to 1.3 s).
- For a link, search one exact business name: the HTML contains `entry/place/([0-9]{7,12})`. A single-business search returns one ID; a list search returns 25 and you cannot tell which is which. Link: `https://map.naver.com/p/entry/place/{id}`.
- **Verify every ID** by opening `pcmap.place.naver.com/place/{id}/home` and reading the first line. In a batch of 30, one first ID belonged to a different business nearby; the second ID was right.
- Wait on `(document.body.innerText||'').length > 20`, not on the title (the title appears before the body). On a 1-result list page, `innerText` is under 200 chars, so a "length > 200" wait times out with results on screen.
- "Open now" is not "open today": the list shows "closed today / closed every <weekday>", which summaries drop.
- "N km from current location" is measured from the map centre, not the user. The same store showed 4.2 km and 121 km depending on the path taken. Identify places by name.

**Kakao Map**
- Seven consecutive queries in one session stayed quiet, and results are in the top-level DOM (no iframe). Selector: `li.PlaceItem` with `data-id`; fields `address`, `phone`, `scoreNum`, `numberofscore`, `homepage`, `periodTxt`.
- Check the subcategory: one regional search returned 15 results that were all non-medical scalp shops.
- A rating shown as `((30))` means reviews are not provided.
- 15 results is the ceiling. The page-2 control has zero size; forcing a click changes nothing, and you end up counting the same 15 twice. Split the query (by category) instead.
- Queries are literal: "<plaza name> bars" returned 0, "<district> bars" returned 15. Keep the area short, use the standard category name.
- A zero-result page is a normal 200. Add `|| /no results text/.test(document.body.innerText)` to the wait condition, or the batch dies on a 30 s timeout.

**Malls**
- For tenants, the mall's own site is the source of truth; the map missed several stores. On a "more" button the same list was appended again to `innerText`; read only the first copy (the one with category headers), or counts triple.

**GitHub search**
- Three different searches in a row, logged out, and the third became "You have exceeded a secondary rate limit": a static page, HTTP 200, so it reads as "0 results" unless you read the text. The notice says "up to an hour". Search once, go straight to repo pages, or use the REST API.

---

## Sessions and profiles

**A debug port is an account boundary**
- Each port gets its own profile directory, so changing the port changes the profile, which changes the logged-in account. When accounts belong to different people (yours and a client's), give each its own port and profile; one shared profile sends your work out under their account or the reverse.
- Check that the boundary actually exists: read the running Chrome's command line (on Windows, `Win32_Process`) for `--user-data-dir`. Observed: the port meant for one account was running a different tool's profile with two people's sessions logged in together, so "separate ports, separate accounts" was not true on that machine. Closing Chrome or moving profiles is a human decision.
- Make sure every command in your tool honours the port. One tool's fast path had the default port hard-coded, so work launched on a second port was read and driven on the first.
- A login check is valid only for that moment. Naver sessions died when Chrome restarted. Check again every time; do not expect a session to be alive.

**Do not assume the profile is empty**
- A reused profile still had a government site's session: going to `login.do` skipped the form entirely (a missing `#loginId` meant already logged in, not failure).

**Do not assume it is alive either**
- That same profile was logged out of Naver after a restart. After every restart, check logout state first and call a human if needed.

**Which account is this console?**
- Supabase and Cloudflare were logged in with two Google accounts whose addresses differ by one character. Supabase does not 404 on an unknown project ref; it silently redirects to `/dashboard/org/<other org>`, so you can run SQL in the wrong project.
- Order: `/dashboard/account/me` for the email, then confirm the project or worker exists, then act.

**Supabase dashboard** (observed 2026-10)
- The SQL editor is Monaco: its typing target sits under the rendered lines, so a safe click reports "covered" and typing never lands. Open the editor with the query already in it instead: `/dashboard/project/<ref>/sql/new?content=<url-encoded SQL>`, check the text on screen, then press Run. Strip comments and newlines first so nothing depends on how the editor indents.
- With unsaved edits in the editor, navigating away by URL silently does nothing (a leave-page guard). Use the sidebar links, or close the tab.
- A `content` URL of about 6.6 KB opened an empty editor with no error; 3.5 KB worked. Split long setup SQL into parts.
- Run stops on "Potential issue detected" for any `drop`, including `drop trigger if exists` on a table created a line earlier. In first-time setup, leave such lines out rather than confirming a destructive-operation prompt.
- A personal access token kept in one app's config can be scoped to that app's project: listing still works, but creating a project and reading the organisation return 403. Check what a token can do before planning around it.
- "Asia-Pacific" on the new-project form placed the project in Sydney, and the overview showed a warm-pool name (`warm-project-pool_...`) for about half a minute before the real name appeared.
- On the Free plan the two-project limit counts active projects; a paused project does not take a slot.
- The new publishable key (`sb_publishable_...`) worked both as `apikey` and as the `Authorization: Bearer` value for anonymous reads, and row-level security refused an anonymous insert with HTTP 401 and code `42501`.

**Where auth lives**
- Discord and Telegram Web keep auth in `localStorage`, not cookies. Saving cookies alone does not keep the session.

**Picking the tab**
- If your tool picks a tab by title or URL substring (as `--tab` and `CDP(match=...)` do), give it a URL fragment, not an ID returned by "new tab" (that fails as "no such tab"), and not a regex (`"A\|B"` does not work). Titles change on every navigation; a URL fragment is more stable. Re-list tabs during long sessions.

**Uploading into a same-origin iframe**
- A Google Business Profile photo dialog was an iframe, but on the same origin as its parent. Setting `location.href = document.querySelectorAll('iframe')[1].src` moved the whole tab to that page, where `input[type=file]` (multiple) sat at the top level. Before uploading, compare against photos already present: a ledger-only check produced 20 duplicate photos. Downscaling thumbnails in the page (`fetch` -> `createImageBitmap` -> 16x12 greyscale, distance < 12) finds duplicate pairs in one pass.

---

## Iframes

**Google Apps Script web apps: a double iframe** (`script.google.com/macros/s/.../exec`)
- Symptom: `goto` succeeds, the title is right, but the text is three lines ("This application was created by a Google Apps Script user / Report abuse") and there are 0 `select`s and 0 `input`s. Easy to misread as "page did not render".
- Cause: two shells.
  ```
  script.google.com/macros/s/.../exec          <- type=page, notice only
   └ ...googleusercontent.com/userCodeAppPanel  <- cross-origin OOPIF, type=iframe
      └ iframe#userHtmlFrame                    <- same-origin; the real content is here
  ```
  A tab picker that reads `/json` and keeps only `type=page` can never reach the OOPIF. Opening the `userCodeAppPanel` URL directly loads, but with no parent context the inner frame is `src="/blank"` and `body.innerText.length === 0`, so a "length > 100" wait burns the full timeout.
- Fix: open the `exec` URL first (that creates the OOPIF), find the target in `/json` with `type == "iframe"` and `userCodeAppPanel` in its URL, attach to that target's websocket, then go one level deeper:
  ```python
  import json, urllib.request
  t = next(x for x in json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
           if x["type"] == "iframe" and "userCodeAppPanel" in x["url"])
  # attach a CDP client to t["webSocketDebuggerUrl"], then:
  # Runtime.evaluate {"expression": "document.getElementById('userHtmlFrame')"
  #                                 ".contentDocument.body.innerText.slice(0,500)",
  #                   "returnByValue": true}
  ```
  The same trap is in every Apps Script web app (forms, lookup tools, internal tools).

**Other frames seen**
- Naver Place results and details are inside `iframe#searchIframe` and `iframe#entryIframe`; switch frames or you read nothing.
- Cross-origin payment frames: see "Flows that spend money".

---

## Waiting and reading results

- Scope progress polling to the item's row. A completion check on `'Download' in body.innerText` matched an "app download" FAQ at the bottom and declared success at once.
- When scoping to a row, climb with `parentElement` far enough: the result link was a sibling of the filename div, not a child.
- A wait condition must accept "no results" and must not pass on a title alone (see Captchas and rate limits).
- After any navigation that can redirect silently, read `location.href`.

---

## Tooling traps (scripts, shells, encodings)

**Newlines in JS inside step JSON are unescaped twice**
- Symptom: an `eval` step dies with `SyntaxError: Invalid or unexpected token`; the same JS runs fine in the console. Hit three times in one session.
- Cause: a backslash-n in the JSON is turned into a real newline by the JSON parser and lands inside a JS string literal. With a shell heredoc in the middle it is three layers: even a quoted heredoc (`cat <<'EOF'`) turned backslash-n into a real newline in that environment.
- Fix, in order: use `String.fromCharCode(10)` instead of backslash-n; build the JSON with `json.dumps` instead of by hand (and double the backslashes you want JS to see); regexes too, since JSON rejects `\s` and `\d` as unknown escapes. Write files containing backslashes with a file-writing tool, not a heredoc.

**PowerShell pipes add a BOM** (Windows PowerShell 5.1)
- Symptom: `Get-Content -Raw steps.json | python ... -` fails with `JSONDecodeError: Unexpected UTF-8 BOM`, even after re-emitting the file without a BOM.
- Cause: the native-to-native pipe re-encodes stdout and prepends a BOM.
- Fix: write the JSON without a BOM and feed it from bash (`< file`).

**Non-ASCII step JSON piped on Windows**
- Symptom: `UnicodeDecodeError` (the file was written as cp949). This arrives before the BOM problem.
- Fix: keep the JSON pure ASCII (`ensure_ascii=True`, replace labels with indices; URLs are already percent-encoded). When printing results, `sys.stdout.reconfigure(encoding='utf-8')`, or IDs print fine while names are garbled.

**Two different `/tmp`s**
- On Windows, Python's `/tmp` (drive-root `tmp`) and Git Bash's `/tmp` are different folders. Pipe, or use absolute paths.

---

## Removed from the source

The source is a private field log. Removed: names of people, the school and its systems, clubs,
client businesses and their accounts, account and ad-account numbers, the table mapping ports to
people and projects, internal project names, the harness's own vocabulary (rule numbers, internal
module and function names, internal file and folder names, sweep and errand terms), and dated
incident narratives (kept only as "observed 2026-08"-style dates where the date matters). Internal
function names were rewritten as generic CDP or Selenium terms, or as this repository's `CDP` and
`Pointer` API. Also removed: local-machine facts with no general value (which CLIs are installed,
local paths), a research-site section and university admission-rate sites unrelated to these
topics, and several site-specific coordinates and constant IDs (mall category IDs, Google Ads
help-article numbers, menu details for adding ad assets) that would go stale. The terms and
court-case lines keep only the facts the source stated; one cause the source marked as unconfirmed
(an ad-blocker banner as the reason a submit button did not respond) was left out.
