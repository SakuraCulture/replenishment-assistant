"""Guard the adapter's unchanged matching/calculation bodies after IO extraction.

Not an equivalence substitute: table IO and bridge semantics are independently
compared against fixed 0.6.0 by structure_oracle and verify_memory_adapter.
"""
import ast
from pathlib import Path


def audit_adapter(original,current):
    return audit_adapter_source(Path(original).read_text(encoding='utf-8-sig'),Path(current).read_text(encoding='utf-8-sig'))


def audit_adapter_source(original,current):
    old=ast.parse(original);new=ast.parse(current)
    functions=lambda tree:{n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
    a,b=functions(old),functions(new)
    dump=lambda n:ast.dump(n,include_attributes=False)
    unchanged=[]
    for name,node in a.items():
        if name in {'read_table','prepare'}:continue
        assert dump(node)==dump(b[name]),'adapter algorithm changed: '+name
        unchanged.append(name)
    # The complete mapping/matching/notes/metadata block moved intact.
    start=lambda body:next(i for i,n in enumerate(body) if isinstance(n,ast.For) and isinstance(n.target,ast.Tuple))
    before=a['prepare'].body[start(a['prepare'].body):]
    after=b['prepare_records'].body[start(b['prepare_records'].body):]
    assert [dump(n) for n in before]==[dump(n) for n in after],'prepare matching block changed'
    return {'passed':True,'unchanged_functions':unchanged,'matching_block':'exact AST equality',
            'io_changes':'read_table delegates row parsing; prepare delegates to prepare_records; required header checks reused',
            'separate_required_tests':['strict 0.6.0 full-result oracle','physical temporary XLSX vs in-memory cell/table comparison']}
