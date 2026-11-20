"""Evidence captured by payment recognition and rechecked during document save.

These types describe the existing plan keys. They do not transform or fill
missing fields on recovered plans. Lifecycle document versions and upstream
tokens belong to the lifecycle plan, not the signing identity below.
"""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import re
from typing import NotRequired, TypedDict
import unicodedata

from purchase_io import number
from snapshot_json import dumps

PREFIX = 'payment_match.'
HEAD_KEYS = ('oid', 'revision', 'warehouse', 'number', 'store', 'digest')


class SignedWarehouseIdentity(TypedDict):
    """Identity of the signed warehouse order used by this payment.

    revision is its source signing revision. A later parent revision or a
    separate warehouse execution revision is not interchangeable with it.
    digest identifies the signed snapshot. number is the business warehouse
    order number and store is the store code.
    """

    oid: str
    revision: int
    warehouse: str
    number: str
    store: str
    digest: str


class ShopNameContext(TypedDict):
    """Name narrowing used at recognition. pairs records [store, warehouse]."""

    basis: str
    configured: 'ShopNameEvidence'
    pairs: list[list[str]]
    identities: list[SignedWarehouseIdentity]


class ShopNameEvidence(TypedDict):
    """Normalized name and the configured rows considered at recognition."""

    name: str
    pairs: list[list[str]]
    token: str


class MatchRequest(TypedDict):
    """One recognized payment source and its selected signed warehouse order.

    Source scope is a verified company ID or None if the company is unknown.
    A missing generation ID means no applicable allocation snapshot was found.
    A None signature means the source facts are incomplete.
    """

    internal: str
    source_scope: str | None
    original_name: str
    remember: bool
    automatic_proof: NotRequired[str | None]
    name_context: NotRequired[ShopNameContext | None]
    manual_selection: NotRequired[bool]
    explicit_identity: NotRequired[bool]
    quantity_signature: str | None
    facts_signature: NotRequired[str | None]
    identity_constraints: NotRequired[list[dict[str, str | None]]]
    head: SignedWarehouseIdentity
    generation_id: str | None
    name_evidence: NotRequired[ShopNameEvidence]


class MatchScope(TypedDict):
    """Versioned digests of relevant candidates and prior source relations."""

    version: int
    heads: str
    mappings: str


class PaymentMatchEvidence(TypedDict):
    """Recognition sidecar, persisted in recovery plans with its own checksum.

    pool_token covers current order/revision visibility, active generation
    identity and digest, and stage versions (including payment version).
    mapping_token covers saved relations and accounts_token verified sources.
    Legacy plans may lack the scoped fallback and still use the original
    whole-pool comparison.
    """

    pool_token: str
    mapping_token: str
    accounts_token: str
    requests: list[MatchRequest]
    scope_evidence: NotRequired[MatchScope]


def checksum(value):return hashlib.sha256(dumps(value).encode()).hexdigest()


def normalized(value):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value or '')))


def mapping_key(kind,scope,value):return PREFIX+kind+'.'+checksum([scope,str(value)])


def facts(rows):
    values=[];seen=set();complete=True
    for row in rows:
        sku=str(row['sku']);q=row.get('base_quantity',row.get('quantity'));a=row.get('amount')
        q=number(q,True);a=number(a,True)
        if sku in seen:complete=False
        seen.add(sku);complete=complete and q is not None and a is not None
        values.append([sku,q,a])
    return {'signature':checksum(sorted(values)) if complete else None,
            'quantity_signature':checksum(sorted([sku,q] for sku,q,_ in values)) if complete else None,'count':len(values),
            'quantity':str(sum((Decimal(r[1]) for r in values),Decimal(0))) if all(r[1] is not None for r in values) else None,
            'amount':str(sum((Decimal(r[2]) for r in values),Decimal(0))) if all(r[2] is not None for r in values) else None,
            'complete':complete}


def source_day(rows):
    days=set()
    for row in rows:
        value=(row.get('source_order') or {}).get('下单时间')
        if isinstance(value,datetime):days.add(value.date().isoformat())
        elif isinstance(value,date):days.add(value.isoformat())
        elif isinstance(value,str):
            try:days.add(date.fromisoformat(value.strip()[:10]).isoformat())
            except ValueError:pass
    return next(iter(days)) if len(days)==1 else None


def related(rows,head):
    return all(not r.get(field) or r[field]==head[target] for r in rows for field,target in [('signed_number','number'),('store','store'),('warehouse','warehouse')])


def same_head(a,b):return all(a.get(k)==b.get(k) for k in HEAD_KEYS)


def local_key(internal,name):return mapping_key('document',None,checksum([str(internal),normalized(name)]))
