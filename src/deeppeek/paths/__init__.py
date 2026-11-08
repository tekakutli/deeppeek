"""Extract filenames and paths from a markdown document.

Public API:
    extract_paths(source)   -> list[PathMatch]     (flat, deduped)
    extract_events(source)  -> list[Event]         (headers + paths)
    PathMatch, HeaderEvent, PathEvent, Event
"""
from .models import PathMatch
from .extract import (
    Event, HeaderEvent, PathEvent, extract_events, extract_paths,
)

__all__ = [
    "PathMatch", "Event", "HeaderEvent", "PathEvent",
    "extract_events", "extract_paths",
]
