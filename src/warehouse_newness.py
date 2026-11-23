"""First inbound evidence and the Beijing-day warehouse new-product window."""
from datetime import date, datetime, time
import re

from admission import tags

FIRST_INBOUND = '首次入库时间'
CREATED = '商品创建日期'
VIRTUAL_CATEGORY = '虚拟分类'
AGE_LIMIT = 45
LEGACY_RULE = 'warehouse-first-inbound-45d-v1'
SCREENED_RULE = 'warehouse-first-inbound-45d-v2'
RULE = 'warehouse-first-inbound-45d-v3'
MISSING_COLUMN = '该版本未提供首次入库日期'
MISSING_VIRTUAL_CATEGORY = '仓资料未留存虚拟分类，请刷新或重新导入全商品档案'
RETIRED = '仓资料虚拟分类为淘汰品'
DISABLED = '仓商品资料禁用'
STORE_CONSUMABLES = '仓商品分类为门店耗材'
UNKNOWN = '首入库日期未知'
INVALID = '首入库日期无效'
FUTURE = '首入库日期晚于判定日'
EARLY = '首次入库早于商品建档日期'


def blank(value):
    return value is None or isinstance(value, str) and not value.strip()


def business_date(value):
    """Read the calendar day without converting an Excel value to UTC."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    token = value.strip()
    match = re.fullmatch(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?)?', token)
    if not match:
        return None
    y, m, d, hour, minute, second = match.groups()
    try:
        if hour is not None:
            time(int(hour), int(minute), int(second or 0))
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def json_cell(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def available(catalog):
    return catalog.get('kind') == 'jst_full_archive' and catalog.get('first_inbound_column') is True


def exclusion(status, virtual_category, *, category=None):
    reasons = []
    if str(status or '').strip() == '禁用':
        reasons.append(DISABLED)
    if any('淘汰' in tag for tag in tags(virtual_category)):
        reasons.append(RETIRED)
    if str(category or '').strip() == '门店耗材':
        reasons.append(STORE_CONSUMABLES)
    return '，'.join(reasons)


def source(catalog, item):
    if not available(catalog) or item is None:
        return None
    return {'warehouse': catalog['warehouse'], 'catalog_revision': catalog['revision'],
            'raw': item.get(FIRST_INBOUND), 'created': item.get(CREATED),
            'status': item.get('商品状态'), 'category': item.get('分类'),
            **({'virtual_category': item[VIRTUAL_CATEGORY]} if VIRTUAL_CATEGORY in item else {})}


def decide(evidence, day):
    day = date.fromisoformat(day)
    first = business_date(evidence['raw'])
    created = business_date(evidence.get('created'))
    age = (day - first).days if first is not None else None
    warning = EARLY if first and created and first < created else ''
    issue = (UNKNOWN if blank(evidence['raw']) else INVALID if first is None else
             FUTURE if age < 0 else '')
    excluded_reason = exclusion(evidence.get('status'), evidence.get('virtual_category'), category=evidence.get('category'))
    screening_issue = MISSING_VIRTUAL_CATEGORY if 'virtual_category' not in evidence else ''
    return {**evidence, 'rule': RULE, 'day': day.isoformat(), 'first_date': first.isoformat() if first else None,
            'age': age, 'new': not (issue or excluded_reason or screening_issue) and age <= AGE_LIMIT,
            'issue': issue, 'warning': warning, 'excluded_reason': excluded_reason,
            'screening_issue': screening_issue}


def for_row(row, day=None):
    meta = row.get('_meta', {})
    basis = meta.get('warehouse_newness')
    if (basis and basis.get('rule') in (LEGACY_RULE, SCREENED_RULE, RULE) and row.get('_source') == '资料匹配'
            and basis['warehouse'] == row.get('AU') and basis['catalog_revision'] == meta.get('product_revision')
            and (day is None or basis['day'] == day)):
        return basis
    return None


def row_screening_reason(row):
    basis = for_row(row)
    if basis is not None:
        return basis.get('excluded_reason') or basis.get('screening_issue', '')
    return row.get('_meta', {}).get('new_product_exclusion', '')


def view(catalog, day):
    """The current workbench view and calculations apply exactly the same rule."""
    if not available(catalog):
        return None
    return [(item, decide(source(catalog, item), day)) for item in catalog['items'].values()]
