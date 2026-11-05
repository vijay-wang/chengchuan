"""Bounded caches for immutable presentation data, with explicit age and weight."""

from collections import OrderedDict
from dataclasses import replace
from time import monotonic


class MemoryCache:
    def __init__(self, capacity=32, max_weight=120000):
        self.capacity, self.max_weight = capacity, max_weight
        self.items = OrderedDict()
        self.weight = 0

    def clear(self):
        self.items.clear()
        self.weight = 0

    def get(self, key, max_age=None):
        item = self.items.get(key)
        if item is None:
            return None
        value, weight, inserted = item
        if max_age is not None and monotonic() - inserted >= max_age:
            self.items.pop(key)
            self.weight -= weight
            return None
        self.items.move_to_end(key)
        return value

    def put(self, key, value, weight=1):
        if key in self.items:
            self.weight -= self.items.pop(key)[1]
        if weight > self.max_weight:
            return
        self.items[key] = value, weight, monotonic()
        self.weight += weight
        while len(self.items) > self.capacity or self.weight > self.max_weight:
            self.weight -= self.items.popitem(last=False)[1][1]


def bundle_weight(bundle):
    return sum(max(1, len(t.rows)) * max(1, len(t.columns)) for t in bundle.tables)


def cached_bundle(bundle, request=None):
    return replace(
        bundle,
        request=request or bundle.request,
        tables=tuple(replace(t, meta=replace(t.meta, cached=True)) for t in bundle.tables),
    )
