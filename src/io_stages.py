"""Measured I/O stages. Progress never estimates a completion percentage."""
import hashlib,json,os,time,zlib
from contextlib import contextmanager
from pathlib import Path
from export_progress import RowProgress


@contextmanager
def stage(name,progress=lambda _:None):
    progress(name+'…');start=time.perf_counter();ok=False
    try:
        yield
        ok=True
    finally:
        target=os.environ.get('REPLENISHMENT_IO_TRACE')
        if target:
            # Opt-in QA trace, no payloads, paths or business values recorded.
            try:
                folder=Path(target);folder.mkdir(parents=True,exist_ok=True)
                with (folder/(str(os.getpid())+'.jsonl')).open('a',encoding='utf-8') as f:
                    f.write(json.dumps({'stage':name,'seconds':time.perf_counter()-start,'ok':ok},ensure_ascii=False)+'\n')
            except OSError:pass


def digest(raw,progress=lambda _:None):
    with stage('校验完整快照',progress):
        h=hashlib.sha256();total=len(raw)
        for offset in range(0,total,4*1024*1024):
            end=min(offset+4*1024*1024,total);h.update(memoryview(raw)[offset:end])
            progress(RowProgress(f'校验快照：{end/2**20:.1f} / {total/2**20:.1f} MB',end,total))
        return h.hexdigest()


def compress(raw,progress=lambda _:None):
    with stage('压缩完整快照',progress):
        encoder=zlib.compressobj();parts=[];total=len(raw)
        for offset in range(0,total,4*1024*1024):
            end=min(offset+4*1024*1024,total);parts.append(encoder.compress(memoryview(raw)[offset:end]))
            progress(RowProgress(f'压缩快照：{end/2**20:.1f} / {total/2**20:.1f} MB',end,total))
        parts.append(encoder.flush());return b''.join(parts)
