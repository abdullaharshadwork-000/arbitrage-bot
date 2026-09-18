"""Semantic knowledge store.

Phase 9 foundation.

Stores validated lessons with evidence metadata. A lesson is NOT truth
until sample size and confidence thresholds are met. Never places orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class KnowledgeItem:
    id: str
    statement: str
    category: str = "lesson"  # lesson | pattern | warning
    sample_size: int = 0
    confidence: float = 0.0
    applicable_regimes: tuple[str, ...] = ()
    applicable_symbols: tuple[str, ...] = ()
    supporting_evidence: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_utc_now)
    last_validated_at: Optional[datetime] = None
    status: str = "provisional"  # provisional | validated | deprecated

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        if self.last_validated_at:
            data["last_validated_at"] = self.last_validated_at.isoformat()
        data["applicable_regimes"] = list(self.applicable_regimes)
        data["applicable_symbols"] = list(self.applicable_symbols)
        data["supporting_evidence"] = list(self.supporting_evidence)
        return data


class KnowledgeStore:
    """In-memory knowledge base (can later persist to SQLite)."""

    def __init__(self, *,
                 min_sample_for_validated: int = 20,
                 min_confidence_for_validated: float = 0.6):
        self._items: dict[str, KnowledgeItem] = {}
        self.min_sample = int(min_sample_for_validated)
        self.min_confidence = float(min_confidence_for_validated)

    def add(
        self,
        statement: str,
        *,
        category: str = "lesson",
        sample_size: int = 0,
        confidence: float = 0.0,
        regimes: tuple[str, ...] = (),
        symbols: tuple[str, ...] = (),
        evidence: tuple[str, ...] = (),
    ) -> KnowledgeItem:
        status = "provisional"
        if sample_size >= self.min_sample and confidence >= self.min_confidence:
            status = "validated"
        item = KnowledgeItem(
            id=f"kn_{uuid4().hex[:12]}",
            statement=statement.strip(),
            category=category,
            sample_size=int(sample_size),
            confidence=float(confidence),
            applicable_regimes=regimes,
            applicable_symbols=symbols,
            supporting_evidence=evidence,
            last_validated_at=_utc_now() if status == "validated" else None,
            status=status,
        )
        self._items[item.id] = item
        return item

    def list_items(self, *,
                   status: Optional[str] = None,
                   category: Optional[str] = None) -> list[KnowledgeItem]:
        items = list(self._items.values())
        if status:
            items = [i for i in items if i.status == status]
        if category:
            items = [i for i in items if i.category == category]
        return items

    def deprecate(self, item_id: str) -> Optional[KnowledgeItem]:
        item = self._items.get(item_id)
        if not item:
            return None
        updated = KnowledgeItem(
            id=item.id,
            statement=item.statement,
            category=item.category,
            sample_size=item.sample_size,
            confidence=item.confidence,
            applicable_regimes=item.applicable_regimes,
            applicable_symbols=item.applicable_symbols,
            supporting_evidence=item.supporting_evidence,
            created_at=item.created_at,
            last_validated_at=item.last_validated_at,
            status="deprecated",
        )
        self._items[item_id] = updated
        return updated

    def snapshot(self) -> dict[str, Any]:
        items = self.list_items()
        return {
            "count": len(items),
            "validated": sum(1 for i in items if i.status == "validated"),
            "provisional": sum(1 for i in items if i.status == "provisional"),
            "items": [i.as_dict() for i in items[:50]],
        }


__all__ = ["KnowledgeItem", "KnowledgeStore"]
