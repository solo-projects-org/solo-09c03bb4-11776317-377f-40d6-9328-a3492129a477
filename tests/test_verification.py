"""Phrase verification: literal matching, coverage rules and judgment history."""
from __future__ import annotations

import pytest

import app.db as db_module
from app.timeutil import iso_from_ms, parse_iso_ms


def at(seconds: float) -> int:
    return parse_iso_ms("2026-11-20T00:00:00Z") + int(seconds * 1000)


def iso(seconds: float) -> str:
    return iso_from_ms(at(seconds))


def register(harness, phrase: str, start_s: float, end_s: float, channel: str = "news"):
    response = harness.client.post(
        "/api/verifications",
        json={
            "channel_id": channel,
            "phrase": phrase,
            "from_at": iso(start_s),
            "to_at": iso(end_s),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def list_verifications(harness, channel: str = "news") -> list[dict]:
    response = harness.client.get("/api/verifications")
    assert response.status_code == 200, response.text
    return [
        item
        for item in response.json()["verifications"]
        if item["channel_id"] == channel
    ]


def test_registration_saves_the_judgment_made_then(harness):
    harness.capture_channel("news")
    record = register(harness, "新闻｜第6段｜现场连线", 36, 48)

    assert len(record["judgments"]) == 1
    judgment = record["judgments"][0]
    assert judgment["conclusion"] == "confirmed"
    # Cue starts 300 ms into #6, which starts at 00:00:36.
    assert judgment["first_seen_ms"] == at(39.3)
    assert judgment["first_seen_utc"] == "2026-11-20T00:00:39.300Z"
    assert judgment["first_seen_seq"] == 6
    assert judgment["matched_text"] == "新闻｜第6段｜现场连线"
    assert judgment["uncovered"] == []

    stored = list_verifications(harness)
    assert len(stored) == 1
    assert stored[0]["phrase"] == "新闻｜第6段｜现场连线"
    assert stored[0]["current"]["conclusion"] == "confirmed"


def test_first_seen_clips_to_the_window_start(harness):
    harness.capture_channel("news")
    # The cue starts at 39.300 but the window starts at 40.000, so the earliest
    # common instant of cue, segment and window is the window start.
    record = register(harness, "新闻｜第6段｜现场连线", 40, 48)
    judgment = record["judgments"][0]
    assert judgment["conclusion"] == "confirmed"
    assert judgment["first_seen_ms"] == at(40)
    assert judgment["first_seen_seq"] == 6


def test_full_coverage_without_match_is_not_found(harness):
    harness.capture_channel("news")
    record = register(harness, "这句话没有播出", 24, 72)
    judgment = record["judgments"][0]
    assert judgment["conclusion"] == "not_found"
    assert judgment["first_seen_ms"] is None
    assert judgment["first_seen_seq"] is None
    assert judgment["matched_text"] is None
    assert judgment["uncovered"] == []


def test_partial_phrase_does_not_match_a_cue(harness):
    harness.capture_channel("news")
    # Single-cue, literal matching: a substring of a real cue is not a hit.
    record = register(harness, "现场连线", 24, 72)
    assert record["judgments"][0]["conclusion"] == "not_found"


def test_hole_before_the_match_stays_unverified(harness):
    harness.set_gap(6, True)
    harness.capture_channel("news")  # #6 stays pending, 36..42 not covered
    record = register(harness, "新闻｜第8段｜整点快讯", 36, 54)
    judgment = record["judgments"][0]
    assert judgment["conclusion"] == "unverified"
    assert judgment["first_seen_ms"] == at(48.3)
    assert judgment["first_seen_seq"] == 8
    assert [(span["start_ms"], span["end_ms"]) for span in judgment["uncovered"]] == [
        (at(36), at(42))
    ]


def test_hole_after_the_match_does_not_prevent_confirmation(harness):
    harness.set_gap(8, True)
    harness.capture_channel("news")
    record = register(harness, "新闻｜第6段｜现场连线", 36, 54)
    judgment = record["judgments"][0]
    # The gap 48..54 lies after the first-seen instant, so the check is proven.
    assert judgment["conclusion"] == "confirmed"
    assert judgment["first_seen_ms"] == at(39.3)
    assert [(span["start_ms"], span["end_ms"]) for span in judgment["uncovered"]] == [
        (at(48), at(54))
    ]


def test_late_segment_rewrites_first_seen_and_appends_one_judgment(harness):
    harness.set_gap(6, True)
    harness.capture_channel("news")  # window 4..11, #6 pending
    # The spanning cue is repeated in #6's and #7's files.  With #6 missing the
    # earliest archived copy is #7's, clamped to #7's start (00:00:42).
    record = register(harness, "新闻｜跨段字幕｜第6段起", 36, 54)
    first = record["judgments"][-1]
    assert first["conclusion"] == "unverified"
    assert first["first_seen_ms"] == at(42)
    assert first["first_seen_seq"] == 7

    harness.set_gap(6, False)
    harness.capture_channel("news")  # late arrival of #6

    stored = list_verifications(harness)[0]
    assert len(stored["judgments"]) == 2
    latest = stored["judgments"][-1]
    assert latest["conclusion"] == "confirmed"
    assert latest["first_seen_ms"] == at(40)  # rewritten earlier by #6
    assert latest["first_seen_seq"] == 6
    assert latest["matched_text"] == "新闻｜跨段字幕｜第6段起"
    # The first judgment is preserved verbatim.
    assert stored["judgments"][0]["conclusion"] == "unverified"
    assert stored["judgments"][0]["first_seen_ms"] == at(42)
    assert stored["judgments"][0]["uncovered"][0]["start_utc"] == "2026-11-20T00:00:36.000Z"

    # Re-captures and a restart must not record the same judgment again.
    harness.capture_channel("news")
    harness.restart()
    harness.capture_channel("news")
    after = list_verifications(harness)[0]
    assert [(j["conclusion"], j["first_seen_ms"]) for j in after["judgments"]] == [
        ("unverified", at(42)),
        ("confirmed", at(40)),
    ]


def test_empty_caption_segment_fills_a_coverage_hole(harness):
    harness.set_gap(5, True)
    harness.capture_channel("news")  # #5 (caption-less) stays pending
    record = register(harness, "新闻｜第5段｜现场连线", 30, 36)
    assert record["judgments"][-1]["conclusion"] == "unverified"

    harness.set_gap(5, False)
    harness.capture_channel("news")
    stored = list_verifications(harness)[0]
    assert harness.segments_by_seq(harness.archive("news"))[5]["captions_empty"] is True
    assert len(stored["judgments"]) == 2
    assert stored["judgments"][-1]["conclusion"] == "not_found"
    assert stored["judgments"][-1]["uncovered"] == []


def test_unchanged_judgment_is_not_appended_when_a_hole_after_it_heals(harness):
    harness.set_gap(8, True)
    harness.capture_channel("news")
    register(harness, "新闻｜第6段｜现场连线", 36, 54)
    assert len(list_verifications(harness)[0]["judgments"]) == 1

    harness.set_gap(8, False)
    harness.capture_channel("news")
    stored = list_verifications(harness)[0]
    # Conclusion and first-seen did not change, so the uncovered-span change
    # alone does not create a new judgment.
    assert len(stored["judgments"]) == 1
    assert stored["judgments"][0]["conclusion"] == "confirmed"


def test_judgment_history_survives_restart(harness):
    harness.capture_channel("news")
    register(harness, "新闻｜第6段｜现场连线", 36, 48)
    harness.restart()
    stored = list_verifications(harness)
    assert len(stored) == 1
    assert stored[0]["judgments"][0]["first_seen_ms"] == at(39.3)


def test_failed_judgment_write_rolls_back_the_capture(harness, monkeypatch):
    harness.set_gap(6, True)
    harness.capture_channel("news")  # #6 pending
    register(harness, "新闻｜跨段字幕｜第6段起", 36, 54)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated judgment write failure")

    monkeypatch.setattr(db_module.Database, "_insert_judgment", boom)
    harness.set_gap(6, False)
    with pytest.raises(RuntimeError):
        harness.client.post("/api/channels/news/capture")
    monkeypatch.undo()

    # Neither the segment nor its cues became visible, and no judgment was
    # appended for the capture that failed.
    segment = harness.db.get_segment("news", 6)
    assert segment.status == "pending"
    assert harness.archive("news")["counts"]["captured"] == 7
    assert len(list_verifications(harness)[0]["judgments"]) == 1

    # A retry then applies the capture and the judgment together, once.
    harness.capture_channel("news")
    assert harness.db.get_segment("news", 6).status == "captured"
    stored = list_verifications(harness)[0]
    assert [j["conclusion"] for j in stored["judgments"]] == [
        "unverified",
        "confirmed",
    ]


def test_verification_validation(harness):
    bad_channel = harness.client.post(
        "/api/verifications",
        json={
            "channel_id": "nope",
            "phrase": "x",
            "from_at": iso(0),
            "to_at": iso(10),
        },
    )
    assert bad_channel.status_code == 404

    empty = harness.client.post(
        "/api/verifications",
        json={"channel_id": "news", "phrase": "", "from_at": iso(0), "to_at": iso(10)},
    )
    assert empty.status_code == 400

    backwards = harness.client.post(
        "/api/verifications",
        json={"channel_id": "news", "phrase": "x", "from_at": iso(10), "to_at": iso(10)},
    )
    assert backwards.status_code == 400

    assert harness.client.get("/api/verifications/9999").status_code == 404


def test_channel_scoped_listing(harness):
    harness.capture_channel("news")
    harness.capture_channel("sports")
    register(harness, "体育｜第6段｜现场连线", 4 * 3600 + 36, 4 * 3600 + 48, "sports")
    assert list_verifications(harness, "news") == []
    sports = list_verifications(harness, "sports")
    assert len(sports) == 1
    assert sports[0]["current"]["conclusion"] == "confirmed"
    assert sports[0]["current"]["first_seen_seq"] == 6
