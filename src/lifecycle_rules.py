"""Ordered reconciliation of immutable document facts; no model calculation."""
import copy
from decimal import Decimal

RULE = 'sequential-zero-v1'
LABELS = {'value':'已提供', 'zero':'明确填0', 'blank':'空白按0', 'missing':'缺行按0', 'extra':'清单外商品'}

def positive(rows):
    return {r['sku']:{'sku':r['sku'],'name':r['name'],'quantity':r['base_quantity']} for r in rows if r.get('base_quantity') is not None and Decimal(str(r['base_quantity']))>0}

def reconcile(raw_rows, products, pending=False):
    """Predecessor order first, source-only rows last. Missing source != invented row."""
    original={r['sku']:r for r in raw_rows}
    keys=list(original) if pending else [*products,*[s for s in original if s not in products]]
    rows=[]
    for sku in keys:
        source=original.get(sku); before=products.get(sku,{}).get('quantity')
        if source is None:
            row={'sku':sku,'name':products[sku].get('name'),'quantity':None,'unit':'库存单位','factor':None,'conversion_basis':None,'base_quantity':'0','amount':None,'currency':'','document_number':raw_rows[0]['document_number'] if raw_rows else '', 'document_date':None,'note':None,'sources':[],'raw_headers':[],'raw_values':[]}
            origin='missing'
        else:
            row=copy.deepcopy(source)
            if row.get('base_quantity') is None:
                row['base_quantity']='0'; origin='blank'
            else:origin='zero' if Decimal(str(row['base_quantity']))==0 else 'value'
        row['quantity_origin']=origin
        row['upstream_quantity']=None if before is None else str(before)
        row['difference']=None if before is None else str(Decimal(str(row['base_quantity']))-Decimal(str(before)))
        row['outside_basis']=not pending and sku not in products
        row['processing_note']=LABELS[origin]+('；清单外商品' if row['outside_basis'] else '')+('；上游未就绪，仅留存' if pending else '')
        rows.append(row)
    return rows

def basis_token(doc):
    return {'version':doc['version'],'digest':doc['digest']} if doc else None

def same_basis(a,b):
    return a==b
