"""Lightweight static contracts only: no constructors, coercion or storage migration.

Business letters remain dictionaries. Optional fields reflect actual stage-specific
presence; None, zero, empty text and pending text are distinct, never defaulted here.
See docs/字段与数据契约.md for every model letter and precedence rules.
"""
from __future__ import annotations
from datetime import date,datetime,time,timedelta
from typing import Any,TypeAlias,TypedDict

Cell:TypeAlias=str|int|float|bool|None|date|datetime|time|timedelta
RawRecord:TypeAlias=dict[str,Cell]
ModelRow:TypeAlias=dict[str,Any]  # letters plus named diagnostic/audit/execution fields
WarehouseCycles:TypeAlias=dict[str,tuple[float,float]|list[float]]

class _RowMetadataRequired(TypedDict):
    export_row:int
    product_row:int|None
    match:str
    blocking:list[str]
    notes:list[str]
    category:Cell
    export_status:Cell
    export_updated:Cell

class RowMetadata(_RowMetadataRequired,total=False):
    product_tags:Cell
    product_tags_known:bool
    medical_sources:list[dict[str,Any]]
    admission:dict[str,Any]
    profile_override:Cell
    export_file:str
    export_sheet:str
    product_file:str
    product_sheet:str
    product_revision:str
    warehouse_available:Cell
    routing:dict[str,Any]

NormalizedRecord:TypeAlias=tuple[ModelRow,RowMetadata]

class _ParameterConfigRequired(TypedDict):
    parameters:dict[str,int|float]
    weight_profiles:dict[str,dict[str,int|float]]
    category_profiles:dict[str,str]
    warehouses:WarehouseCycles
    exception:bool

class ParameterConfig(_ParameterConfigRequired,total=False):
    profile_parameters:dict[str,dict[str,int|float]]
    restock_switches:dict[str,bool]

class _ParameterPresetRequired(TypedDict):
    id:str
    version:int
    name:str
    config:ParameterConfig
    builtin:bool

class ParameterPreset(_ParameterPresetRequired,total=False):
    base_config:ParameterConfig
    plan_source:dict[str,Any]

class _StoreSettingsRequired(TypedDict):
    warehouses:WarehouseCycles
    exception:bool
    budget:float|int|None

class StoreSettings(_StoreSettingsRequired,total=False):
    admission:dict[str,Any]
    supply_route:dict[str,str|None]
    parameter_config:ParameterConfig
    preset:dict[str,Any]
    parameter_signature:str

class _InputBatchRequired(TypedDict):
    normalized:list[NormalizedRecord]
    config:dict[str,Any]
    stores:dict[str,str]
    hashes:dict[str,str]
    warehouses:list[str]

class InputBatch(_InputBatchRequired,total=False):
    info:dict[str,Any]
    loaded_at:str
    intake:dict[str,Any]
    blocked:dict[str,int]
    catalogs:dict[str,dict[str,Any]]
    warehouse_rows:dict[str,list[NormalizedRecord]]
    sales:str|list[str]
    products:str|None

class _CalculationResultRequired(TypedDict):
    store:str
    store_name:str
    rows:list[ModelRow]
    baseline:list[ModelRow]
    config:dict[str,Any]
    warehouses:WarehouseCycles
    enabled:bool
    sources:dict[str,str]
    created:str
    checks:dict[str,Any]
    summary:dict[str,int|float]
    budget:dict[str,Any]
    must_list:dict[str,Any]|None
    must_list_audit:list[dict[str,str]]

class CalculationResult(_CalculationResultRequired,total=False):
    effective_settings:StoreSettings
    catalogs:dict[str,dict[str,Any]]
    supply_route:dict[str,str|None]
    import_audit:dict[str,Any]
    version:dict[str,Any]
    task:dict[str,int|float]


class JstRow(TypedDict):
    sku:str
    demand:str
    quantity:str
    amount:str|None
    price:str|None
    step:str|None
    stock:str|None
    before:str|None
    after:str|None
    missing:str
    reason:str
    issue:bool


class JstReceipt(TypedDict):
    generated:list[str]
    skipped:list[dict[str,Any]]
    zero:list[str]
    errors:list[dict[str,Any]]
    cancelled:bool
