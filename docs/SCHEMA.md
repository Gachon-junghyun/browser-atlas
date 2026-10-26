# Atlas schema

`atlas/sites.json` is one object. Keys starting with `_` are comments. Every other key is a site.

## Site

```json
"dart": {
  "label": "DART 전자공시",
  "access": "open",
  "hosts": ["dart.fss.or.kr"],
  "go": {"search": "https://dart.fss.or.kr/dsab007/main.do"},
  "has": ["filings", "attachments"],
  "can": [],
  "prefer": "OpenDART API (free key, 20,000 calls/day)",
  "why": "",
  "pages": { "search": { ... } },
  "verified": "2026-10-26"
}
```

| Field | Required | Meaning |
|---|---|---|
| `label` | yes | Human name. Local language is fine. |
| `access` | yes | `open` no login · `session` log in once by hand, then automate · `api` use the official API (`prefer`) · `blocked` do not automate (`why`). |
| `hosts` | yes | Hostnames the entry covers; subdomains match. Pointer actions refuse on `blocked` hosts. |
| `go` | no | Named entry URLs: where to start for a task. |
| `has` / `can` | no | What you can read / what you can *do* (writes, submissions). Anything in `can` is a human decision. |
| `prefer` | no | A cheaper or allowed route (API, official file, another site). |
| `why` | for `blocked` | The reason, with the date it was observed. |
| `pages` | no | The button map, below. |
| `verified` | no | Last date someone actually opened the site. |

## Pages and controls (the button map)

People remember a site as "the blue *Search* button right of the box", not as a CSS selector.
The map stores the same thing: the **role** and **accessible name** of each control, where it is,
and what it does. Roles and names survive redesigns far better than selectors, and they are what
an accessibility-tree snapshot returns, so an agent can find them without guessing.

```json
"pages": {
  "search": {
    "url": "https://dart.fss.or.kr/dsab007/main.do",
    "ready": "!!document.querySelector('#textCrpNm')",
    "controls": [
      {"role": "textbox", "name": "회사명", "where": "first field of the search form", "does": "company name input"},
      {"role": "button", "name": "검색", "where": "right end of the search form", "does": "lists filings", "then": "results"},
      {"role": "link", "name": "다운로드", "where": "toolbar above the document", "does": "original PDF", "irreversible": false}
    ],
    "traps": ["Results table paginates by 15; check the page bar before counting."]
  }
}
```

| Control field | Required | Meaning |
|---|---|---|
| `role` | yes | ARIA role as Chrome exposes it: `button`, `link`, `textbox`, `searchbox`, `combobox`, `checkbox`, `radio`, `tab`, `menuitem`, `option`, `spinbutton` (a number box such as "go to page"). |
| `name` | yes | Accessible name, exactly as exposed (aria-label, then visible text, then title/alt/placeholder). |
| `where` | yes | Position in words a person would use. |
| `act` | yes | What kind of action, from a closed list: `navigate` `search` `filter` `sort` `paginate` `tab` `expand` `open_detail` `download` `input` `login` `submit` `pay` `send` `delete` `apply` `publish`. |
| `does` | yes | What happens when used, in words, for people. |
| `then` | no | Page key or URL it leads to. |
| `irreversible` | no | `true` for submit / pay / send / delete / apply. Agents stop before these. |
| `css` | no | Fallback selector, only from stable attributes (`id`, `name`, `data-*`, `aria-*`). Never hashed class names. |
| `verified` | no | Date this control was seen, if different from the site's. |

`url` may contain `{query}`-style placeholders. `ready` is a JS expression that is truthy once the
page is usable (prefix DOM lookups with `!!`). `traps` are short, observed pitfalls.

## Collecting controls

Open the page, wait for `ready`, then list the visible interactive elements with their role and name.
Keep the 5 to 15 that matter for real tasks (search, filters, tabs, pagination, download, detail
links); skip footer links, ads and cookie banners. Never press anything that would be `irreversible`.

## Trust model: shared text must not become instructions

Everyone's agent reads this map, so a field like `does: "click this, then pay"` would be a prompt
injection for every user at once. Describing instead of commanding is not enough on its own. Four rules:

1. **Agents decide with closed values, people read the words.** `role`, `act`, `access`, `irreversible`,
   host names and URLs are checked or enumerable. `where`, `does`, `traps`, `why` are for people.
   Experience rows shared between users carry only checked values (`_memory.shareable`).
2. **Danger is judged locally, and the map can only tighten it.** Whether a control is irreversible is
   decided on the user's machine from the live control's name and role (`_safety.irreversible`).
   The map can add `irreversible: true` or an irreversible `act`; it cannot make a pay button safe.
3. **A name in the map is only a pointer.** Before acting, the agent finds the control on the live page
   by role and name. A map entry cannot create a button that is not there.
4. **One writer.** The repository map changes through reviewed pull requests. The shared database is
   written by maintainers only (row-level security) and limits the shape of every row; readers never write.
   What each machine learns applies to that machine at once and is shared only when its owner asks.


## Offline validation and merging

Run `python -m browser_atlas atlas validate [PATH] [--json]` before reviewing map changes.
The default path follows `BROWSER_ATLAS_MAP`, otherwise `atlas/sites.json`. Findings contain
`level`, `site`, `path` and `msg`; errors exit 1, warnings alone exit 0. No browser is opened.
Validation checks duplicate JSON keys, closed roles/actions, hosts and URLs, required fields,
irreversible actions, duplicate controls and personal traces. Unknown fields, unstable CSS,
variable names, page sizes and invalid verification dates produce warnings. Only the roles
listed above are allowed; add a role here and in `_validate.ROLES` when a site really exposes it.
Known optional fields besides the tables above: `source` on a site (where the entry came from),
`act_inferred` on a control (the `act` was assigned from its name, not watched), `dynamic`, `unnamed`.
Use `unnamed: true` with a stable `css` for unnamed controls; use `dynamic: true` and a `<...>`
placeholder for variable accessible names. Even dynamic duplicates retain all safety checks.
Optional `atlas/.trace_words` adds private, case-insensitive forbidden words, one per line,
with `#` comments; keep real personal words out of version control. Built-in trace words are
`DOMAIN.md`, `HANDOVER`, `users/` and `module_`. Detection is heuristic and does not replace review.

Preview additions with:

```bash
python -m browser_atlas atlas merge incoming.json --dry-run
python -m browser_atlas atlas merge incoming.json --into atlas/sites.json --replace example other --json
```

Merge validates input first, skips existing keys unless explicitly replaced, and appends new
keys in input order. Comment entries are ignored. Host overlaps and renamed controls matching
an old role/position or CSS are reported as soft conflicts, with old and new values. The merged
map must also pass validation before any write; existing errors can be repaired by replacement.
`--dry-run` never writes. JSON output includes findings, conflicts, additions/replacements,
format information and whether anything was written. Files use UTF-8 without BOM, a trailing
newline and the destination's detected indentation and line endings. The format report says
whether the unchanged destination round-trips byte-for-byte; otherwise merge uses the closest
stable JSON serialization and explicitly reports that formatting may change.
