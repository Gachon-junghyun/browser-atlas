import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from browser_atlas import _validate as v
from browser_atlas.__main__ import main


def clean(host='example.com'):
    return {'example': {'label': 'Example', 'access': 'open', 'hosts': [host],
                        'go': {'search': f'https://{host}/?q={{query}}'},
                        'pages': {'search': {'url': f'https://sub.{host}/', 'controls': [
                            {'role': 'button', 'name': name, 'where': name, 'act': 'search', 'does': 'searches'}
                            for name in ('Search', 'Next', 'Previous')]}}}}


class ValidateTest(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(v.validate(clean()), [])
        self.assertFalse(v.has_errors(v.validate({'example': {'label': 'Example', 'access': 'open', 'hosts': ['example.com']}})))

    def test_site_errors(self):
        for field in ('label', 'access', 'hosts'):
            data = clean()
            del data['example'][field]
            self.assertTrue(v.has_errors(v.validate(data)), field)
        for field, value in [('access', 'unknown'), ('hosts', []), ('hosts', ['HTTPS://example.com']),
                             ('hosts', ['Example.com']), ('hosts', ['example.com/path']),
                             ('hosts', ['example.com:80']), ('hosts', ['some host']), ('hosts', None),
                             ('pages', []), ('go', []), ('go', {'home': 'file:///test'})]:
            data = clean()
            data['example'][field] = value
            self.assertTrue(v.has_errors(v.validate(data)), (field, value))
        data = clean()
        data['Bad-Key'] = data.pop('example')
        self.assertTrue(v.has_errors(v.validate(data)))
        data = clean()
        data['example']['access'] = 'blocked'
        self.assertTrue(v.has_errors(v.validate(data)))
        data['example']['why'] = ' '
        self.assertTrue(v.has_errors(v.validate(data)))

    def test_page_errors(self):
        for value in ('ftp://example.com', 'https://evil-example.com', 'https://example.com.evil.org', None):
            data = clean()
            data['example']['pages']['search']['url'] = value
            self.assertTrue(v.has_errors(v.validate(data)), value)

    def test_control_errors(self):
        for field in ('role', 'where', 'act', 'does', 'name'):
            data = clean()
            del data['example']['pages']['search']['controls'][0][field]
            self.assertTrue(v.has_errors(v.validate(data)), field)
        for field, value in [('role', 'listbox'), ('act', 'unknown'), ('name', ''), ('unnamed', True)]:
            data = clean()
            data['example']['pages']['search']['controls'][0][field] = value
            self.assertTrue(v.has_errors(v.validate(data)), (field, value))
        for act in v.DANGER:
            for flag in (None, False, 1):
                data = clean()
                c = data['example']['pages']['search']['controls'][0]
                c.update(act=act, irreversible=flag)
                self.assertTrue(v.has_errors(v.validate(data)), (act, flag))
                c['irreversible'] = True
                self.assertFalse(v.has_errors(v.validate(data)))
        data = clean()
        c = data['example']['pages']['search']['controls'][0]
        c.update(name='', unnamed=True, css='#search')
        self.assertFalse(v.has_errors(v.validate(data)))

    def test_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'map.json'
            for text in ('{"x":{},"x":{}}', '{"x":{"pages":{"a":{},"a":{}}}}',
                         '{"x":{"go":{"a":"x","a":"y"}}}'):
                path.write_text(text, encoding='utf-8')
                _, findings = v.read_map(path)
                self.assertIn('duplicate key', findings[0]['msg'])
                self.assertTrue(findings[0]['path'])
        data = clean()
        controls = data['example']['pages']['search']['controls']
        controls[1] = copy.deepcopy(controls[0])
        findings = v.validate(data)
        self.assertFalse(v.has_errors(findings))
        self.assertTrue(any('duplicate control' in f['msg'] for f in findings))
        controls[1]['dynamic'] = True
        self.assertFalse(any('duplicate control' in f['msg'] for f in v.validate(data)))

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'map.json'
            for text in ('{', '[]', 'null', '{"x": NaN}'):
                path.write_text(text, encoding='utf-8')
                self.assertTrue(v.has_errors(v.validate_file(path)[1]))
            path.write_bytes(b'\xff')
            self.assertTrue(v.has_errors(v.validate_file(path)[1]))

    def test_traces(self):
        values = [r'C:\Users\someone', '/home/someone', '/Users/someone', 'someone@example.com',
                  '010-1234-5678', '0111234567', '900101-1234567', 'a1' * 20, 'sk-exampleSecret',
                  'ghp_exampleSecret', *v.WORDS]
        for value in values:
            data = clean()
            data['example']['pages']['search']['traps'] = [value]
            findings = v.validate(data)
            self.assertTrue(v.has_errors(findings), value)
            self.assertIn('traps[0]', findings[0]['path'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.trace_words'
            path.write_text('# comment\nfictional-test-word # comment\n', encoding='utf-8')
            data = clean()
            data['example']['label'] = 'FICTIONAL-TEST-WORD'
            self.assertTrue(v.has_errors(v.validate(data, v.trace_words(path))))
            self.assertEqual(v.trace_words(Path(tmp) / 'missing'), v.WORDS)
        data = clean()
        data['example']['go']['search'] = 'https://example.com/?q={' + 'x' * 40 + '}'
        self.assertFalse(v.has_errors(v.validate(data)))
        # handler names and API paths are not keys; URL ids are not phone numbers
        for value in ('see selectProtSystProtTrgtPrdctSrchList.do', 'apis.data.go.kr/B553457/nopenapi/rest/publicperformancedisplays/',
                      'https://example.com/page?pageId=0101010000', 'https://investor.example.com/home/default.aspx'):
            data = clean()
            data['example']['pages']['search']['traps'] = [value]
            self.assertFalse(v.has_errors(v.validate(data)), value)

    def test_warnings(self):
        for css in ('.css-1x2y3z', '.sc-AbCdE', '._a1b2c3', '[class*="css-1x2y3z"]'):
            data = clean()
            data['example']['pages']['search']['controls'][0]['css'] = css
            self.assertTrue(any('hashed' in f['msg'] for f in v.validate(data)), css)
        for css in ('#search', '[name="query"]', '[data-testid="search"]', '[aria-label="Search"]', 'button'):
            data = clean()
            data['example']['pages']['search']['controls'][0]['css'] = css
            self.assertEqual(v.validate(data), [])
        for name, dynamic in [('123건', False), ('12 results', False), ('2026-10-26', False), ('Search', True)]:
            data = clean()
            data['example']['pages']['search']['controls'][0].update(name=name, dynamic=dynamic)
            self.assertTrue(any('looks variable' in f['msg'] for f in v.validate(data)))
        data = clean()
        data['example']['origin'] = 'observed'
        page = data['example']['pages']['search']
        page['unknown'] = True
        page['controls'][0]['unknown'] = True
        page['controls'][0]['verified'] = '2026-02-30'
        page['controls'] = page['controls'][:1]
        findings = v.validate(data)
        self.assertEqual(len(findings), 5)
        self.assertFalse(v.has_errors(findings))
        page['controls'] *= 21
        self.assertTrue(any('more than 20' in f['msg'] for f in v.validate(data)))

    def test_malformed_shapes(self):
        for obj in (None, [], {'example': None}, {'example': {'pages': {'x': None}}}):
            self.assertTrue(v.has_errors(v.validate(obj)))
        for value in ([], {}, None, 3):
            data = clean()
            data['example']['pages']['search']['controls'][0].update(role=value, act=value, name=value)
            self.assertTrue(v.has_errors(v.validate(data)))


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'sites.json'
        self.source = Path(self.tmp.name) / 'incoming.json'
        self.base = clean()
        self.write(self.target, self.base)
        self.original = self.target.read_bytes()

    def write(self, path, data):
        path.write_bytes((json.dumps(data, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))

    def test_add_and_roundtrip(self):
        incoming = {'second': clean('second.test')['example'], '_comment': 'ignored'}
        incoming['second']['label'] = '\uac80\uc0c9'
        self.write(self.source, incoming)
        report = v.merge(self.source, self.target)
        self.assertTrue(report['written'])
        self.assertEqual(report['added'], ['second'])
        self.assertTrue(report['format']['round_trip'])
        raw = self.target.read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        data = json.loads(raw)
        self.assertEqual(list(data), ['example', 'second'])
        self.assertEqual(data['example'], self.base['example'])
        self.assertEqual(raw, (json.dumps(data, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))

    def test_skip_replace_and_rename(self):
        incoming = clean()
        incoming['example']['pages']['search']['controls'][0]['name'] = 'Find'
        self.write(self.source, incoming)
        report = v.merge(self.source, self.target)
        self.assertFalse(report['written'])
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertTrue(report['conflicts'])
        report = v.merge(self.source, self.target, replace=['example'])
        self.assertEqual(report['replaced'], ['example'])
        conflict = report['conflicts'][0]
        self.assertEqual(conflict['kind'], 'same control, different name')
        self.assertEqual(conflict['old']['name'], 'Search')
        self.assertEqual(conflict['new']['name'], 'Find')

    def test_dry_run_overlap_and_format(self):
        self.write(self.source, {'second': clean('sub.example.com')['example']})
        report = v.merge(self.source, self.target, dry_run=True)
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(report['conflicts'][0]['kind'], 'host overlap')
        self.target.write_text(json.dumps(self.base), encoding='utf-8')
        report = v.merge(self.source, self.target, dry_run=True)
        self.assertFalse(report['format']['round_trip'])
        self.assertIn('closest stable', report['format']['note'])

    def test_invalid_input_and_merged(self):
        self.source.write_text('{"invalid":{}}', encoding='utf-8')
        report = v.merge(self.source, self.target)
        self.assertTrue(v.has_errors(report['findings']))
        self.assertEqual(self.target.read_bytes(), self.original)
        self.write(self.source, {'second': clean('second.test')['example']})
        self.write(self.target, {'bad': {}})
        original = self.target.read_bytes()
        self.assertTrue(v.has_errors(v.merge(self.source, self.target)['findings']))
        self.assertEqual(self.target.read_bytes(), original)

    def test_crlf_and_css_rename(self):
        self.target.write_bytes(self.original.replace(b'\n', b'\r\n'))
        incoming = clean()
        old = self.base['example']['pages']['search']['controls'][0]
        old['css'] = '#search'
        self.write(self.target, self.base)
        self.target.write_bytes(self.target.read_bytes().replace(b'\n', b'\r\n'))
        new = incoming['example']['pages']['search']['controls'][0]
        new.update(css='#search', name='Find', where='new position')
        self.write(self.source, incoming)
        report = v.merge(self.source, self.target, replace=['example'])
        self.assertTrue(report['format']['round_trip'])
        self.assertTrue(report['conflicts'])
        self.assertIn(b'\r\n', self.target.read_bytes())

    def test_replace_malformed_existing(self):
        self.write(self.target, {'example': None})
        self.write(self.source, clean())
        report = v.merge(self.source, self.target, replace=['example'])
        self.assertFalse(v.has_errors(report['findings']))
        self.assertTrue(report['written'])

    def test_url_placeholder_spaces(self):
        data = clean()
        data['example']['pages']['search']['url'] = 'https://example.com/{event slug}/'
        self.assertFalse(v.has_errors(v.validate(data)))

    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['atlas', 'validate', str(self.target), '--json']), 0)
        self.assertEqual(json.loads(out.getvalue())['errors'], 0)
        self.write(self.source, {'second': clean('second.test')['example']})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['atlas', 'merge', str(self.source), '--into', str(self.target), '--dry-run', '--json']), 0)
        self.assertFalse(json.loads(out.getvalue())['written'])
        with patch('browser_atlas._atlas.map_path', return_value=self.target), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['atlas', 'validate']), 0)
        self.source.write_text('[]', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['atlas', 'validate', str(self.source)]), 1)
            self.assertEqual(main(['atlas', 'merge', str(self.source), '--into', str(self.target)]), 1)


if __name__ == '__main__':
    unittest.main()
