import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_sync_bundle import ComponentIntegrityError, digest, verify_component
from catalog_sync_client import Client


class CatalogBundleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        components = self.root/'_internal'/'components'
        self.jst, self.ax = components/'jst_catalog', components/'aoxiang'
        self.entry = self.jst/'src/machine-entry.mjs'
        self.config = self.jst/'package.json'
        self.runtime = self.ax/'runtime/node.exe'
        for path in (self.entry, self.config, self.runtime):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic')
        (components.parent/'build_info.json').write_text('{"build_id":"rc-test","source_commit":"app-test"}', 'utf-8')
        self.path = self.jst/'integrity.manifest.json'
        self.manifest = {'schema': 1, 'source_commit': 'jst-test',
                         'files': {'src/machine-entry.mjs': digest(self.entry), 'package.json': digest(self.config)},
                         'shared': {'runtime/node.exe': digest(self.runtime)}}
        self.path.write_text(json.dumps(self.manifest), 'utf-8')

    def test_clean_manifest_then_report_all_mismatches(self):
        self.assertEqual(verify_component(self.jst, self.ax), self.manifest)
        self.entry.unlink()
        self.config.write_bytes(b'changed!!')
        self.runtime.unlink()
        (self.jst/'unlisted.mjs').write_bytes(b'not in manifest')
        with self.assertRaises(ComponentIntegrityError) as raised:
            verify_component(self.jst, self.ax)
        details = raised.exception.details
        for item in (str(self.root), 'rc-test', 'app-test', 'jst-test',
                     '缺失：聚水潭/src/machine-entry.mjs', '内容不符：聚水潭/package.json',
                     '多余：聚水潭/unlisted.mjs', '缺失：共用组件/runtime/node.exe'):
            self.assertIn(item, details)

    def test_missing_manifest_or_bad_manifest_is_explained(self):
        self.path.unlink()
        with self.assertRaises(ComponentIntegrityError) as raised:
            verify_component(self.jst, self.ax)
        self.assertIn('缺失：聚水潭/integrity.manifest.json', raised.exception.details)
        self.path.write_text('{"schema":1,"files":null}', 'utf-8')
        with self.assertRaises(ComponentIntegrityError) as raised:
            verify_component(self.jst, self.ax)
        self.assertIn('清单格式异常', raised.exception.details)

    def test_bad_package_does_not_start_component(self):
        self.entry.unlink()
        data = self.root/'settings'/'jst_catalog_sync'
        client = Client(data, component_roots=(self.jst, self.ax))
        with patch.object(sys, 'frozen', True, create=True), patch('catalog_sync_client.subprocess.Popen') as popen:
            with self.assertRaises(ComponentIntegrityError):
                client.start()
            popen.assert_not_called()
        self.assertFalse(data.exists())
