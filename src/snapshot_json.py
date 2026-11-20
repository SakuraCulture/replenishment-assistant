"""Canonical snapshot JSON, shared by streaming and persisted codecs."""
import json
from datetime import date, datetime


def encode_default(value):
    if isinstance(value,(datetime,date)):return {'__workbook_datetime__':value.isoformat(),'date_only':not isinstance(value,datetime)}
    raise TypeError('无法保存的输入类型：'+type(value).__name__)
def decode_object(d):
    if set(d)=={'__workbook_datetime__','date_only'}:return (date if d['date_only'] else datetime).fromisoformat(d['__workbook_datetime__'])
    return d
def dumps(data):return json.dumps(data,ensure_ascii=False,sort_keys=True,allow_nan=False,default=encode_default)
def loads(data):return json.loads(data,object_hook=decode_object)
