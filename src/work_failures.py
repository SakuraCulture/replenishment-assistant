"""Small failure evidence for work I/O; never contains a business payload."""
import json
import os
import tempfile
import traceback
import uuid
from datetime import datetime
from pathlib import Path


COMMIT_TEXT = {'not_started': '未开始最终提交', 'unknown': '最终提交结果待核',
               'committed': '已提交，原签发修订保留'}


def exception_text(exc):
    name = type(exc).__name__
    message = str(exc).strip()
    if not message:
        message = ('处理进程无法继续分配内存' if isinstance(exc, MemoryError)
                   else '异常未提供文字说明，请查看诊断记录')
    return name + '：' + message


class WorkFailure(RuntimeError):
    def __init__(self, evidence, message=None):
        self.evidence = evidence
        super().__init__(message or format_failure(evidence))


def format_failure(evidence):
    lines = ['失败阶段：' + evidence['stage'], '原因：' + evidence['reason'],
             '提交状态：' + COMMIT_TEXT.get(evidence['commit_state'], COMMIT_TEXT['unknown'])]
    if evidence.get('diagnostic'): lines.append('诊断记录：' + evidence['diagnostic'])
    if evidence.get('diagnostic_error'): lines.append('诊断记录未能保存：' + evidence['diagnostic_error'])
    return '\n'.join(lines)


class FailureContext:
    def __init__(self, operation):
        self.operation = operation
        self.stage = '准备操作'
        self.commit_state = 'not_started'
        self.heads = []

    def progress(self, value):
        if hasattr(value, 'active'):
            self.commit_state = 'unknown' if value.active else 'committed'
        self.stage = str(value)

    def capture(self, exc, database):
        if isinstance(exc, WorkFailure): return exc.evidence
        # Even a bare MemoryError/AssertionError must retain its type. Formatting
        # the full traceback is best effort and cannot replace the first error.
        evidence = dict(id=uuid.uuid4().hex, created=datetime.now().astimezone().isoformat(),
                        operation=self.operation, stage=self.stage, commit_state=self.commit_state,
                        exception_type=type(exc).__name__, reason=exception_text(exc))
        if self.heads:evidence['prepared_heads'] = self.heads
        try:
            evidence['exception_repr'] = repr(exc)
            evidence['traceback'] = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-65536:]
        except Exception as formatting:
            evidence['traceback_error'] = type(formatting).__name__
        temporary = None
        try:
            root = Path(database).resolve().parent / 'diagnostics'
            root.mkdir(parents=True, exist_ok=True)
            target = root / ('work-io-' + evidence['id'] + '.json')
            evidence['diagnostic'] = str(target)
            fd, temporary = tempfile.mkstemp(prefix='.work-error-', suffix='.json', dir=root)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                json.dump(evidence, stream, ensure_ascii=False, indent=2)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target); temporary = None
        except Exception as saving:
            evidence.pop('diagnostic', None)
            evidence['diagnostic_error'] = exception_text(saving)
        finally:
            if temporary:
                try: os.unlink(temporary)
                except OSError: pass
        return evidence
