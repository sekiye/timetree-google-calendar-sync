from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SourceEvent:
    source_id: str
    title: str
    start: datetime
    end: datetime
    source_url: str | None = None


@dataclass(frozen=True)
class FetchResult:
    events: list[SourceEvent]
    complete: bool
    incomplete_reason: str | None = None
