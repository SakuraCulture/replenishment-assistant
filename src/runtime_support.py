"""Shared cancellation and bundled-resource location, independent of UI and calculation."""
from pathlib import Path

class Cancelled(Exception):pass

def checkpoint(cancel):
    if cancel and cancel.is_set():raise Cancelled('已取消')


def resource_file(name):
    return Path(__file__).parent/name


class CommitProgress(str):
    def __new__(cls,active,*,deleting=False):
        if deleting:
            text='正在提交删除，暂不可取消…' if active else '删除已提交，正在核对结果…'
        else:
            text='正在最终提交，暂不可取消…' if active else '归档已提交；可取消后续导出'
        obj=super().__new__(cls,text)
        obj.active=active
        return obj
