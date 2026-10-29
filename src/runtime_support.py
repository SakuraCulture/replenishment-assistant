"""Shared cancellation and bundled-resource location, independent of UI and calculation."""
from pathlib import Path

class Cancelled(Exception):pass

def checkpoint(cancel):
    if cancel and cancel.is_set():raise Cancelled('已取消')


def resource_file(name):
    return Path(__file__).parent/name
