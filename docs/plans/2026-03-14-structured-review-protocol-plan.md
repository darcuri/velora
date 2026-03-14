# Structured Review Protocol and Orchestrator State Machine Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the prose-dependent review gate with protocol-driven review objects and refactor the orchestrator from a procedural monolith into a state machine.

**Architecture:** New protocol objects (ReviewBrief, ReviewFinding, ReviewResult, FindingDismissal) in `protocol.py` with strict validation. Coordinator decision vocabulary expands from 3 to 5 decisions. `run_task_mode_a` in `run.py` is refactored from a 900-line procedural function into a state machine with discrete handler functions dispatched from a ~10-line main loop.

**Tech Stack:** Python 3.12+, unittest, no new dependencies.

**Design doc:** `docs/plans/2026-03-14-structured-review-protocol-design.md`

---

## Task 1: Add ReviewScope and ReviewBrief protocol objects

**Files:**
- Modify: `velora/protocol.py` (after `WorkItemCommit` class, ~line 168)
- Test: `tests/test_protocol.py`

**Step 1: Write the failing tests**

Add to `tests/test_protocol.py`:

```python
from velora.protocol import ProtocolError, validate_coordinator_response, validate_review_brief


def _valid_review_brief() -> dict:
    return {
        "id": "RB-0001",
        "reviewer": "gemini",
        "model": None,
        "objective": "Verify the new endpoint handles auth errors correctly.",
        "acceptance_criteria": ["All unit tests pass", "No regressions in existing endpoints"],
        "rejection_criteria": ["No new dependencies introduced"],
        "areas_of_concern": ["Error handling in auth middleware"],
        "scope": {
            "kind": "full_diff",
            "base_ref": "main",
            "head_sha": "abc123",
            "files": [],
        },
    }


class TestReviewBriefProtocol(unittest.TestCase):
    def test_valid_review_brief(self) -> None:
        brief = validate_review_brief(_valid_review_brief())
        self.assertEqual(brief.id, "RB-0001")
        self.assertEqual(brief.reviewer, "gemini")
        self.assertIsNone(brief.model)
        self.assertEqual(brief.scope.kind, "full_diff")

    def test_review_brief_with_model_override(self) -> None:
        payload = _valid_review_brief()
        payload["model"] = "gemini-3-flash-preview"
        brief = validate_review_brief(payload)
        self.assertEqual(brief.model, "gemini-3-flash-preview")

    def test_review_brief_files_scope(self) -> None:
        payload = _valid_review_brief()
        payload["scope"]["kind"] = "files"
        payload["scope"]["files"] = ["velora/run.py", "velora/protocol.py"]
        brief = validate_review_brief(payload)
        self.assertEqual(brief.scope.kind, "files")
        self.assertEqual(len(brief.scope.files), 2)

    def test_review_brief_invalid_reviewer(self) -> None:
        payload = _valid_review_brief()
        payload["reviewer"] = "gpt"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_review_brief_invalid_scope_kind(self) -> None:
        payload = _valid_review_brief()
        payload["scope"]["kind"] = "partial"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_review_brief_missing_objective(self) -> None:
        payload = _valid_review_brief()
        del payload["objective"]
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_review_brief_unknown_keys_rejected(self) -> None:
        payload = _valid_review_brief()
        payload["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_review_brief_scope_unknown_keys_rejected(self) -> None:
        payload = _valid_review_brief()
        payload["scope"]["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_review_brief_empty_acceptance_criteria_allowed(self) -> None:
        payload = _valid_review_brief()
        payload["acceptance_criteria"] = []
        brief = validate_review_brief(payload)
        self.assertEqual(brief.acceptance_criteria, [])
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_protocol.py::TestReviewBriefProtocol -v`
Expected: FAIL — `validate_review_brief` does not exist yet.

**Step 3: Write minimal implementation**

Add to `velora/protocol.py`:

```python
_REVIEWER_BACKENDS = {"gemini", "claude"}
_REVIEW_SCOPE_KINDS = {"full_diff", "files"}


@dataclass(frozen=True)
class ReviewScope:
    kind: str
    base_ref: str
    head_sha: str
    files: list[str]

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "review_brief.scope") -> ReviewScope:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"kind", "base_ref", "head_sha", "files"})
        kind = _expect_enum(obj.get("kind"), ctx=f"{ctx}.kind", allowed=_REVIEW_SCOPE_KINDS)
        base_ref = _expect_str(obj.get("base_ref"), ctx=f"{ctx}.base_ref")
        head_sha = _expect_str(obj.get("head_sha"), ctx=f"{ctx}.head_sha")
        files_raw = _expect_list(obj.get("files"), ctx=f"{ctx}.files")
        files = [_expect_str(x, ctx=f"{ctx}.files[]") for x in files_raw]
        return ReviewScope(kind=kind, base_ref=base_ref, head_sha=head_sha, files=files)


@dataclass(frozen=True)
class ReviewBrief:
    id: str
    reviewer: str
    model: str | None
    objective: str
    acceptance_criteria: list[str]
    rejection_criteria: list[str]
    areas_of_concern: list[str]
    scope: ReviewScope

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "review_brief") -> ReviewBrief:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(
            obj,
            ctx=ctx,
            allowed_keys={
                "id", "reviewer", "model", "objective",
                "acceptance_criteria", "rejection_criteria",
                "areas_of_concern", "scope",
            },
        )
        bid = _expect_str(obj.get("id"), ctx=f"{ctx}.id")
        reviewer = _expect_enum(obj.get("reviewer"), ctx=f"{ctx}.reviewer", allowed=_REVIEWER_BACKENDS)
        model = obj.get("model")
        if model is not None:
            model = _expect_str(model, ctx=f"{ctx}.model")
        objective = _expect_str(obj.get("objective"), ctx=f"{ctx}.objective")
        acceptance_raw = _expect_list(obj.get("acceptance_criteria"), ctx=f"{ctx}.acceptance_criteria")
        acceptance_criteria = [_expect_str(x, ctx=f"{ctx}.acceptance_criteria[]") for x in acceptance_raw]
        rejection_raw = _expect_list(obj.get("rejection_criteria"), ctx=f"{ctx}.rejection_criteria")
        rejection_criteria = [_expect_str(x, ctx=f"{ctx}.rejection_criteria[]") for x in rejection_raw]
        areas_raw = _expect_list(obj.get("areas_of_concern"), ctx=f"{ctx}.areas_of_concern")
        areas_of_concern = [_expect_str(x, ctx=f"{ctx}.areas_of_concern[]") for x in areas_raw]
        scope = ReviewScope.from_dict(obj.get("scope"), ctx=f"{ctx}.scope")
        return ReviewBrief(
            id=bid, reviewer=reviewer, model=model, objective=objective,
            acceptance_criteria=acceptance_criteria, rejection_criteria=rejection_criteria,
            areas_of_concern=areas_of_concern, scope=scope,
        )


def validate_review_brief(payload: object) -> ReviewBrief:
    return ReviewBrief.from_dict(payload)
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_protocol.py::TestReviewBriefProtocol -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add velora/protocol.py tests/test_protocol.py
git commit -m "feat(protocol): add ReviewScope and ReviewBrief protocol objects"
```

---

## Task 2: Add ReviewFinding and ReviewResult protocol objects

**Files:**
- Modify: `velora/protocol.py` (after ReviewBrief class)
- Test: `tests/test_protocol.py`

**Step 1: Write the failing tests**

Add to `tests/test_protocol.py`:

```python
from velora.protocol import validate_review_result


def _valid_review_result() -> dict:
    return {
        "review_brief_id": "RB-0001",
        "verdict": "reject",
        "findings": [
            {
                "id": "RF-001",
                "severity": "blocker",
                "category": "correctness",
                "location": "velora/run.py:42",
                "description": "Missing null check causes crash on empty input.",
                "criterion_id": 0,
            },
            {
                "id": "RF-002",
                "severity": "nit",
                "category": "style",
                "location": "velora/run.py:50",
                "description": "Variable name could be more descriptive.",
                "criterion_id": None,
            },
        ],
        "summary": "One blocker found: missing null check.",
    }


class TestReviewResultProtocol(unittest.TestCase):
    def test_valid_reject_review_result(self) -> None:
        result = validate_review_result(_valid_review_result())
        self.assertEqual(result.verdict, "reject")
        self.assertEqual(len(result.findings), 2)
        self.assertEqual(result.findings[0].severity, "blocker")
        self.assertEqual(result.findings[0].criterion_id, 0)
        self.assertEqual(result.findings[1].severity, "nit")
        self.assertIsNone(result.findings[1].criterion_id)

    def test_valid_approve_review_result(self) -> None:
        payload = {
            "review_brief_id": "RB-0001",
            "verdict": "approve",
            "findings": [],
            "summary": "No issues found.",
        }
        result = validate_review_result(payload)
        self.assertEqual(result.verdict, "approve")
        self.assertEqual(result.findings, [])

    def test_approve_with_nits_allowed(self) -> None:
        payload = {
            "review_brief_id": "RB-0001",
            "verdict": "approve",
            "findings": [
                {
                    "id": "RF-001",
                    "severity": "nit",
                    "category": "style",
                    "location": "",
                    "description": "Minor style issue.",
                    "criterion_id": None,
                },
            ],
            "summary": "Approved with minor nits.",
        }
        result = validate_review_result(payload)
        self.assertEqual(result.verdict, "approve")
        self.assertEqual(len(result.findings), 1)

    def test_approve_with_blocker_is_protocol_error(self) -> None:
        payload = {
            "review_brief_id": "RB-0001",
            "verdict": "approve",
            "findings": [
                {
                    "id": "RF-001",
                    "severity": "blocker",
                    "category": "correctness",
                    "location": "",
                    "description": "Serious issue.",
                    "criterion_id": None,
                },
            ],
            "summary": "Approved.",
        }
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_reject_without_blocker_is_protocol_error(self) -> None:
        payload = {
            "review_brief_id": "RB-0001",
            "verdict": "reject",
            "findings": [
                {
                    "id": "RF-001",
                    "severity": "nit",
                    "category": "style",
                    "location": "",
                    "description": "Minor.",
                    "criterion_id": None,
                },
            ],
            "summary": "Rejected.",
        }
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_invalid_verdict_is_protocol_error(self) -> None:
        payload = _valid_review_result()
        payload["verdict"] = "maybe"
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_invalid_severity_is_protocol_error(self) -> None:
        payload = _valid_review_result()
        payload["findings"][0]["severity"] = "warning"
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_invalid_category_is_protocol_error(self) -> None:
        payload = _valid_review_result()
        payload["findings"][0]["category"] = "performance"
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_unknown_keys_in_finding_rejected(self) -> None:
        payload = _valid_review_result()
        payload["findings"][0]["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_unknown_keys_in_result_rejected(self) -> None:
        payload = _valid_review_result()
        payload["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_protocol.py::TestReviewResultProtocol -v`
Expected: FAIL — `validate_review_result` does not exist yet.

**Step 3: Write minimal implementation**

Add to `velora/protocol.py`:

```python
_REVIEW_VERDICTS = {"approve", "reject"}
_FINDING_SEVERITIES = {"blocker", "nit"}
_FINDING_CATEGORIES = {"correctness", "security", "regression", "style", "docs"}


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    severity: str
    category: str
    location: str
    description: str
    criterion_id: int | None

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "ReviewResult.findings[]") -> ReviewFinding:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(
            obj, ctx=ctx,
            allowed_keys={"id", "severity", "category", "location", "description", "criterion_id"},
        )
        fid = _expect_str(obj.get("id"), ctx=f"{ctx}.id")
        severity = _expect_enum(obj.get("severity"), ctx=f"{ctx}.severity", allowed=_FINDING_SEVERITIES)
        category = _expect_enum(obj.get("category"), ctx=f"{ctx}.category", allowed=_FINDING_CATEGORIES)
        location = _expect_str(obj.get("location"), ctx=f"{ctx}.location", non_empty=False)
        description = _expect_str(obj.get("description"), ctx=f"{ctx}.description")
        criterion_id = obj.get("criterion_id")
        if criterion_id is not None:
            criterion_id = _expect_int(criterion_id, ctx=f"{ctx}.criterion_id")
        return ReviewFinding(
            id=fid, severity=severity, category=category,
            location=location, description=description, criterion_id=criterion_id,
        )


@dataclass(frozen=True)
class ReviewResult:
    review_brief_id: str
    verdict: str
    findings: list[ReviewFinding]
    summary: str

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "ReviewResult") -> ReviewResult:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"review_brief_id", "verdict", "findings", "summary"})
        review_brief_id = _expect_str(obj.get("review_brief_id"), ctx=f"{ctx}.review_brief_id")
        verdict = _expect_enum(obj.get("verdict"), ctx=f"{ctx}.verdict", allowed=_REVIEW_VERDICTS)
        findings_raw = _expect_list(obj.get("findings"), ctx=f"{ctx}.findings")
        findings = [ReviewFinding.from_dict(x, ctx=f"{ctx}.findings[]") for x in findings_raw]
        summary = _expect_str(obj.get("summary"), ctx=f"{ctx}.summary")

        has_blocker = any(f.severity == "blocker" for f in findings)
        if verdict == "approve" and has_blocker:
            raise ProtocolError(f"{ctx}: verdict=approve but findings contain blocker-severity finding(s)")
        if verdict == "reject" and not has_blocker:
            raise ProtocolError(f"{ctx}: verdict=reject requires at least one blocker-severity finding")

        return ReviewResult(
            review_brief_id=review_brief_id, verdict=verdict,
            findings=findings, summary=summary,
        )


def validate_review_result(payload: object) -> ReviewResult:
    return ReviewResult.from_dict(payload)
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_protocol.py::TestReviewResultProtocol -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add velora/protocol.py tests/test_protocol.py
git commit -m "feat(protocol): add ReviewFinding and ReviewResult protocol objects"
```

---

## Task 3: Add FindingDismissal protocol object

**Files:**
- Modify: `velora/protocol.py` (after ReviewResult class)
- Test: `tests/test_protocol.py`

**Step 1: Write the failing tests**

Add to `tests/test_protocol.py`:

```python
from velora.protocol import validate_finding_dismissal


class TestFindingDismissalProtocol(unittest.TestCase):
    def test_valid_dismissal(self) -> None:
        payload = {
            "finding_ids": ["RF-001", "RF-002"],
            "justification": "These are style nits that do not affect correctness.",
        }
        dismissal = validate_finding_dismissal(payload)
        self.assertEqual(dismissal.finding_ids, ["RF-001", "RF-002"])
        self.assertEqual(dismissal.justification, "These are style nits that do not affect correctness.")

    def test_empty_finding_ids_is_protocol_error(self) -> None:
        payload = {
            "finding_ids": [],
            "justification": "Nothing to dismiss.",
        }
        with self.assertRaises(ProtocolError):
            validate_finding_dismissal(payload)

    def test_empty_justification_is_protocol_error(self) -> None:
        payload = {
            "finding_ids": ["RF-001"],
            "justification": "",
        }
        with self.assertRaises(ProtocolError):
            validate_finding_dismissal(payload)

    def test_unknown_keys_rejected(self) -> None:
        payload = {
            "finding_ids": ["RF-001"],
            "justification": "Reason.",
            "extra": "nope",
        }
        with self.assertRaises(ProtocolError):
            validate_finding_dismissal(payload)
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_protocol.py::TestFindingDismissalProtocol -v`
Expected: FAIL — `validate_finding_dismissal` does not exist yet.

**Step 3: Write minimal implementation**

Add to `velora/protocol.py`:

```python
@dataclass(frozen=True)
class FindingDismissal:
    finding_ids: list[str]
    justification: str

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "finding_dismissal") -> FindingDismissal:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"finding_ids", "justification"})
        ids_raw = _expect_list(obj.get("finding_ids"), ctx=f"{ctx}.finding_ids")
        finding_ids = [_expect_str(x, ctx=f"{ctx}.finding_ids[]") for x in ids_raw]
        if not finding_ids:
            raise ProtocolError(f"{ctx}.finding_ids must contain at least one finding ID")
        justification = _expect_str(obj.get("justification"), ctx=f"{ctx}.justification")
        return FindingDismissal(finding_ids=finding_ids, justification=justification)


def validate_finding_dismissal(payload: object) -> FindingDismissal:
    return FindingDismissal.from_dict(payload)
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_protocol.py::TestFindingDismissalProtocol -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add velora/protocol.py tests/test_protocol.py
git commit -m "feat(protocol): add FindingDismissal protocol object"
```

---

## Task 4: Expand CoordinatorResponse for new decisions

**Files:**
- Modify: `velora/protocol.py` (the `CoordinatorResponse` class, ~line 249 and `_DECISIONS` set at line 24)
- Test: `tests/test_protocol.py`

**Step 1: Write the failing tests**

Add to `tests/test_protocol.py`:

```python
class TestExpandedCoordinatorDecisions(unittest.TestCase):
    def test_request_review_valid(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "request_review",
            "reason": "CI passed; requesting structured review.",
            "selected_specialist": {"role": "reviewer", "runner": "gemini"},
            "review_brief": _valid_review_brief(),
        }
        resp = validate_coordinator_response(payload)
        self.assertEqual(resp.decision, "request_review")
        self.assertIsNotNone(resp.review_brief)
        self.assertIsNone(resp.work_item)
        self.assertIsNone(resp.finding_dismissal)

    def test_request_review_missing_brief_is_protocol_error(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "request_review",
            "reason": "Review needed.",
            "selected_specialist": {"role": "reviewer", "runner": "gemini"},
        }
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_request_review_with_work_item_is_protocol_error(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "request_review",
            "reason": "Review needed.",
            "selected_specialist": {"role": "reviewer", "runner": "gemini"},
            "review_brief": _valid_review_brief(),
            "work_item": _valid_execute_payload()["work_item"],
        }
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_dismiss_finding_valid(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "dismiss_finding",
            "reason": "Findings are cosmetic nits, not blockers.",
            "selected_specialist": {"role": "reviewer", "runner": "claude"},
            "finding_dismissal": {
                "finding_ids": ["RF-001"],
                "justification": "Style-only issue, does not affect correctness.",
            },
        }
        resp = validate_coordinator_response(payload)
        self.assertEqual(resp.decision, "dismiss_finding")
        self.assertIsNotNone(resp.finding_dismissal)
        self.assertIsNone(resp.work_item)
        self.assertIsNone(resp.review_brief)

    def test_dismiss_finding_missing_dismissal_is_protocol_error(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "dismiss_finding",
            "reason": "Dismiss.",
            "selected_specialist": {"role": "reviewer", "runner": "claude"},
        }
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_execute_work_item_with_review_brief_is_protocol_error(self) -> None:
        payload = _valid_execute_payload()
        payload["review_brief"] = _valid_review_brief()
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_finalize_with_review_brief_is_protocol_error(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "Done.",
            "selected_specialist": {"role": "investigator", "runner": "claude"},
            "review_brief": _valid_review_brief(),
        }
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_reviewer_role_accepted(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "request_review",
            "reason": "Review.",
            "selected_specialist": {"role": "reviewer", "runner": "gemini"},
            "review_brief": _valid_review_brief(),
        }
        resp = validate_coordinator_response(payload)
        self.assertEqual(resp.selected_specialist.role, "reviewer")
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_protocol.py::TestExpandedCoordinatorDecisions -v`
Expected: FAIL — `request_review` not in `_DECISIONS`, `reviewer` not in `_SPECIALIST_ROLES`, etc.

**Step 3: Write the implementation**

Modify `velora/protocol.py`:

1. Update the enum sets at the top of the file:

```python
_DECISIONS = {"execute_work_item", "request_review", "dismiss_finding", "finalize_success", "stop_failure"}
_SPECIALIST_ROLES = {"implementer", "docs", "refactor", "investigator", "reviewer"}
_ALLOWED_RUNNERS = {"codex", "claude", "gemini"}
```

Note: `gemini` is added to `_ALLOWED_RUNNERS` because it is now a valid runner for the `reviewer` role. The specialist matrix enforcement will prevent it from being used for code-writing roles.

2. Rewrite `CoordinatorResponse.from_dict` to handle the three payload-bearing decisions:

```python
@dataclass(frozen=True)
class CoordinatorResponse:
    protocol_version: int
    decision: str
    reason: str
    selected_specialist: SelectedSpecialist
    work_item: WorkItem | None = None
    review_brief: ReviewBrief | None = None
    finding_dismissal: FindingDismissal | None = None

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "CoordinatorResponse") -> CoordinatorResponse:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(
            obj, ctx=ctx,
            allowed_keys={
                "protocol_version", "decision", "reason", "selected_specialist",
                "work_item", "review_brief", "finding_dismissal",
            },
        )

        protocol_version = _expect_int(obj.get("protocol_version"), ctx=f"{ctx}.protocol_version")
        if protocol_version != 1:
            raise ProtocolError(f"{ctx}.protocol_version must be 1")

        decision = _expect_enum(obj.get("decision"), ctx=f"{ctx}.decision", allowed=_DECISIONS)
        reason = _expect_str(obj.get("reason"), ctx=f"{ctx}.reason")
        selected_specialist = SelectedSpecialist.from_dict(
            obj.get("selected_specialist"), ctx=f"{ctx}.selected_specialist",
        )

        # Decision-specific payloads: exactly one must be present.
        _payload_fields = {
            "execute_work_item": "work_item",
            "request_review": "review_brief",
            "dismiss_finding": "finding_dismissal",
        }
        required_field = _payload_fields.get(decision)
        for field_name in ("work_item", "review_brief", "finding_dismissal"):
            raw_value = obj.get(field_name)
            if field_name == required_field:
                if raw_value is None:
                    raise ProtocolError(f"{ctx}.{field_name} is required when decision={decision}")
            else:
                if raw_value is not None:
                    raise ProtocolError(f"{ctx}.{field_name} must be omitted when decision={decision}")

        work_item = None
        review_brief = None
        finding_dismissal = None

        if decision == "execute_work_item":
            work_item = WorkItem.from_dict(obj["work_item"], ctx=f"{ctx}.work_item")
        elif decision == "request_review":
            review_brief = ReviewBrief.from_dict(obj["review_brief"], ctx=f"{ctx}.review_brief")
        elif decision == "dismiss_finding":
            finding_dismissal = FindingDismissal.from_dict(
                obj["finding_dismissal"], ctx=f"{ctx}.finding_dismissal",
            )

        return CoordinatorResponse(
            protocol_version=protocol_version,
            decision=decision,
            reason=reason,
            selected_specialist=selected_specialist,
            work_item=work_item,
            review_brief=review_brief,
            finding_dismissal=finding_dismissal,
        )
```

**Step 4: Run ALL protocol tests (old + new)**

Run: `python -m pytest tests/test_protocol.py -v`
Expected: all PASS. Old tests still pass — existing decisions unchanged.

**Step 5: Run the full test suite for regressions**

Run: `python -m unittest discover -s tests -p 'test_*.py'`
Expected: all 147+ tests PASS.

**Step 6: Commit**

```bash
git add velora/protocol.py tests/test_protocol.py
git commit -m "feat(protocol): expand CoordinatorResponse with request_review and dismiss_finding decisions"
```

---

## Task 5: Add new audit event types

**Files:**
- Modify: `velora/audit.py` (add constants after existing event type constants)
- Test: `tests/test_audit.py`

**Step 1: Write the failing test**

Add to `tests/test_audit.py`:

```python
class TestNewAuditEventTypes(unittest.TestCase):
    def test_review_requested_event_constant_exists(self) -> None:
        from velora.audit import REVIEW_REQUESTED
        self.assertEqual(REVIEW_REQUESTED, "review_requested")

    def test_finding_dismissed_event_constant_exists(self) -> None:
        from velora.audit import FINDING_DISMISSED
        self.assertEqual(FINDING_DISMISSED, "finding_dismissed")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit.py::TestNewAuditEventTypes -v`
Expected: FAIL — `REVIEW_REQUESTED` does not exist.

**Step 3: Write minimal implementation**

Add to `velora/audit.py` after existing event constants:

```python
REVIEW_REQUESTED = "review_requested"
FINDING_DISMISSED = "finding_dismissed"
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit.py -v`
Expected: all PASS.

**Step 5: Commit**

```bash
git add velora/audit.py tests/test_audit.py
git commit -m "feat(audit): add REVIEW_REQUESTED and FINDING_DISMISSED event types"
```

---

## Task 6: Refactor run_task_mode_a into state machine — RunContext and state enum

This is the first step of the orchestrator refactor. It introduces the `RunContext` dataclass and state constants without changing behavior yet.

**Files:**
- Modify: `velora/run.py` (add RunContext and state constants before `run_task_mode_a`)
- Test: `tests/test_state_machine.py` (new file)

**Step 1: Write the failing test**

Create `tests/test_state_machine.py`:

```python
import unittest
from velora.run import OrchestratorState, RunContext


class TestRunContextAndStates(unittest.TestCase):
    def test_all_states_defined(self) -> None:
        expected = {
            "PREFLIGHT", "AWAITING_DECISION", "DISPATCHING_WORKER",
            "POLLING_CI", "DISPATCHING_REVIEW", "PROCESSING_DISMISSAL",
            "TERMINAL", "DONE",
        }
        for state_name in expected:
            self.assertTrue(
                hasattr(OrchestratorState, state_name),
                f"OrchestratorState missing {state_name}",
            )

    def test_run_context_construction(self) -> None:
        from pathlib import Path
        from unittest.mock import MagicMock
        ctx = RunContext(
            task_id="t1", run_id="r1", repo_ref="o/r", verb="feature",
            owner="o", repo="r", base_branch="main", work_branch="velora/t1",
            repo_path=Path("/tmp"), config=MagicMock(),
            max_attempts=3, max_tokens=100000, max_wall_seconds=600,
            no_progress_max=3, review_enabled=True,
            iteration=1, record={}, request={},
            active_review_result=None,
            gh=MagicMock(), home=Path("/tmp"), task_dir=Path("/tmp/tasks/t1"),
            debug=False, loop_start=0.0,
        )
        self.assertEqual(ctx.task_id, "t1")
        self.assertEqual(ctx.iteration, 1)
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_state_machine.py -v`
Expected: FAIL — `OrchestratorState` and `RunContext` do not exist.

**Step 3: Write minimal implementation**

Add to `velora/run.py` before `run_task_mode_a`:

```python
import enum


class OrchestratorState(enum.Enum):
    PREFLIGHT = "PREFLIGHT"
    AWAITING_DECISION = "AWAITING_DECISION"
    DISPATCHING_WORKER = "DISPATCHING_WORKER"
    POLLING_CI = "POLLING_CI"
    DISPATCHING_REVIEW = "DISPATCHING_REVIEW"
    PROCESSING_DISMISSAL = "PROCESSING_DISMISSAL"
    TERMINAL = "TERMINAL"
    DONE = "DONE"


@dataclass
class RunContext:
    # Identity
    task_id: str
    run_id: str
    repo_ref: str
    verb: str

    # Repo
    owner: str
    repo: str
    base_branch: str
    work_branch: str
    repo_path: Path

    # Config / policy
    config: Any
    max_attempts: int
    max_tokens: int
    max_wall_seconds: int
    no_progress_max: int
    review_enabled: bool

    # Mutable state
    iteration: int
    record: dict[str, Any]
    request: dict[str, Any]
    active_review_result: Any  # ReviewResult | None

    # Infrastructure
    gh: Any  # GitHubClient
    home: Path
    task_dir: Path
    debug: bool
    loop_start: float
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_state_machine.py -v`
Expected: all PASS.

**Step 5: Run full suite for regressions**

Run: `python -m unittest discover -s tests -p 'test_*.py'`
Expected: all tests PASS — we only added new types, nothing changed.

**Step 6: Commit**

```bash
git add velora/run.py tests/test_state_machine.py
git commit -m "feat(run): add OrchestratorState enum and RunContext dataclass"
```

---

## Task 7: Extract state handlers from run_task_mode_a

This is the core refactor. Each section of the existing `run_task_mode_a` function becomes a state handler function. The main loop becomes a dispatcher.

This is the largest task. It is a **pure refactor** — behavior must not change. Every existing test must pass.

**Files:**
- Modify: `velora/run.py`
- Test: `tests/test_mode_a_work_result_integration.py` (existing tests must still pass)
- Test: `tests/test_state_machine.py` (new tests for state transitions)

**Step 1: Write transition tests**

Add to `tests/test_state_machine.py`:

```python
from unittest.mock import MagicMock, patch
from pathlib import Path
from velora.run import OrchestratorState, RunContext, run_task_mode_a_sm


class TestStateTransitions(unittest.TestCase):
    def test_finalize_success_reaches_terminal(self) -> None:
        """Coordinator returns finalize_success on first decision → TERMINAL → DONE."""
        from types import SimpleNamespace
        from velora.acpx import CmdResult
        from velora.protocol import validate_coordinator_response
        import os

        finalize_resp = validate_coordinator_response({
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "done",
            "selected_specialist": {"role": "investigator", "runner": "claude"},
        })
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", return_value=SimpleNamespace(
                response=finalize_resp, cmd=CmdResult(0, "", ""),
            )),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            from velora.spec import RunSpec
            result = run_task_mode_a_sm("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))
        self.assertEqual(result["status"], "ready")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_state_machine.py::TestStateTransitions -v`
Expected: FAIL — `run_task_mode_a_sm` does not exist.

**Step 3: Implement the state machine**

This is the core work. In `velora/run.py`:

1. Create handler functions by extracting sections from `run_task_mode_a`:
   - `_state_preflight(ctx) -> OrchestratorState` — lines 1467-1579 of current run.py (repo checkout, config, request building)
   - `_state_awaiting_decision(ctx) -> OrchestratorState` — lines 1583-1760 (breaker checks, coordinator call, decision routing)
   - `_state_dispatching_worker(ctx) -> OrchestratorState` — lines 1762-2057 (worker prompt, execution, WorkResult loading, branch publication)
   - `_state_polling_ci(ctx) -> OrchestratorState` — lines 2189-2357 (CI polling, infra classification, infra retry)
   - `_state_dispatching_review(ctx) -> OrchestratorState` — new handler, initially wraps existing review logic from lines 2359-2463
   - `_state_processing_dismissal(ctx) -> OrchestratorState` — new handler, validates FindingDismissal
   - `_state_terminal(ctx) -> OrchestratorState` — lines 1726-1760 (persist final state)

2. Create the dispatcher:

```python
_STATE_HANDLERS: dict[OrchestratorState, Any] = {
    OrchestratorState.PREFLIGHT: _state_preflight,
    OrchestratorState.AWAITING_DECISION: _state_awaiting_decision,
    OrchestratorState.DISPATCHING_WORKER: _state_dispatching_worker,
    OrchestratorState.POLLING_CI: _state_polling_ci,
    OrchestratorState.DISPATCHING_REVIEW: _state_dispatching_review,
    OrchestratorState.PROCESSING_DISMISSAL: _state_processing_dismissal,
    OrchestratorState.TERMINAL: _state_terminal,
}


def run_task_mode_a_sm(
    repo_ref: str,
    verb: str,
    spec: RunSpec,
    home: Path | None = None,
    base_branch: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    ctx = RunContext(...)  # initialized from args, same as current run_task_mode_a preamble
    state = OrchestratorState.PREFLIGHT
    while state != OrchestratorState.DONE:
        handler = _STATE_HANDLERS.get(state)
        if handler is None:
            raise RuntimeError(f"No handler for state {state}")
        state = handler(ctx)
    return ctx.result
```

3. Keep the old `run_task_mode_a` as-is temporarily — `run_task_mode_a_sm` is the new implementation. Wire `run_task` to call `run_task_mode_a_sm` once all tests pass.

**Step 4: Run ALL existing integration tests**

Run: `python -m unittest discover -s tests -p 'test_*.py'`
Expected: all tests PASS with identical behavior.

**Step 5: Swap run_task to use state machine, remove old function**

In `run_task()`, change `run_task_mode_a` call to `run_task_mode_a_sm`. Remove old `run_task_mode_a`. Rename `run_task_mode_a_sm` to `run_task_mode_a`.

**Step 6: Run ALL tests again**

Run: `python -m unittest discover -s tests -p 'test_*.py'`
Expected: all tests PASS.

**Step 7: Commit**

```bash
git add velora/run.py tests/test_state_machine.py
git commit -m "refactor(run): extract run_task_mode_a into state machine with handler functions"
```

---

## Task 8: Wire DISPATCHING_REVIEW and PROCESSING_DISMISSAL into state machine

Now that the state machine exists, wire the new coordinator decisions (`request_review`, `dismiss_finding`) into the `_state_awaiting_decision` handler so they route to the correct state handlers.

**Files:**
- Modify: `velora/run.py` (`_state_awaiting_decision`, `_state_dispatching_review`, `_state_processing_dismissal`)
- Test: `tests/test_state_machine.py`

**Step 1: Write the failing tests**

Add to `tests/test_state_machine.py`:

```python
class TestReviewDecisionRouting(unittest.TestCase):
    def test_request_review_routes_to_dispatching_review(self) -> None:
        """Coordinator returns request_review → DISPATCHING_REVIEW state."""
        # Similar mock setup to TestStateTransitions but with request_review decision
        # and a review_brief payload. Assert the review runs and result flows back.
        ...

    def test_dismiss_finding_routes_to_processing_dismissal(self) -> None:
        """Coordinator returns dismiss_finding → PROCESSING_DISMISSAL state."""
        ...

    def test_review_enabled_blocks_finalize_without_review(self) -> None:
        """With review_enabled=True, finalize_success without prior review is protocol error."""
        ...
```

(Full test implementations follow the same mock pattern as Task 7 step 1, using the new protocol payloads from Task 4.)

**Step 2-4: Implement and verify**

The `_state_awaiting_decision` handler's decision routing expands from:

```python
if decision == "execute_work_item": return DISPATCHING_WORKER
elif decision in ("finalize_success", "stop_failure"): return TERMINAL
```

to:

```python
if decision == "execute_work_item": return DISPATCHING_WORKER
elif decision == "request_review": return DISPATCHING_REVIEW
elif decision == "dismiss_finding": return PROCESSING_DISMISSAL
elif decision in ("finalize_success", "stop_failure"): return TERMINAL
```

The `_state_dispatching_review` handler:
- Reads `ctx.active_review_brief` (set by awaiting_decision)
- Builds reviewer prompt from ReviewBrief fields
- Dispatches to reviewer (Gemini or Claude based on `review_brief.reviewer`)
- Validates ReviewResult
- Stores result in `ctx.active_review_result` and `ctx.request["state"]["latest_review_result"]`
- Returns `AWAITING_DECISION`

The `_state_processing_dismissal` handler:
- Reads `ctx.active_finding_dismissal` (set by awaiting_decision)
- Validates finding_ids against `ctx.active_review_result.findings`
- Records dismissal in audit trail
- Returns `AWAITING_DECISION`

**Step 5: Run ALL tests**

Run: `python -m unittest discover -s tests -p 'test_*.py'`
Expected: all PASS.

**Step 6: Commit**

```bash
git add velora/run.py tests/test_state_machine.py
git commit -m "feat(run): wire request_review and dismiss_finding decisions into state machine"
```

---

## Task 9: Refactor reviewer dispatch to accept ReviewBrief

**Files:**
- Modify: `velora/acpx.py` (refactor `run_gemini_review` and add `run_review`)
- Test: `tests/test_acpx.py`

**Step 1: Write the failing test**

Add to `tests/test_acpx.py`:

```python
class TestRunReview(unittest.TestCase):
    def test_run_review_dispatches_to_gemini(self) -> None:
        from velora.acpx import run_review
        from velora.protocol import ReviewBrief, ReviewScope
        brief = ReviewBrief(
            id="RB-0001", reviewer="gemini", model=None,
            objective="Check correctness.",
            acceptance_criteria=["Tests pass"], rejection_criteria=[],
            areas_of_concern=[],
            scope=ReviewScope(kind="full_diff", base_ref="main", head_sha="abc", files=[]),
        )
        # Mock the underlying Gemini API call, verify the prompt includes brief fields
        ...

    def test_run_review_prompt_includes_acceptance_criteria(self) -> None:
        ...

    def test_run_review_prompt_includes_areas_of_concern(self) -> None:
        ...
```

**Step 2-4: Implement `run_review` in `velora/acpx.py`**

- New function `run_review(brief: ReviewBrief, diff_text: str) -> CmdResult` that:
  - Builds prompt from ReviewBrief fields (objective, acceptance_criteria, rejection_criteria, areas_of_concern)
  - Instructs reviewer to output structured JSON matching ReviewResult schema
  - Dispatches to Gemini or Claude based on `brief.reviewer`
- Keep existing `run_gemini_review` for legacy mode compatibility.

**Step 5: Run tests**

Run: `python -m pytest tests/test_acpx.py -v`
Expected: all PASS.

**Step 6: Commit**

```bash
git add velora/acpx.py tests/test_acpx.py
git commit -m "feat(acpx): add run_review dispatcher that accepts ReviewBrief"
```

---

## Task 10: Update coordinator prompt template for new decisions

**Files:**
- Modify: `velora/coordinator.py` (`COORDINATOR_PROMPT_TEMPLATE_V1`)
- Test: `tests/test_coordinator.py`

**Step 1: Write the failing test**

Add to `tests/test_coordinator.py`:

```python
class TestExpandedCoordinatorPrompt(unittest.TestCase):
    def test_prompt_contains_request_review_schema(self) -> None:
        from velora.coordinator import render_coordinator_prompt_v1
        prompt = render_coordinator_prompt_v1({"protocol_version": 1, "run_id": "r1", "iteration": 1})
        self.assertIn("request_review", prompt)
        self.assertIn("review_brief", prompt)

    def test_prompt_contains_dismiss_finding_schema(self) -> None:
        from velora.coordinator import render_coordinator_prompt_v1
        prompt = render_coordinator_prompt_v1({"protocol_version": 1, "run_id": "r1", "iteration": 1})
        self.assertIn("dismiss_finding", prompt)
        self.assertIn("finding_dismissal", prompt)
```

**Step 2-4: Update the prompt template**

Add ReviewBrief and FindingDismissal schemas to the coordinator prompt output section, alongside the existing WorkItem schema. Document the new decisions and when each is appropriate.

**Step 5: Run tests**

Run: `python -m pytest tests/test_coordinator.py -v`
Expected: all PASS.

**Step 6: Commit**

```bash
git add velora/coordinator.py tests/test_coordinator.py
git commit -m "feat(coordinator): add request_review and dismiss_finding to coordinator prompt"
```

---

## Task 11: End-to-end integration test for the full review protocol flow

**Files:**
- Test: `tests/test_review_protocol_integration.py` (new file)

**Step 1: Write the integration test**

```python
class TestReviewProtocolIntegration(unittest.TestCase):
    def test_full_review_cycle_request_approve_finalize(self) -> None:
        """
        Coordinator: execute_work_item → worker completes → CI passes
        → Coordinator: request_review → reviewer approves
        → Coordinator: finalize_success
        """
        ...

    def test_full_review_cycle_request_reject_fix_rereview_finalize(self) -> None:
        """
        Coordinator: execute_work_item → worker completes → CI passes
        → Coordinator: request_review → reviewer rejects with blocker
        → Coordinator: execute_work_item (fix) → worker completes → CI passes
        → Coordinator: request_review → reviewer approves
        → Coordinator: finalize_success
        """
        ...

    def test_full_review_cycle_dismiss_finding_then_finalize(self) -> None:
        """
        Coordinator: execute_work_item → worker completes → CI passes
        → Coordinator: request_review → reviewer rejects with blocker
        → Coordinator: dismiss_finding → justification recorded
        → Coordinator: finalize_success
        """
        ...

    def test_review_enabled_blocks_finalize_without_review(self) -> None:
        """
        With review_enabled=True, coordinator cannot finalize_success
        without at least one request_review having occurred.
        """
        ...
```

These tests follow the same mock pattern as `test_mode_a_work_result_integration.py`, using mock coordinator responses that cycle through the new decisions.

**Step 2: Implement and verify**

Run: `python -m pytest tests/test_review_protocol_integration.py -v`
Expected: all PASS.

**Step 3: Run full suite**

Run: `python -m unittest discover -s tests -p 'test_*.py'`
Expected: all PASS.

**Step 4: Commit**

```bash
git add tests/test_review_protocol_integration.py
git commit -m "test: add end-to-end integration tests for structured review protocol flow"
```

---

## Task 12: Update docs and planning files

**Files:**
- Modify: `docs/mode-a-safety-rails.md` — add review protocol section
- Modify: `docs/cli.md` — document new audit event types
- Modify: `docs/plans/current-state.md` — update with new state
- Modify: `docs/plans/next-tasks.md` — update priorities

**Step 1: Update docs**

- `docs/mode-a-safety-rails.md`: Add section on structured review protocol — ReviewBrief, ReviewResult, FindingDismissal. Document that the coordinator owns review decisions, not the orchestrator.
- `docs/cli.md`: Document that `velora audit inspect` will show `review_requested` and `finding_dismissed` events.
- `docs/plans/current-state.md`: Update to reflect the state machine refactor and structured review protocol.
- `docs/plans/next-tasks.md`: Remove completed items (review robustness). Add new items (reviewer prompt tuning, specialist matrix for reviewer role, dogfood the review protocol).

**Step 2: Commit**

```bash
git add docs/
git commit -m "docs: update safety rails, CLI docs, and planning files for structured review protocol"
```

---

## Recommended execution order

Tasks 1–5 are independent protocol/audit additions — safe to parallelize.
Task 6 depends on nothing and sets up the state machine scaffolding.
Task 7 is the core refactor — depends on Task 6, must pass all existing tests.
Task 8 depends on Tasks 4 + 7 (new decisions + state machine).
Task 9 depends on Task 1 (ReviewBrief exists).
Task 10 depends on Tasks 1–3 (all protocol objects exist).
Task 11 depends on Tasks 7–10 (full stack wired up).
Task 12 depends on Task 11 (everything works).

```
[1] ReviewBrief ──┐
[2] ReviewResult ─┤
[3] FindingDismissal ─┤── [4] Expanded CoordinatorResponse ──┐
[5] Audit events ─────────────────────────────────────────────┤
[6] RunContext + State enum ── [7] State machine refactor ── [8] Wire new decisions ── [11] Integration tests ── [12] Docs
[9] Reviewer dispatch (needs 1) ──────────────────────────────┘
[10] Coordinator prompt (needs 1-3) ──────────────────────────┘
```
