"""Application defaults, separate from the immutable model's algorithm defaults.

These are existing values, not new recommendations. Each config load returns a fresh
dictionary. Import aliases preserve the pre-refactor public engine/wc constants.
"""
import json
from runtime_support import resource_file

DEFAULT_WAREHOUSES={'河北仓':(8.0,4.0),'山东仓':(8.0,5.0)}
WAREHOUSES=tuple(DEFAULT_WAREHOUSES)

def load_default_config():
    return json.loads(resource_file('default_config.json').read_text(encoding='utf-8-sig'))

def supply_route_choices():
    a,b=WAREHOUSES
    return [('仅'+a,{'primary':a,'secondary':None}),('仅'+b,{'primary':b,'secondary':None}),
            (a[:-1]+'主仓 → '+b[:-1]+'从仓',{'primary':a,'secondary':b}),
            (b[:-1]+'主仓 → '+a[:-1]+'从仓',{'primary':b,'secondary':a})]
