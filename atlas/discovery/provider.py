from __future__ import annotations

from typing import Protocol

from .models import DiscoveryBatch


class DiscoveryProvider(Protocol):
    name: str

    def discover(self, queries: list[str]) -> DiscoveryBatch:
        ...
