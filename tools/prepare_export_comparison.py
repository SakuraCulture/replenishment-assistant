"""Prepare isolated stage documents from the frozen signing pressure fixture."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import lifecycle_facts
import lifecycle_store
import order_lifecycle
import warehouse_execution
import work_repository
from export_files import sha256


def prepare(source, output):
    output.mkdir()
    settings = output / 'settings'
    settings.mkdir()
    path = settings / 'workbench.sqlite3'
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as before:
        with closing(sqlite3.connect(path)) as target:
            before.backup(target)
    work_repository.Repository(settings)
    evidence = []
    with closing(sqlite3.connect(path)) as db, db:
        lifecycle_store.schema(db)
        if db.execute('SELECT COUNT(*) FROM lifecycle_versions').fetchone()[0]:
            raise ValueError('Source fixture already has stage documents')
        heads = warehouse_execution.heads(db)
        if len(heads) != 40:
            raise ValueError('Expected forty independent warehouse orders')
        # Interleave warehouse scopes in the selected order; neither export may
        # sort the resulting files or the rows within each warehouse.
        for index, head in enumerate(heads, 1):
            loaded = lifecycle_facts.full_head(path, head)
            products = loaded['products']
            if len(products) != 15000:
                raise ValueError((head['number'], len(products)))
            versions = {}
            for stage, quantity in [('return', 2), ('operations', 3)]:
                rows = []
                for position, (sku, item) in enumerate(products.items()):
                    q = str(0 if position % 23 == 0 else quantity)
                    rows.append({'sku': sku, 'name': item['name'], 'quantity': q, 'base_quantity': q,
                                 'unit': '库存单位', 'factor': 1, 'conversion_basis': None,
                                 'amount': None, 'currency': '', 'document_number': f'{stage}-{index}',
                                 'document_date': None, 'note': None, 'sources': [],
                                 'raw_headers': [], 'raw_values': []})
                plan = order_lifecycle.make_plan(db, loaded, stage, rows, [])
                lifecycle_store.insert_version(db, head, stage, 1, plan['digest'], work_repository.dumps(plan['payload']))
                versions[stage] = plan['digest']
            db.commit()
            evidence.append({'number': head['number'], 'rows': len(products), 'versions': versions})
            print(f'{index}/40 {head["number"]}', flush=True)
    (output / 'fixture.json').write_text(json.dumps({'synthetic': True, 'live_platform': False,
        'source': str(source.resolve()), 'signed_source_sha256': sha256(source),
        'database_sha256': sha256(path), 'heads': heads, 'stages': evidence},
        ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    arguments = parser.parse_args()
    prepare(arguments.source, arguments.out)
