"""Phrase verification against archived broadcast captions.

A verification ("核查") names one channel, one UTC window and one exact phrase.
Every archived cue is matched *individually* and *literally*: ``cue.text`` must
equal the phrase, and a cue that was merged across two segments for display is
still checked as two separate rows.

The first-seen instant is the earliest instant at which three intervals overlap:

* the matched cue (``cue.start_ms .. cue.end_ms``),
* the captured segment that records that cue, and
* the requested window (``from_ms .. to_ms``).

In other words ``max(cue.start, segment.start, window.start)``.  The match only
counts when that point lies strictly before the end of all three intervals.

Conclusions:

``confirmed``
    A match exists and the interval from the window start to the first-seen
    instant is continuously covered by captured segments (no uncovered span).
``not_found``
    No match exists and the *whole* requested window is continuously covered by
    captured segments.
``unverified``
    Anything else -- typically a hole in the archive that could still hide the
    phrase.

The module deliberately has no dependency on :mod:`app.db` (which imports it)
and only relies on attributes of the segment/cue rows passed in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

CONCLUSION_CONFIRMED = "confirmed"
CONCLUSION_NOT_FOUND = "not_found"
CONCLUSION_UNVERIFIED = "unverified"

CONCLUSIONS = (CONCLUSION_CONFIRMED, CONCLUSION_NOT_FOUND, CONCLUSION_UNVERIFIED)

CAPTURED = "captured"


@dataclass(frozen=True)
class Judgment:
    conclusion: str
    first_seen_ms: Optional[int]
    first_seen_seq: Optional[int]
    matched_text: Optional[str]
    uncovered: tuple[tuple[int, int], ...]

    @property
    def signature(self) -> tuple[str, Optional[int]]:
        """What makes two judgments "the same judgment" for de-duplication."""
        return (self.conclusion, self.first_seen_ms)


def _merged_covered(captured_segments: Iterable) -> list[tuple[int, int]]:
    spans = sorted(
        (segment.start_ms, segment.end_ms)
        for segment in captured_segments
        if segment.end_ms > segment.start_ms
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def uncovered_intervals(
    captured_segments: Iterable, from_ms: int, to_ms: int
) -> list[tuple[int, int]]:
    """Parts of ``[from_ms, to_ms)`` not covered by any captured segment."""
    uncovered: list[tuple[int, int]] = []
    cursor = from_ms
    for start, end in _merged_covered(captured_segments):
        if end <= cursor:
            continue
        if start >= to_ms:
            break
        if start > cursor:
            uncovered.append((cursor, start))
        cursor = max(cursor, end)
        if cursor >= to_ms:
            break
    if cursor < to_ms:
        uncovered.append((cursor, to_ms))
    return uncovered


def evaluate(
    captured_segments: Sequence,
    cues_by_seq: dict[int, Sequence],
    from_ms: int,
    to_ms: int,
    phrase: str,
) -> Judgment:
    """Judge one phrase over ``[from_ms, to_ms)`` against the current archive."""
    by_seq = {segment.seq: segment for segment in captured_segments}

    best: Optional[tuple[tuple[int, int], object, object]] = None
    for seq, cues in cues_by_seq.items():
        segment = by_seq.get(seq)
        if segment is None:
            continue
        for cue in cues:
            if cue.text != phrase:
                continue
            start = max(cue.start_ms, segment.start_ms, from_ms)
            end = min(cue.end_ms, segment.end_ms, to_ms)
            if end <= start:
                continue
            # Earliest overlap start; ties go to the lower sequence number.
            key = (start, seq)
            if best is None or key < best[0]:
                best = (key, cue, segment)

    uncovered = uncovered_intervals(captured_segments, from_ms, to_ms)

    if best is not None:
        first_seen_ms = best[0][0]
        cue = best[1]
        segment = best[2]
        gap_before_first_seen = any(
            start < first_seen_ms and end > from_ms for start, end in uncovered
        )
        conclusion = (
            CONCLUSION_UNVERIFIED if gap_before_first_seen else CONCLUSION_CONFIRMED
        )
        return Judgment(
            conclusion=conclusion,
            first_seen_ms=first_seen_ms,
            first_seen_seq=segment.seq,
            matched_text=cue.text,
            uncovered=tuple(uncovered),
        )

    conclusion = CONCLUSION_NOT_FOUND if not uncovered else CONCLUSION_UNVERIFIED
    return Judgment(
        conclusion=conclusion,
        first_seen_ms=None,
        first_seen_seq=None,
        matched_text=None,
        uncovered=tuple(uncovered),
    )
