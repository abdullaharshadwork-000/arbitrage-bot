"""Strategy Registry – versioned strategies with genealogy.

Phase 5 foundation.

Rules:
* Every change creates a new StrategyVersion (never overwrite).
* Parent links form the genealogy tree.
* Status transitions are explicit.
* The registry is the single source of truth for later selection,
  promotion, and rollback.
* Nothing here places orders or changes risk limits.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .domain import StrategyStatus, StrategyVersion


def _new_id() -> str:
    return f"strat_{uuid4().hex[:12]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrategyRegistry:
    """In-memory registry of StrategyVersion records.

    Later phases will add persistence. For now the registry is process-local
    and deterministic so it can be unit-tested without a database.
    """

    def __init__(self):
        self._versions: dict[str, StrategyVersion] = {}
        self._by_name: dict[str, list[str]] = {}  # name -> list of version ids (oldest first)

    # ------------------------------------------------------------------ create

    def create(
        self,
        name: str,
        version: str,
        *,
        parent_version_id: Optional[str] = None,
        status: StrategyStatus = StrategyStatus.DRAFT,
        description: str = "",
        parameters: Optional[dict[str, Any]] = None,
        supported_symbols: tuple[str, ...] = (),
        supported_timeframes: tuple[str, ...] = (),
        intended_regimes: tuple[str, ...] = (),
        feature_dependencies: tuple[str, ...] = (),
        creation_reason: str = "",
        hypothesis_id: Optional[str] = None,
        created_by: str = "system",
    ) -> StrategyVersion:
        """Register a new StrategyVersion. Always creates a new id."""
        if parent_version_id and parent_version_id not in self._versions:
            raise ValueError(f"parent_version_id {parent_version_id!r} not found")

        sv = StrategyVersion(
            id=_new_id(),
            name=name,
            version=version,
            parent_version_id=parent_version_id,
            status=status,
            description=description,
            parameters=dict(parameters or {}),
            supported_symbols=supported_symbols,
            supported_timeframes=supported_timeframes,
            intended_regimes=intended_regimes,
            feature_dependencies=feature_dependencies,
            creation_reason=creation_reason,
            hypothesis_id=hypothesis_id,
            created_by=created_by,
            created_at=_utc_now(),
        )
        self._versions[sv.id] = sv
        self._by_name.setdefault(name, []).append(sv.id)
        return sv

    def fork(
        self,
        parent_id: str,
        new_version: str,
        *,
        parameters: Optional[dict[str, Any]] = None,
        creation_reason: str = "",
        created_by: str = "system",
        status: StrategyStatus = StrategyStatus.CANDIDATE,
    ) -> StrategyVersion:
        """Create a child version from an existing parent."""
        parent = self.get(parent_id)
        if parent is None:
            raise ValueError(f"parent {parent_id!r} not found")
        merged_params = dict(parent.parameters)
        if parameters:
            merged_params.update(parameters)
        return self.create(
            name=parent.name,
            version=new_version,
            parent_version_id=parent.id,
            status=status,
            description=parent.description,
            parameters=merged_params,
            supported_symbols=parent.supported_symbols,
            supported_timeframes=parent.supported_timeframes,
            intended_regimes=parent.intended_regimes,
            feature_dependencies=parent.feature_dependencies,
            creation_reason=creation_reason or f"forked from {parent.version}",
            created_by=created_by,
        )

    # ------------------------------------------------------------------ status

    def set_status(self, version_id: str, status: StrategyStatus) -> StrategyVersion:
        """Transition status. Returns the new immutable StrategyVersion."""
        current = self.get(version_id)
        if current is None:
            raise ValueError(f"version {version_id!r} not found")
        updated = replace(current, status=status)
        self._versions[version_id] = updated
        return updated

    # ------------------------------------------------------------------ query

    def get(self, version_id: str) -> Optional[StrategyVersion]:
        return self._versions.get(version_id)

    def list_by_name(self, name: str) -> list[StrategyVersion]:
        ids = self._by_name.get(name, [])
        return [self._versions[i] for i in ids if i in self._versions]

    def list_by_status(self, status: StrategyStatus) -> list[StrategyVersion]:
        return [v for v in self._versions.values() if v.status is status]

    def latest(self, name: str) -> Optional[StrategyVersion]:
        versions = self.list_by_name(name)
        return versions[-1] if versions else None

    def genealogy(self, version_id: str) -> list[StrategyVersion]:
        """Return the chain from root to this version (inclusive)."""
        chain: list[StrategyVersion] = []
        current = self.get(version_id)
        seen = set()
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = self.get(current.parent_version_id) if current.parent_version_id else None
        chain.reverse()
        return chain

    def all_versions(self) -> list[StrategyVersion]:
        return list(self._versions.values())


__all__ = ["StrategyRegistry"]
