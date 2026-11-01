"""Release bundle identity and exclusion checks without browser or user data."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
import aoxiang_bundle as bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.dependencies, self.dest = [self.root/p for p in ('source', 'dependencies', 'bundle')]
        for name in ('src/machine-entry.mjs', 'assets/contract.json', 'package.json', 'package-lock.json', 'NOTICE.txt'):
            p = self.source/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{}', encoding='utf-8')
        files = {}
        for name in ('runtime/node.exe', 'runtime/LICENSE.txt', 'node_modules/module.js'):
            p = self.dependencies/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'locked bytes')
            files[name] = bundle.sha(p)
        lock = {'format':1, 'upstream_version':'0.2.1', 'package_lock_sha256':bundle.sha(self.source/'package-lock.json'), 'files':files}
        (self.source/'runtime-lock.json').write_text(json.dumps(lock), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def stage(self):
        return bundle.stage(self.source, self.dependencies, self.dest, source_commit='a'*40, app_version='0.24.0')

    def test_only_committed_sources_and_locked_dependencies_no_user_files(self):
        (self.source/'config').mkdir()
        (self.source/'config/machine.binding.json').write_text('private', encoding='utf-8')
        (self.dependencies/'state.sqlite').write_bytes(b'private')
        result = self.stage()
        manifest = bundle.verify(self.dest)
        self.assertFalse((self.dest/'config').exists())
        self.assertFalse((self.dest/'state.sqlite').exists())
        self.assertFalse((self.dest/'.dev-checkout').exists())
        self.assertEqual(result['source_commit'], manifest['source_commit'])
        self.assertEqual((self.dest/'runtime/node.exe').read_bytes(), b'locked bytes')
        with self.assertRaises(FileExistsError):
            self.stage()

    def test_changed_or_unlocked_runtime_never_packaged(self):
        (self.dependencies/'node_modules/extra.js').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'file set'):
            self.stage()
        self.assertFalse(self.dest.exists())
        (self.dependencies/'node_modules/extra.js').unlink()
        (self.dependencies/'runtime/node.exe').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'runtime changed'):
            self.stage()
        self.assertFalse(self.dest.exists())

    def test_package_verification_detects_changes_extra_and_missing_files(self):
        self.stage()
        p = self.dest/'src/machine-entry.mjs'
        p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            bundle.verify(self.dest)
        p.write_bytes(b'{}')
        extra = self.dest/'unexpected.txt'
        extra.write_bytes(b'new')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            bundle.verify(self.dest)
        extra.unlink()
        p.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            bundle.verify(self.dest)

    def test_dependency_lock_paths_and_declarations_cannot_escape(self):
        p = self.source/'runtime-lock.json'
        value = json.loads(p.read_text(encoding='utf-8'))
        value['files']['../private'] = 'a'*64
        p.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            self.stage()
        del value['files']['../private']
        p.write_text(json.dumps(value), encoding='utf-8')
        (self.source/'package-lock.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'declarations changed'):
            self.stage()


if __name__ == '__main__':
    unittest.main()
