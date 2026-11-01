"""AX06 local native-template preparation check; synthetic payment only, no platform calls."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from aoxiang_directory import atomic_json, file_hash
from aoxiang_purchase import freeze_payment, validate_basis
from aoxiang_purchase_excel import generate
from test_aoxiang_purchase import config
import test_lifecycle as fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = file_hash(args.template)
    # Inspect the actual received native workbook with the existing component parser,
    # including instructions, sample rows, hidden sheet, validations and defined names.
    script = "import {inspectPurchaseTemplate,templateContract} from './src/purchase-template.mjs';import {canonical} from './src/common.mjs';const x=await inspectPurchaseTemplate(process.argv[1]),c=await templateContract();if(canonical(x.schema)!==canonical(c.schema))throw Error('native schema differs');console.log(JSON.stringify({...x,status:'SUPPORTED',contractVersion:c.version}));"
    raw = subprocess.check_output([str(ROOT / 'components/aoxiang/runtime/node.exe'), '--input-type=module', '-e', script, str(args.template.resolve())], cwd=ROOT / 'components/aoxiang', encoding='utf-8')
    receipt = json.loads(raw)
    receipt.update(file=str(args.template.resolve()), sha256=before, identity={'accountId': 'A', 'merchantCode': 'M'},
                   uiProductLimit=None, source='provided native template + synthetic payment QA; NOT a live latest-template receipt')
    atomic_json(output / 'template-inspection.json', receipt)
    f = fixture.LifecycleTests()
    f.setUp()
    try:
        f.save('return')
        f.save('payment', [{'付款单商品数量': 7, '实际金额': .99}])
        suppliers = {'河北仓': '2078246', '山东仓': 'ZXC2025'}
        facts_before, view_before = f.old_tables(), f.view()
        times = []
        artifacts = []
        for index in range(3):
            started = time.perf_counter()
            plan = freeze_payment(f.path, f.h, config(), suppliers)
            prepared = time.perf_counter()
            validate_basis(f.path, plan, config(), suppliers)
            artifact = generate(plan, receipt, ROOT / 'components/aoxiang/assets/purchase-import-template-contract.json', output / f'合成采购-{index+1}.xlsx')
            times.append({'prepare_seconds': prepared-started, 'recheck_and_write_seconds': time.perf_counter()-prepared,
                          'total_seconds': time.perf_counter()-started})
            artifacts.append(artifact)
            with zipfile.ZipFile(args.template) as a, zipfile.ZipFile(artifact['file']) as b:
                if a.namelist() != b.namelist():
                    raise ValueError('ZIP member order changed')
                changes = [n for n in a.namelist() if a.read(n) != b.read(n)]
                if changes != [artifact['modified_zip_member']]:
                    raise ValueError('Template unrelated members changed: ' + repr(changes))
        import openpyxl
        wb = openpyxl.load_workbook(artifacts[0]['file'])
        try:
            ws = wb['sheet1']
            values = [ws.cell(8,c).value for c in range(1,13)]
            expected = ['T','2078246','0001',7,None,.99,None,'测试商品',None,plan['head']['number'],'否',None]
            if values != expected or ws.max_row != 8 or [ws[c].data_type for c in ['A8','B8','C8']] != ['s','s','s']:
                raise ValueError('Native output values/types/sample cleanup differ')
        finally:
            wb.close()
        if f.old_tables() != facts_before or f.view() != view_before or file_hash(args.template) != before:
            raise ValueError('Source business data or template changed')
        atomic_json(output / 'payment-plan.json', plan)
        report = {'scope': 'AX06 local payment snapshot and file generator only', 'passed_local': True,
                  'fixture': 'Received native purchase template; synthetic local signed/return/payment order',
                  'head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,encoding='utf-8').strip(),
                  'template_sha256': before, 'outputs': artifacts, 'timings_raw': times,
                  'quantity': plan['total_quantity'], 'amount': plan['total_amount'],
                  'changed_original_template': False, 'changed_business_facts': False,
                  'code_sha256': {n:file_hash(ROOT / n) for n in ['src/aoxiang_purchase.py','src/aoxiang_purchase_excel.py']},
                  'limitations': ['No live latest-template fetch, procurement upload or EXE verification.',
                                  'Three tiny warm local synthetic trials are diagnostics, not real batch performance evidence.',
                                  'UI and durable procurement creation/binding remain under development.']}
        atomic_json(output / 'report.json', report)
        print(json.dumps({'passed_local': True, 'quantity': plan['total_quantity'], 'amount': plan['total_amount']},ensure_ascii=False))
    finally:
        f.tearDown()


if __name__ == '__main__':
    main()
