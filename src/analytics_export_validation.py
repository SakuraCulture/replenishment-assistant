"""Export-local reuse of a complete analysis check, with no retained read lock.

The public full validator remains the fallback. Main-DB changes, replaced files,
changed query/identity, or changed external purchase facts discard this proof.
No report or proof is persisted, shared across exports, or used for a write.
"""
import sqlite3
from pathlib import Path

import order_analytics as analytics
from analytics_identifiers import token
from runtime_support import checkpoint


def _file_id(path):
    stat=path.stat()
    # An unavailable filesystem identity is not evidence of an unchanged file.
    if not stat.st_ino:return None
    return stat.st_dev,stat.st_ino


class SnapshotCheck:
    def __init__(self,path,report,progress=lambda _:None,cancel=None,*,temp_folder=None):
        self.path=Path(path).resolve();self.report=report;self.progress=progress;self.cancel=cancel
        self.db=None;self.identity=None;self.proof=None
        self.temp_folder=temp_folder

    def __enter__(self):
        checkpoint(self.cancel)
        try:before=_file_id(self.path)
        except OSError:before=None
        try:self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=0)
        except sqlite3.Error:return self
        try:
            if before==_file_id(self.path):self.identity=before
        except OSError:
            pass  # The full validator still decides whether the live path is valid.
        return self

    def __exit__(self,*_):
        self.proof=None
        if self.db is not None:self.db.close();self.db=None

    def _fence(self):
        try:
            if self.identity is None or _file_id(self.path)!=self.identity:return None
            files=[]
            for path in (self.path,Path(str(self.path)+'-wal'),Path(str(self.path)+'-journal')):
                try:
                    stat=path.stat();files.append((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns))
                except FileNotFoundError:files.append(None)
            cursor=self.db.execute('PRAGMA data_version')
            try:version=cursor.fetchone()[0]
            finally:cursor.close()
            context=analytics.digest([self.report['schema'],self.report['query'],self.report['snapshot_id'],
                                      self.report['heads'],self.report.get('identifier_token')])
            return version,files,context
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            return None

    def __call__(self):
        checkpoint(self.cancel);before=self._fence()
        if self.proof is not None and before==self.proof:
            try:
                current=token(self.path,self.db,self.report['heads'],self.cancel)
                if current==self.report['identifier_token'] and self._fence()==before:
                    checkpoint(self.cancel);return True
            except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
                pass  # Never substitute an uncertain shortcut for the full check.
        self.proof=None
        result=analytics.validate_snapshot(self.path,self.report,self.progress,self.cancel,temp_folder=self.temp_folder)
        after=self._fence()
        if result is True and before is not None and before==after and isinstance(self.report.get('identifier_token'),str):
            self.proof=after
        return result
