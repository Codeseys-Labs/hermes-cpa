"""Notice-generation tests for scripts/build_console.py; no Bun, network or build output.

Run with the qualification interpreter:
  python -m unittest discover -s qualification -p 'test_build_console.py' -v
Fixtures are generated in temporary directories; the repository tests only read committed inputs.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_console.py'
SPEC = importlib.util.spec_from_file_location('build_console', SCRIPT)
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.console = self.root / 'console'
        self.supplements = self.root / 'supplements'
        self.supplements.mkdir()
        self.set_supplements([])
        for name, path in (('CONSOLE', self.console), ('SUPPLEMENTS', self.supplements)):
            patcher = patch.object(b, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_supplements(self, items):
        (self.supplements / 'index.json').write_text(json.dumps({'supplements': items}))

    def supplement(self, package, text, identifier='MIT'):
        (self.supplements / f'{package}.LICENSE').write_text(text)
        return {'package': package, 'version': '1.0.0', 'identifier': identifier,
                'file': f'{package}.LICENSE', 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                'source': 'https://example.invalid/LICENSE', 'note': 'Reviewed fixture.'}

    def install(self, name, license='MIT', files=None):
        directory = self.console / 'node_modules' / name
        directory.mkdir(parents=True)
        manifest = {'name': name, 'version': '1.0.0'}
        if license:
            manifest['license'] = license
        (directory / 'package.json').write_text(json.dumps(manifest))
        for filename, text in (files or {}).items():
            (directory / filename).write_text(text, encoding='utf-8')

    @staticmethod
    def entry(name, identifier='MIT', text=None):
        entry = {'name': name, 'version': '1.0.0'}
        if identifier:
            entry['identifier'] = identifier
        if text is not None:
            entry['text'] = text
        return entry

    def write_inventory(self, entries):
        dist = self.root / 'dist'
        (dist / '.vite').mkdir(parents=True, exist_ok=True)
        (dist / '.vite/license.json').write_text(json.dumps(entries))
        return dist


class CollectNotices(Fixture):
    def test_every_bundled_license_and_notice_is_published(self):
        self.install('alpha', files={'LICENSE': 'Alpha permission text\n'})
        self.install('beta', license='ISC', files={'LICENSE.md': 'Beta ISC text\n',
                                                   'NOTICE': 'Beta attribution notice\n'})
        inventory = [self.entry('alpha', text='Alpha permission text'),
                     self.entry('beta', identifier='ISC', text='Beta ISC text')]
        document = b.render_notices('# Head\n\n', 'Root license text', b.collect_notices(inventory)).decode()
        for expected in ('Root license text', 'Alpha permission text', 'Beta ISC text',
                         'Beta attribution notice', '- alpha@1.0.0: MIT', '- beta@1.0.0: ISC'):
            self.assertIn(expected, document)
        self.assertEqual(document.count(b.NOTICE_MARKER), 1)
        self.assertTrue(document.startswith('# Head\n\n' + b.NOTICE_MARKER))

    def test_license_text_missing_from_the_package_needs_a_matching_supplement(self):
        self.install('alpha')
        with self.assertRaisesRegex(RuntimeError, 'alpha@1.0.0 ships no license text'):
            b.collect_notices([self.entry('alpha')])
        self.set_supplements([self.supplement('alpha', 'Supplied MIT text\n')])
        document = b.render_notices('# Head\n', 'Root', b.collect_notices([self.entry('alpha')])).decode()
        self.assertIn('Supplied MIT text', document)
        self.assertIn('https://example.invalid/LICENSE', document)

    def test_supplement_with_wrong_digest_or_identifier_is_refused(self):
        self.install('alpha')
        item = self.supplement('alpha', 'Supplied MIT text\n')
        (self.supplements / 'alpha.LICENSE').write_text('Tampered text\n')
        self.set_supplements([item])
        with self.assertRaisesRegex(RuntimeError, 'recorded digest'):
            b.collect_notices([self.entry('alpha')])
        self.set_supplements([self.supplement('alpha', 'Supplied MIT text\n', identifier='ISC')])
        with self.assertRaisesRegex(RuntimeError, 'supplement states ISC'):
            b.collect_notices([self.entry('alpha')])

    def test_supplement_for_a_package_that_ships_its_text_is_stale(self):
        self.install('alpha', files={'LICENSE': 'Alpha permission text\n'})
        self.set_supplements([self.supplement('alpha', 'Supplied MIT text\n')])
        with self.assertRaisesRegex(RuntimeError, 'Stale license supplement for alpha@1.0.0'):
            b.collect_notices([self.entry('alpha', text='Alpha permission text')])

    def test_unreviewed_or_missing_identifier_is_refused(self):
        self.install('alpha', license='GPL-3.0-only', files={'LICENSE': 'Alpha text\n'})
        self.install('beta', license=None, files={'LICENSE': 'Beta text\n'})
        with self.assertRaisesRegex(RuntimeError, 'alpha@1.0.0 declares license GPL-3.0-only') as caught:
            b.collect_notices([self.entry('alpha', identifier='GPL-3.0-only', text='Alpha text'),
                               self.entry('beta', identifier=None, text='Beta text')])
        self.assertIn('beta@1.0.0 declares license none', str(caught.exception))

    def test_bundler_text_must_match_the_installed_package(self):
        self.install('alpha', files={'LICENSE': 'Alpha permission text\n'})
        with self.assertRaisesRegex(RuntimeError, 'matches no license file'):
            b.collect_notices([self.entry('alpha', text='Some other text')])

    def test_bundled_package_missing_from_node_modules_is_refused(self):
        (self.console / 'node_modules').mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'not installed'):
            b.collect_notices([self.entry('alpha', text='Alpha text')])


class Inventory(Fixture):
    def test_inventory_refuses_missing_required_license_entries(self):
        entries = [self.entry(name) for name in reversed(b.REQUIRED_BUNDLED)]
        with self.assertRaisesRegex(RuntimeError, 'lacks bundled packages axios'):
            b.bundled_packages(self.write_inventory([e for e in entries if e['name'] != 'axios']))

    def test_malformed_inventory_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'not a list'):
            b.bundled_packages(self.write_inventory({}))
        with self.assertRaisesRegex(RuntimeError, 'Malformed'):
            b.bundled_packages(self.write_inventory([{'name': 'alpha'}]))

    def test_build_only_packages_must_not_be_bundled(self):
        entries = [self.entry(name) for name in (*b.REQUIRED_BUNDLED, 'braces')]
        with self.assertRaisesRegex(RuntimeError, 'braces is bundled into the console'):
            b.bundled_packages(self.write_inventory(entries))

    def test_output_must_be_only_the_document_and_the_inventory(self):
        dist = self.write_inventory([])
        (dist / 'index.html').write_text('<html></html>')
        b.require_single_file(dist)
        (dist / 'index-abc123.js').write_text('export {}')
        with self.assertRaisesRegex(RuntimeError, 'exactly'):
            b.require_single_file(dist)


class InstallMatchesLock(Fixture):
    def lock(self, *entries):
        self.console.mkdir(parents=True, exist_ok=True)
        lines = ''.join(f'    "{key}": ["{spec}", "", {{}}, "sha512-fixture"],\n' for key, spec in entries)
        (self.console / 'bun.lock').write_text('{\n  "packages": {\n' + lines + '  }\n}\n')


    def test_install_that_drifted_from_the_lock_is_refused(self):
        self.install('alpha')
        self.lock(('alpha', 'alpha@1.1.0'))
        with self.assertRaisesRegex(RuntimeError, 'alpha: installed 1.0.0, locked 1.1.0'):
            b.require_install_matches_lock()


class NoticeInputs(Fixture):
    def test_generated_marker_splits_the_hand_written_head(self):
        package = self.root / 'package'
        package.mkdir()
        self.console.mkdir()
        (self.console / 'LICENSE').write_text('Root MIT\n')
        notices = package / 'THIRD_PARTY_NOTICES.md'
        notices.write_text('# Head\n\n' + b.NOTICE_MARKER + '\n\nold generated text\n')
        with patch.object(b, 'PACKAGE', package):
            self.assertEqual(b.notice_inputs(), ('# Head\n\n', 'Root MIT'))
            notices.write_text('# Head only\n')
            with self.assertRaisesRegex(RuntimeError, 'generated-section marker'):
                b.notice_inputs()




if __name__ == '__main__':
    unittest.main()
