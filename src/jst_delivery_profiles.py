"""Local warehouse-specific buyer and delivery profile maintenance."""
from contextlib import closing
from pathlib import Path

from aoxiang_directory import load_for_settings
from app_defaults import WAREHOUSES
import jst_auto_order_store as store
from jst_auto_order_template import CONTACT, cell_length
from runtime_support import checkpoint
import work_repository as wr


ADDRESS_LIMIT = 500
TEXT_LIMIT = 200


def clean_profile(warehouse, values):
    if warehouse not in WAREHOUSES or set(values)!=set(CONTACT):
        raise ValueError('请选择有效的供货仓并核对配送资料字段')
    clean={}
    for field,value in values.items():
        title=CONTACT[field][1]
        if not isinstance(value,str):
            raise ValueError(title+'应为单行文本')
        value=value.strip()
        if any(ord(c)<32 for c in value):
            raise ValueError(title+'中包含换行、制表符或其他控制字符，请保留一行内容')
        if cell_length(value)>(ADDRESS_LIMIT if field=='address' else TEXT_LIMIT):
            raise ValueError(title+'内容过长')
        clean[field]=value
    return clean


def save_profile(path, code, warehouse, values, expected, progress=lambda _:None, cancel=None):
    return store.save_profile(path,code,warehouse,clean_profile(warehouse,values),expected,cancel)


def directory(path, known, progress=lambda _:None, cancel=None):
    names=dict(known)
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        records=store.profiles(db)
        if store.exists(db,'stores'):
            for code,raw in db.execute('SELECT code,data FROM stores'):
                checkpoint(cancel)
                if not names.get(code):
                    names[code]=wr.loads(raw).get('name','')
        if store.exists(db,'normal_orders'):
            for (code,) in db.execute('SELECT DISTINCT store FROM normal_orders'):
                checkpoint(cancel)
                names.setdefault(code,'')
    warning=''
    try:
        for item in (load_for_settings(Path(path).parent) or {}).get('items',[]):
            checkpoint(cancel)
            if not names.get(item['code']):
                names[item['code']]=item['name']
    except (OSError, ValueError, KeyError, TypeError):
        warning='本机门店目录未能读取，已列出本机订单及门店配置中的编码。'
    for code,warehouse in records:
        if warehouse in WAREHOUSES:
            names.setdefault(code,'')
    checkpoint(cancel)
    return {'names':names,'records':records,'warning':warning}
