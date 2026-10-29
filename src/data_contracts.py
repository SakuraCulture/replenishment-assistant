"""Lightweight static contracts only: no constructors, coercion or storage migration.

Business letters remain dictionaries. Optional fields reflect actual stage-specific
presence; None, zero, empty text and pending text are distinct, never defaulted here.
See docs/字段与数据契约.md for every model letter and precedence rules.
"""
from __future__ import annotations
from datetime import date,datetime,time,timedelta
from typing import Any,NotRequired,TypeAlias,TypedDict

Cell:TypeAlias=str|int|float|bool|None|date|datetime|time|timedelta
RawRecord:TypeAlias=dict[str,Cell]
ModelRow:TypeAlias=dict[str,Any]  # letters plus named diagnostic/audit/execution fields
WarehouseCycles:TypeAlias=dict[str,tuple[float,float]|list[float]]

class RowMetadata(TypedDict):
    export_row:int
    product_row:int|None
    match:str
    blocking:list[str]
    notes:list[str]
    category:Cell
    export_status:Cell
    export_updated:Cell
    profile_override:NotRequired[Cell]
    export_file:NotRequired[str]
    export_sheet:NotRequired[str]
    product_file:NotRequired[str]
    product_sheet:NotRequired[str]
    product_revision:NotRequired[str]
    warehouse_available:NotRequired[Cell]
    routing:NotRequired[dict[str,Any]]

NormalizedRecord:TypeAlias=tuple[ModelRow,RowMetadata]

class ParameterConfig(TypedDict):
    parameters:dict[str,int|float]
    weight_profiles:dict[str,dict[str,int|float]]
    category_profiles:dict[str,str]
    warehouses:WarehouseCycles
    exception:bool

class ParameterPreset(TypedDict):
    id:str
    version:int
    name:str
    config:ParameterConfig
    builtin:bool
    base_config:NotRequired[ParameterConfig]

class StoreSettings(TypedDict):
    warehouses:WarehouseCycles
    exception:bool
    budget:float|int|None
    supply_route:NotRequired[dict[str,str|None]]
    parameter_config:NotRequired[ParameterConfig]
    preset:NotRequired[dict[str,Any]]
    parameter_signature:NotRequired[str]

class InputBatch(TypedDict):
    normalized:list[NormalizedRecord]
    config:dict[str,Any]
    stores:dict[str,str]
    hashes:dict[str,str]
    warehouses:list[str]
    info:NotRequired[dict[str,Any]]
    loaded_at:NotRequired[str]
    intake:NotRequired[dict[str,Any]]
    blocked:NotRequired[dict[str,int]]
    catalogs:NotRequired[dict[str,dict[str,Any]]]
    warehouse_rows:NotRequired[dict[str,list[NormalizedRecord]]]
    sales:NotRequired[str|list[str]]
    products:NotRequired[str|None]

class CalculationResult(TypedDict):
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
    effective_settings:NotRequired[StoreSettings]
    catalogs:NotRequired[dict[str,dict[str,Any]]]
    supply_route:NotRequired[dict[str,str|None]]
    import_audit:NotRequired[dict[str,Any]]
    version:NotRequired[dict[str,Any]]
    task:NotRequired[dict[str,int|float]]
