"""Shared cancellation and bundled-resource location, independent of UI and calculation."""
from pathlib import Path

class Cancelled(Exception):pass

def checkpoint(cancel):
    if cancel and cancel.is_set():raise Cancelled('已取消')


def resource_file(name):
    return Path(__file__).parent/name


class CommitProgress(str):
    def __new__(cls,active):
        obj=super().__new__(cls,'正在最终提交，暂不可取消…' if active else '归档已提交；可取消后续导出')
        obj.active=active
        return obj
