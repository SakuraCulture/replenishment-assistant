import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from portable_assets import resolve
import input_adapter

class PortableTests(unittest.TestCase):
    def test_missing_original_resolves_owned_copy_but_existing_user_file_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old=root/'old'/'original.xlsx';asset=root/'settings'/'migration-assets'/'hash'/'original.xlsx';asset.parent.mkdir(parents=True);asset.write_bytes(b'frozen-bytes')
            marker=asset.parents[2]/'迁入原文件映射.json';marker.write_text(json.dumps({'version':1,'paths':{str(old):asset.relative_to(marker.parent).as_posix()},'contents':{}}),'utf-8')
            with patch.dict(os.environ,{'REPLENISHMENT_PORTABLE_PATHS':str(marker)}):
                self.assertEqual(resolve(old),asset);self.assertEqual(input_adapter.digest(old),hashlib.sha256(b'frozen-bytes').hexdigest())
                old.parent.mkdir();old.write_bytes(b'fresh-user-selection');self.assertEqual(resolve(old),old)
                self.assertEqual(input_adapter.digest(old),hashlib.sha256(b'fresh-user-selection').hexdigest())
    def test_escape_is_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);marker=root/'map.json';old=root/'absent.xlsx';marker.write_text(json.dumps({'version':1,'paths':{str(old):'../secret.xlsx'},'contents':{}}),'utf-8')
            with self.assertRaisesRegex(ValueError,'越出'):resolve(old,mapping=marker)
