"""Lazy full-review JSON with the original encoder and validation contract."""
import json
import pickle
import tempfile
import work_repository as wr
from runtime_support import checkpoint
from draft_spool import temporary_root

class Prepared:
    def __init__(self, value, *, canonical=False, cancel=None, series=False):
        self.value=value
        self.series=series
        self.encoder=(json.JSONEncoder(ensure_ascii=False,sort_keys=True,allow_nan=False,default=wr.encode_default)
                      if canonical else json.JSONEncoder(ensure_ascii=False,indent=2))
        # Preserve the original prepare-time rejection of unsupported values,
        # without retaining a second complete JSON string alongside the tree.
        for n,_ in enumerate(self.tokens()):
            if n%256==0:checkpoint(cancel)
        checkpoint(cancel)

    def write(self, stream):
        # The process exclusively owns this frozen review for the session.
        # A new preparation invalidates it before receiving the next payload.
        for chunk in self.tokens():stream.write(chunk)

    def tokens(self):
        if not self.series:
            yield from self.encoder.iterencode(self.value)
            return
        yield '['
        for i,record in enumerate(self.value):
            if i:yield ', '
            yield from self.encoder.iterencode(record)
        yield ']'


class Records:
    """Repeatable review rows with at most one decoded store at a time."""
    def __init__(self, database):
        self.file=tempfile.TemporaryFile(dir=temporary_root(database),prefix='draft-review-',suffix='.tmp')
        self.count=0

    def append(self, record):
        pickle.dump(record,self.file,protocol=5)
        self.count+=1

    def __iter__(self):
        self.file.seek(0)
        for _ in range(self.count):yield pickle.load(self.file)

    def close(self):
        self.file.close()
