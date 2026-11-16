"""Bounded scratch budgets; never changes workers, business rows or commit order."""
import time

from work_memory import snapshot

MIB = 1024 ** 2
STREAM_BUFFER = 128 * 1024


class Budget:
    def __init__(self, provider=None, clock=None):
        self.provider = provider or snapshot
        self.clock = clock or time.monotonic
        self.last = -float('inf')
        self.grow_since = None
        self.forced_until = 0
        self.extra_bytes = 32 * MIB
        self.cache_bytes = 16 * MIB
        self.fast_json_bytes = STREAM_BUFFER
        self.mode = 'unknown'
        self.headroom = None
        self.events = []
        self.shrinks = self.grows = self.emergencies = 0
        self.refresh(force=True, initial=True)

    def refresh(self, force=False, initial=False):
        now = self.clock()
        if not force and now - self.last < .5:
            return False
        self.last = now
        try:
            counters = self.provider()
        except Exception:
            counters = {}
        available = []
        system_known=all(k in counters for k in ('physical_total_bytes','physical_available_bytes','commit_limit_bytes','commit_total_bytes'))
        total = counters.get('physical_total_bytes')
        reserve = max(512 * MIB, min(1024 * MIB, int((total or 0) * .05)))
        physical = counters.get('physical_available_bytes')
        if physical is not None:
            available.append(physical - reserve)
        if all(k in counters for k in ('commit_limit_bytes', 'commit_total_bytes')):
            available.append(counters['commit_limit_bytes'] - counters['commit_total_bytes'] - 256*MIB)
        if counters.get('process_limit_bytes') and 'private_bytes' in counters:
            available.append(counters['process_limit_bytes'] - counters['private_bytes'] - 64 * MIB)
        self.headroom = max(0, min(available)) if available else None
        desired = min(256 * MIB, self.headroom // 4) if available else 32 * MIB
        if not system_known:desired=min(desired,32*MIB)
        desired = desired // (4 * MIB) * (4 * MIB)
        if now < self.forced_until:
            desired = min(desired, 4 * MIB)
        old = self.extra_bytes
        if initial or desired < old:
            self.extra_bytes = desired
            self.grow_since = None
            if not initial:
                self.shrinks += 1
        elif desired > old + max(8 * MIB, old // 2):
            if self.grow_since is None:
                self.grow_since = now
            if now - self.grow_since >= 3:
                self.extra_bytes = min(desired, max(16 * MIB, old * 2))
                self.grow_since = now
                self.grows += 1
        else:
            self.grow_since = None
        self.cache_bytes = min(128 * MIB, self.extra_bytes // 2) if self.extra_bytes >= 8 * MIB else 0
        self.fast_json_bytes = min(8 * MIB, self.extra_bytes // 8) if self.extra_bytes >= 16 * MIB and system_known else STREAM_BUFFER
        self.mode = ('unknown' if not system_known else 'critical' if self.extra_bytes < 8 * MIB
                     else 'tight' if self.extra_bytes < 64 * MIB else 'ample')
        changed = initial or old != self.extra_bytes
        if changed and len(self.events) < 64 and (not self.events or self.events[-1]['mode'] != self.mode
                or abs(self.events[-1]['extra_budget_bytes']-self.extra_bytes)>=8*MIB):
            self.events.append({'at': now, 'mode': self.mode, 'headroom_bytes': self.headroom,
                                'extra_budget_bytes': self.extra_bytes, 'cache_bytes': self.cache_bytes,
                                'fast_json_bytes': self.fast_json_bytes})
        return changed

    def emergency(self):
        self.emergencies += 1
        self.forced_until = self.clock() + 5
        self.refresh(force=True)

    def export_fits(self, remaining_cells):
        self.refresh()
        # Includes Python cells, row dictionaries, styles and potential comments.
        return self.mode not in ('critical', 'unknown') and remaining_cells * 512 + 16 * MIB <= self.extra_bytes

    def restore_fits(self, canonical_bytes):
        self.refresh()
        # Whole JSON restore holds bytes, a Unicode document and decoded rows
        # together. Under pressure decode verified blocks instead.
        return self.mode=='ample' and canonical_bytes*6<=self.extra_bytes

    def description(self):
        names = {'ample': '资源充裕', 'tight': '资源紧张', 'critical': '资源不足', 'unknown': '资源信息不足'}
        return '%s；临时内存预算 %.1f MiB；编码缓存上限 %.1f MiB' % (
            names[self.mode], self.extra_bytes / MIB, self.cache_bytes / MIB)

    def stats(self):
        return {'mode': self.mode, 'extra_budget_bytes': self.extra_bytes, 'headroom_bytes': self.headroom,
                'cache_limit_bytes': self.cache_bytes, 'fast_json_bytes': self.fast_json_bytes,
                'shrinks': self.shrinks, 'grows': self.grows, 'allocation_fallbacks': self.emergencies,
                'events': list(self.events)}
