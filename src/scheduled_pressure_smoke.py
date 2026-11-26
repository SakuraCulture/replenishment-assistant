"""Interactive isolated Windows acceptance of the existing calculation guard."""
import json
import os
from contextlib import closing
import sqlite3
from pathlib import Path

from performance_counters import Counters


def start_smoke(app, win, args):
    settings=Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()
    case=settings.parent
    marker=json.loads((case/'synthetic.json').read_text('utf-8'))
    if (settings!=Path(app.data_root).resolve() or marker.get('synthetic') is not True
            or marker.get('commit_pressure') is not True):
        raise ValueError('提交余量验收需要独立设置目录和注入标记')
    if (settings/'jst_catalog_sync'/'accounts.json').exists() or (settings/'aoxiang'/'component-state'/'config').exists():
        raise ValueError('提交余量验收只使用本地已验收原件')
    override=case/'commit-room-mib.txt'
    hold_after=marker.get('hold_after_ready')
    counters=Counters()
    platform=win.scheduled.platform
    def with_sample(cancel):
        backend=platform(cancel)
        def sample():
            actual=counters.system()
            try:
                room=int(override.read_text('ascii').strip())*1024**2
            except (OSError,ValueError):
                return actual
            if hold_after is not None:
                with closing(sqlite3.connect((settings/'workbench.sqlite3').as_uri()+'?mode=ro',uri=True)) as database:
                    ready=database.execute("SELECT count(*) FROM scheduled_values WHERE name LIKE 'result:%'").fetchone()[0]
                if ready<hold_after:return actual
            if 0<=room<actual['commit_limit_bytes']-actual['commit_total_bytes']:
                actual['commit_total_bytes']=actual['commit_limit_bytes']-room
            return actual
        backend.commit_sample=sample
        return backend
    win.scheduled.platform=with_sample
