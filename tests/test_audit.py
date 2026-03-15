import json

from velora.audit import (
    CI_RESULT,
    COORDINATOR_DECISION,
    RUN_END,
    REVIEW_RESULT,
    RUN_STARTED,
    RUN_TERMINAL,
    WORKER_COMPLETED,
    WORK_ITEM_DISPATCHED,
    AuditEvent,
    append_event,
    generate_summary,
    load_events,
    write_summary,
)


def _event(
    *,
    run_id: str,
    iteration: int,
    event_type: str,
    timestamp: str,
    payload: dict,
) -> AuditEvent:
    return AuditEvent(
        run_id=run_id,
        iteration=iteration,
        event_type=event_type,
        timestamp=timestamp,
        payload=payload,
    )


def test_append_event_writes_valid_jsonl_and_is_append_only(tmp_path):
    run_id = "run-123"
    event = _event(
        run_id=run_id,
        iteration=1,
        event_type=RUN_STARTED,
        timestamp="2026-03-09T00:00:00+00:00",
        payload={"repo": "octocat/velora", "branch": "velora/run-123"},
    )

    path = append_event(run_id, event, base_dir=tmp_path)
    append_event(run_id, event, base_dir=tmp_path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first == second
    assert first["run_id"] == run_id
    assert first["event_type"] == RUN_STARTED
    assert path == tmp_path / ".velora" / "runs" / run_id / "audit.jsonl"


def test_load_events_round_trips_appended_data(tmp_path):
    run_id = "run-456"
    events = [
        _event(
            run_id=run_id,
            iteration=1,
            event_type=RUN_STARTED,
            timestamp="2026-03-09T00:00:00+00:00",
            payload={"repo": "octocat/velora", "branch": "velora/run-456"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=COORDINATOR_DECISION,
            timestamp="2026-03-09T00:00:05+00:00",
            payload={"decision": "execute_work_item"},
        ),
    ]
    for event in events:
        append_event(run_id, event, base_dir=tmp_path)

    loaded = load_events(run_id, base_dir=tmp_path)
    assert loaded == events


def test_generate_summary_contains_expected_sections_for_known_sequence():
    run_id = "run-789"
    events = [
        _event(
            run_id=run_id,
            iteration=1,
            event_type=RUN_STARTED,
            timestamp="2026-03-09T00:00:00+00:00",
            payload={"repo": "octocat/velora", "branch": "velora/run-789"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=WORK_ITEM_DISPATCHED,
            timestamp="2026-03-09T00:00:10+00:00",
            payload={
                "work_item_id": "WI-0001",
                "kind": "implement",
                "runner": "codex",
                "backend": "direct-codex",
            },
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=WORKER_COMPLETED,
            timestamp="2026-03-09T00:00:20+00:00",
            payload={"work_item_id": "WI-0001"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=CI_RESULT,
            timestamp="2026-03-09T00:00:30+00:00",
            payload={"status": "success"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=REVIEW_RESULT,
            timestamp="2026-03-09T00:00:40+00:00",
            payload={"status": "approved"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=RUN_TERMINAL,
            timestamp="2026-03-09T00:00:50+00:00",
            payload={"status": "completed"},
        ),
    ]

    summary = generate_summary(events)
    assert "Run ID: `run-789`" in summary
    assert "Repo: `octocat/velora`" in summary
    assert "Branch: `velora/run-789`" in summary
    assert "Iterations touched: `1`" in summary
    assert "`WI-0001` kind=`implement` runner=`codex` backend=`direct-codex` outcome=`completed`" in summary
    assert "CI result: `success`" in summary
    assert "Review result: `approved`" in summary
    assert "Terminal status: `completed`" in summary
    assert "event=`run_terminal`" in summary


def test_write_summary_overwrites_existing_file(tmp_path):
    run_id = "run-overwrite"

    first = [_event(run_id=run_id, iteration=1, event_type=RUN_STARTED, timestamp="2026-03-09T00:00:00+00:00", payload={})]
    path = write_summary(run_id, first, base_dir=tmp_path)
    first_text = path.read_text(encoding="utf-8")
    assert "Iterations touched: `1`" in first_text

    second = [
        _event(
            run_id=run_id,
            iteration=2,
            event_type=RUN_STARTED,
            timestamp="2026-03-09T00:00:10+00:00",
            payload={"repo": "octocat/velora", "branch": "velora/run-overwrite"},
        )
    ]
    write_summary(run_id, second, base_dir=tmp_path)
    second_text = path.read_text(encoding="utf-8")
    assert "Branch: `velora/run-overwrite`" in second_text
    assert second_text != first_text


def test_multi_iteration_summary_includes_both_iterations_and_work_items(tmp_path):
    run_id = "run-multi"
    events = [
        _event(
            run_id=run_id,
            iteration=1,
            event_type=RUN_STARTED,
            timestamp="2026-03-09T00:00:00+00:00",
            payload={"repo": "octocat/velora", "branch": "velora/run-multi"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=COORDINATOR_DECISION,
            timestamp="2026-03-09T00:00:05+00:00",
            payload={"decision": "execute_work_item"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=WORK_ITEM_DISPATCHED,
            timestamp="2026-03-09T00:00:06+00:00",
            payload={"work_item_id": "WI-0001", "kind": "implement", "runner": "codex", "backend": "direct-codex"},
        ),
        _event(
            run_id=run_id,
            iteration=1,
            event_type=WORKER_COMPLETED,
            timestamp="2026-03-09T00:00:20+00:00",
            payload={"work_item_id": "WI-0001"},
        ),
        _event(
            run_id=run_id,
            iteration=2,
            event_type=COORDINATOR_DECISION,
            timestamp="2026-03-09T00:01:00+00:00",
            payload={"decision": "execute_work_item"},
        ),
        _event(
            run_id=run_id,
            iteration=2,
            event_type=WORK_ITEM_DISPATCHED,
            timestamp="2026-03-09T00:01:03+00:00",
            payload={"work_item_id": "WI-0002", "kind": "test", "runner": "codex", "backend": "direct-codex"},
        ),
        _event(
            run_id=run_id,
            iteration=2,
            event_type=WORKER_COMPLETED,
            timestamp="2026-03-09T00:01:20+00:00",
            payload={"work_item_id": "WI-0002"},
        ),
        _event(
            run_id=run_id,
            iteration=2,
            event_type=RUN_TERMINAL,
            timestamp="2026-03-09T00:01:30+00:00",
            payload={"status": "completed"},
        ),
    ]

    path = write_summary(run_id, events, base_dir=tmp_path)
    summary = path.read_text(encoding="utf-8")
    assert "Iterations touched: `1, 2`" in summary
    assert "`WI-0001`" in summary
    assert "`WI-0002`" in summary


def test_append_event_schema_and_redaction(tmp_path):
    run_id = "run-secret"
    event = _event(
        run_id=run_id,
        iteration=1,
        event_type=RUN_END,
        timestamp="2026-03-09T00:00:50+00:00",
        payload={"status": "failed", "api_key": "sk-secret-value", "auth_header": "Bearer abc"},
    )
    path = append_event(run_id, event, base_dir=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert set(payload.keys()) == {"event_type", "iteration", "payload", "run_id", "timestamp"}
    assert payload["payload"]["api_key"] == "[REDACTED]"
    assert payload["payload"]["auth_header"] == "[REDACTED]"


def test_review_requested_event_constant():
    from velora.audit import REVIEW_REQUESTED
    assert REVIEW_REQUESTED == "review_requested"


def test_finding_dismissed_event_constant():
    from velora.audit import FINDING_DISMISSED
    assert FINDING_DISMISSED == "finding_dismissed"
