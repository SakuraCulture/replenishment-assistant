"""Lazy full-review JSON with the original encoder and validation contract."""
import json
import work_repository as wr
from runtime_support import checkpoint

class Prepared:
    def __init__(self, value, *, canonical=False, cancel=None):
        self.value=value
        self.encoder=(json.JSONEncoder(ensure_ascii=False,sort_keys=True,allow_nan=False,default=wr.encode_default)
                      if canonical else json.JSONEncoder(ensure_ascii=False,indent=2))
        # Preserve the original prepare-time rejection of unsupported values,
        # without retaining a second complete JSON string alongside the tree.
        for n,_ in enumerate(self.encoder.iterencode(value)):
            if n%256==0:checkpoint(cancel)
        checkpoint(cancel)

    def write(self, stream):
        # The process exclusively owns this frozen review for the session.
        # A new preparation invalidates it before receiving the next payload.
        for chunk in self.encoder.iterencode(self.value):stream.write(chunk)

