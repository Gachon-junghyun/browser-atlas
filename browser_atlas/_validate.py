"""Offline validation and conservative merging of reviewed atlas maps."""
from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import _atlas

ROLES = set('button link textbox searchbox combobox checkbox radio tab menuitem option spinbutton'.split())
ACTS = set('navigate search filter sort paginate tab expand open_detail download input login submit pay send delete apply publish'.split())
DANGER = set('submit pay send delete apply publish'.split())
FIELDS = {
    'site': set('label access hosts go has can prefer why pages verified source'.split()),
    'page': set('url ready controls traps verified'.split()),
    'control': set('role name where act does then irreversible css verified dynamic unnamed act_inferred'.split()),
}
WORDS = ('DOMAIN.md', 'HANDOVER', 'users/', 'module_')
HOST = re.compile(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\Z')
TRACE = re.compile(r'C:\\Users\\|/home/|/Users/|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)|(?<!\d)\d{6}-[1-4]\d{6}(?!\d)|\bsk-[A-Za-z0-9_-]+|\bghp_[A-Za-z0-9_]+')
# inside a URL, digit runs are ids (pageId=0101010000) and /home/ is a web path, not a home folder
TRACE_URL = re.compile(r'C:\\Users\\|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\bsk-[A-Za-z0-9_-]+|\bghp_[A-Za-z0-9_]+')
# a key mixes letters and digits; camelCase handler names (selectFooBarList) and URL paths are not keys
TOKEN = re.compile(r'(?<![\w/])(?=[A-Za-z0-9+=_-]*\d)(?=[A-Za-z0-9+=_-]*[A-Za-z])[A-Za-z0-9+=_-]{32,}(?![\w/])')
HASH_CSS = re.compile(r'\.(?:css-[\w-]+|sc-[\w-]+|_[a-zA-Z0-9]{5,})|\[class\s*[*^$]?=\s*[\"\x27]?[^\]]*(?:css-|sc-|_[a-zA-Z0-9]{5,}|[a-zA-Z]*\d[a-zA-Z0-9]{4,})')
VARIABLE = re.compile(r'\d+\s*(?:건|개|results)|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}', re.I)


class _Pairs(list):
    """Distinguish JSON objects from arrays until duplicate paths are known."""


def _finding(level, site, path, msg):
    return dict(level=level, site=site, path=path, msg=msg)


def read_map(path):
    findings = []
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_Pairs,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError('invalid JSON constant ' + value)))
    except (OSError, UnicodeError, ValueError) as e:
        return None, [_finding('error', '', '', str(e))]

    def unpack(value, site='', path=''):
        if isinstance(value, _Pairs):
            result = {}
            for key, item in value:
                child = f'{path}.{key}' if path else key
                owner = site or key
                if key in result:
                    findings.append(_finding('error', owner, child, 'duplicate key: ' + key))
                result[key] = unpack(item, owner, child)
            return result
        if isinstance(value, list):
            return [unpack(v, site, f'{path}[{i}]') for i, v in enumerate(value)]
        return value
    data = unpack(raw)
    if not isinstance(data, dict):
        findings.append(_finding('error', '', '', 'top level must be an object'))
        return None, findings
    return data, findings


def trace_words(path=None):
    path = Path(path) if path else _atlas.DEFAULT_MAP.parent / '.trace_words'
    if not path.exists():
        return WORDS
    return (*WORDS, *(line.split('#', 1)[0].strip() for line in path.read_text(encoding='utf-8').splitlines()
                     if line.split('#', 1)[0].strip()))


def validate(data, words=None):
    findings = []
    words = trace_words() if words is None else words
    if not isinstance(data, dict):
        return [_finding('error', '', '', 'top level must be an object')]

    def add(site, path, msg, level='error'):
        findings.append(_finding(level, site, path, msg))

    def strings(value, site, path):
        if isinstance(value, str):
            # Query placeholders are templates, not secrets. Actual query values are checked.
            is_url = value.startswith(('http://', 'https://'))
            text = re.sub(r'\{[^{}]*\}', ' ', value) if is_url else value
            for match in (TRACE_URL if is_url else TRACE).finditer(text):
                add(site, path, 'personal trace or token found: ' + match.group())
            token_text = re.sub(r'[/&?=:.]', ' ', text) if value.startswith(('http://', 'https://')) else text
            for match in TOKEN.finditer(token_text):
                add(site, path, 'API-key-looking token found: ' + match.group())
            for word in words:
                if word.casefold() in value.casefold():
                    add(site, path, 'trace word found: ' + word)
        elif isinstance(value, dict):
            for k, v in value.items():
                strings(v, site, f'{path}.{k}' if path else k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                strings(v, site, f'{path}[{i}]')

    def fields(obj, kind, site, path):
        for key in sorted(obj.keys() - FIELDS[kind]):
            add(site, f'{path}.{key}'.lstrip('.'), 'unknown field: ' + key, 'warning')
        if 'verified' in obj:
            value = obj['verified']
            try:
                if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError()
                datetime.date.fromisoformat(value)
            except ValueError:
                add(site, f'{path}.verified'.lstrip('.'), 'verified is not an ISO date', 'warning')

    def url(value, site, path):
        try:
            template = re.sub(r'\{[^{}]*\}', 'placeholder', value) if isinstance(value, str) else None
            u = urlsplit(template) if template is not None else None
            if not u or u.scheme not in ('http', 'https') or not u.hostname or u.username or re.search(r'\s', template):
                raise ValueError()
            u.port
            return u.hostname
        except ValueError:
            add(site, path, 'expected an http/https URL')
            return None

    for site, entry in data.items():
        strings(entry, site, '')  # Also inspect comment strings; they can carry personal traces.
        if site.startswith('_'):
            continue
        if not re.fullmatch(r'[a-z0-9]+(_[a-z0-9]+)*', site):
            add(site, '', 'invalid site key')
        if not isinstance(entry, dict):
            add(site, '', 'site must be an object')
            continue
        fields(entry, 'site', site, '')
        for key in ('label', 'access', 'hosts'):
            if key not in entry:
                add(site, key, 'missing required field: ' + key)
        if entry.get('access') not in _atlas.ACCESS:
            add(site, 'access', 'invalid access')
        hosts = entry.get('hosts')
        if hosts == [] and entry.get('access') == 'blocked' and not entry.get('go') and not entry.get('pages'):
            # a blocked non-web entry (a desktop client) has nothing to cover
            add(site, 'hosts', 'empty hosts on a blocked entry with no URLs', 'warning')
        elif not isinstance(hosts, list) or not hosts or any(not isinstance(h, str) or not HOST.fullmatch(h) for h in hosts):
            add(site, 'hosts', 'hosts must be a non-empty list of bare lowercase hostnames')
        hosts = [h for h in hosts if isinstance(h, str)] if isinstance(hosts, list) else []
        if entry.get('access') == 'blocked' and (not isinstance(entry.get('why'), str) or not entry['why'].strip()):
            add(site, 'why', 'blocked site requires a non-empty why')
        go = entry.get('go', {})
        if not isinstance(go, dict):
            add(site, 'go', 'go must be an object')
        else:
            for key, value in go.items():
                url(value, site, 'go.' + key)
        pages = entry.get('pages', {})
        if not isinstance(pages, dict):
            add(site, 'pages', 'pages must be an object')
            continue
        for key, page in pages.items():
            path = 'pages.' + key
            if not isinstance(page, dict):
                add(site, path, 'page must be an object')
                continue
            fields(page, 'page', site, path)
            host = url(page.get('url'), site, path + '.url')
            if host and not any(host == h or host.endswith('.' + h) for h in hosts):
                add(site, path + '.url', 'page URL host not covered by hosts')
            controls = page.get('controls', [])
            if not isinstance(controls, list):
                add(site, path + '.controls', 'controls must be a list')
                continue
            if not 3 <= len(controls) <= 20:
                add(site, path + '.controls', 'page has fewer than 3 or more than 20 controls', 'warning')
            seen = {}
            for i, c in enumerate(controls):
                cp = f'{path}.controls[{i}]'
                if not isinstance(c, dict):
                    add(site, cp, 'control must be an object')
                    continue
                fields(c, 'control', site, cp)
                for required in ('role', 'where', 'act', 'does'):
                    if required not in c:
                        add(site, cp + '.' + required, 'missing required field: ' + required)
                if not isinstance(c.get('role'), str) or c['role'] not in ROLES:
                    add(site, cp + '.role', 'invalid role')
                if not isinstance(c.get('act'), str) or c['act'] not in ACTS:
                    add(site, cp + '.act', 'invalid act')
                name = c.get('name')
                if (not isinstance(name, str) or not name.strip()) and c.get('unnamed') is not True:
                    add(site, cp + '.name', 'empty name requires unnamed: true')
                if c.get('unnamed') is True and (not isinstance(c.get('css'), str) or not c['css'].strip()):
                    add(site, cp + '.css', 'unnamed: true requires css')
                if isinstance(c.get('act'), str) and c['act'] in DANGER and c.get('irreversible') is not True:
                    add(site, cp + '.irreversible', 'dangerous act requires irreversible: true')
                if isinstance(c.get('css'), str) and HASH_CSS.search(c['css']):
                    add(site, cp + '.css', 'hashed-looking class selector', 'warning')
                dynamic = c.get('dynamic') is True
                name = name if isinstance(name, str) else ''
                if (dynamic and not re.search(r'<[^<>]+>', name)) or (not dynamic and VARIABLE.search(name)):
                    add(site, cp + '.name', 'looks variable, mark dynamic (use a <...> placeholder)', 'warning')
                identity = (str(c.get('role')), name)
                if identity in seen and not (dynamic or seen[identity]):
                    # real pages can repeat a name (a tab and a sort link both called "Level"); name lookup takes the first
                    add(site, cp, 'duplicate control role + name: lookup by name finds the first one', 'warning')
                seen[identity] = seen.get(identity, False) or dynamic
    return findings


def validate_file(path, words=None):
    data, findings = read_map(path)
    if data is not None:
        findings.extend(validate(data, words))
    return data, findings


def has_errors(findings):
    return any(f['level'] == 'error' for f in findings)


def merge(source, into=None, replace=(), dry_run=False):
    into = _atlas.map_path(into)
    incoming, findings = validate_file(source)
    report = dict(findings=findings, added=[], replaced=[], conflicts=[], format={}, written=False)
    if has_errors(findings):
        return report
    current, errors = read_map(into)
    findings.extend(errors)
    if has_errors(findings):
        return report
    merged = dict(current)
    for key, entry in incoming.items():
        if key.startswith('_'):
            continue
        if key in current and key not in replace:
            report['conflicts'].append(dict(site=key, kind='existing key, skipped'))
            continue
        for other, old in current.items():
            if other.startswith('_') or other == key or not isinstance(old, dict):
                continue
            for host in entry['hosts']:
                for previous in old.get('hosts', []) if isinstance(old.get('hosts'), list) else []:
                    if not isinstance(previous, str):
                        continue
                    if host == previous or host.endswith('.' + previous) or previous.endswith('.' + host):
                        report['conflicts'].append(dict(site=key, kind='host overlap', other=other, old=previous, new=host))
        if key in current:
            old_entry = current[key] if isinstance(current[key], dict) else {}
            old_pages = old_entry.get('pages', {})
            old_controls = [c for p in (old_pages.values() if isinstance(old_pages, dict) else [])
                            if isinstance(p, dict) and isinstance(p.get('controls'), list)
                            for c in p['controls'] if isinstance(c, dict)]
            for page in entry.get('pages', {}).values():
                for c in page.get('controls', []):
                    for old in old_controls:
                        same = (c.get('role'), c.get('where')) == (old.get('role'), old.get('where'))
                        if (same or c.get('css') and c.get('css') == old.get('css')) and c.get('name') != old.get('name'):
                            report['conflicts'].append(dict(site=key, kind='same control, different name', old=old, new=c))
            report['replaced'].append(key)
        else:
            report['added'].append(key)
        merged[key] = entry
    findings.extend(validate(merged))
    if has_errors(findings):
        return report
    original = into.read_bytes()
    text = original.decode('utf-8')
    match = re.search(r'\n([ \t]+)"', text)
    indent = match.group(1) if match else (None if '\n' not in text.rstrip() else 2)
    newline = '\r\n' if b'\r\n' in original else '\n'

    def serialize(value):
        return (json.dumps(value, ensure_ascii=False, indent=indent) + '\n').replace('\n', newline).encode('utf-8')
    round_trip = serialize(current) == original
    report['format'] = dict(round_trip=round_trip, indent=indent, newline=repr(newline),
                            note='existing map round-trips byte-identically' if round_trip else 'existing map does not round-trip; using closest stable JSON format')
    if not dry_run and (report['added'] or report['replaced']):
        into.write_bytes(serialize(merged))
        report['written'] = True
    return report


def cli(argv):
    parser = argparse.ArgumentParser(prog='browser_atlas atlas', description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    check = subs.add_parser('validate', help='validate a map without opening a browser')
    check.add_argument('path', nargs='?')
    check.add_argument('--json', action='store_true')
    combine = subs.add_parser('merge', help='validate and merge sites conservatively')
    combine.add_argument('source')
    combine.add_argument('--into')
    combine.add_argument('--dry-run', action='store_true')
    combine.add_argument('--replace', nargs='+', default=[], action='extend')
    combine.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            _, findings = validate_file(_atlas.map_path(args.path))
            report = dict(findings=findings)
        else:
            report = merge(args.source, args.into, args.replace, args.dry_run)
            findings = report['findings']
    except (OSError, UnicodeError) as e:
        findings = [_finding('error', '', '', str(e))]
        report = dict(findings=findings)
    report['errors'] = sum(f['level'] == 'error' for f in findings)
    report['warnings'] = sum(f['level'] == 'warning' for f in findings)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f"{f['level']}: {f['site']} {f['path']}: {f['msg']}")
        if args.command == 'merge':
            print(json.dumps({k: v for k, v in report.items() if k != 'findings'}, ensure_ascii=False, indent=2))
        print(f"{report['errors']} errors, {report['warnings']} warnings")
    return int(bool(report['errors']))
