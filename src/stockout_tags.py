"""Analysis-only virtual-category policy; no replenishment rule side effects."""
import re

ASSESSMENT = '淘便考核'
VERSION = 'virtual-category-2'


def tokens(raw):
    return None if raw is None else {s.strip() for s in re.split(r'[,，;；\r\n]+', str(raw)) if s.strip()}


def assessment(raw):
    values = tokens(raw)
    return None if values is None else int(ASSESSMENT in values)
