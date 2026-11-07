"""Read only saved parameter-page supply routes; never initialize the business DB."""
from contextlib import closing
import json
import sqlite3
from stockout_common import WAREHOUSES, digest


def supply_snapshot(folder, stores):
    path = folder / 'workbench.sqlite3'
    codes = sorted({s['code'] for s in stores})
    records = {}
    if path.exists() and codes:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA query_only=ON')
            fields = ','.join('?' for _ in codes)
            for code, raw in db.execute(f'SELECT code,json_extract(data,\'$.ui\') FROM stores WHERE code IN ({fields})', codes):
                ui = json.loads(raw) if raw else {}
                records[code] = {k: ui.get(k) for k in ('primary', 'secondary')}
    routes, issues = {}, {}
    for code in codes:
        route = records.get(code, {})
        primary, secondary = route.get('primary'), route.get('secondary')
        if not primary:
            issues[code] = '参数设置中尚未保存本店主仓；请保存后重新采用'
        elif primary not in WAREHOUSES or secondary not in (None, *WAREHOUSES) or secondary == primary:
            issues[code] = '参数设置中的主从仓无效，请核对后重新采用'
        else:
            routes[code] = route
    return {'routes': routes, 'route_issues': issues,
            'route_source': {'source': '参数设置 / 已保存门店配置', 'database': str(path),
                             'digest': digest(records)}}


def route_label(route):
    if not route:
        return '供货关系未配置'
    return (route['primary'] + '主仓 → ' + route['secondary'] + '从仓') if route.get('secondary') else route['primary'] + '单仓'
