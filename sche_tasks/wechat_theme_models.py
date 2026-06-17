from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Message:
    idx: int
    timestamp: int
    time: str
    sender: str
    mtype: str
    content: str
    semantic_text: str
    local_id: str
    urls: list[str]
    attachments: list[dict[str, str]]
    is_meaningful: bool
    score: int
    tags: list[str]


@dataclass
class RecallCandidate:
    idx: int
    timestamp: int
    time: str
    sender: str
    content: str
    semantic_text: str
    score: int
    tags: list[str]
    urls: list[str]
    attachments: list[dict[str, str]]


def to_dict(obj: Any) -> Any:
    if hasattr(obj, '__dataclass_fields__'):
        return asdict(obj)
    return obj
