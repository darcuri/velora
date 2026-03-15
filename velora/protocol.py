from __future__ import annotations

"""Protocol: Coordinator ↔ WorkItem (v1).

This module defines the JSON-serializable contract between the coordinator (control-plane)
model and Velora's execution engine.

Principles:
- Machine-parseable JSON only.
- Strict validation (protocol violations are hard-fail).
- Mode A: exactly one WorkItem per iteration; sequential; single branch.

The coordinator produces a CoordinatorResponse; Velora validates it before doing anything.
"""

from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    pass


_DECISIONS = {"execute_work_item", "request_review", "dismiss_finding", "finalize_success", "stop_failure"}
_SPECIALIST_ROLES = {"implementer", "docs", "refactor", "investigator", "reviewer"}
_WORK_ITEM_KINDS = {"implement", "repair", "refactor", "docs", "test_only", "investigate"}
_ACCEPTANCE_GATES = {"tests", "lint", "security", "ci", "docs"}
_WORK_RESULT_STATUS = {"completed", "blocked", "failed"}
_WORK_RESULT_TEST_STATUS = {"pass", "fail", "not_run"}
_ALLOWED_RUNNERS = {"codex", "claude", "gemini"}  # Gemini is valid for reviewer role; specialist matrix prevents it for code-writing roles.
_ALLOWED_MAX_DIFF_LINES = {50, 100, 200, 400}
_REVIEWER_BACKENDS = {"gemini", "claude"}
_REVIEW_SCOPE_KINDS = {"full_diff", "files"}
_REVIEW_VERDICTS = {"approve", "reject"}
_FINDING_SEVERITIES = {"blocker", "nit"}
_FINDING_CATEGORIES = {"correctness", "security", "regression", "style", "docs"}


def _expect_dict(value: object, *, ctx: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{ctx} must be an object")
    return value


def _expect_str(value: object, *, ctx: str, non_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{ctx} must be a string")
    s = value.strip()
    if non_empty and not s:
        raise ProtocolError(f"{ctx} must be a non-empty string")
    return s


def _expect_int(value: object, *, ctx: str) -> int:
    if not isinstance(value, int):
        raise ProtocolError(f"{ctx} must be an int")
    return value


def _expect_list(value: object, *, ctx: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{ctx} must be a list")
    return value


def _expect_enum(value: object, *, ctx: str, allowed: set[str]) -> str:
    s = _expect_str(value, ctx=ctx)
    if s not in allowed:
        allowed_str = ", ".join(sorted(allowed))
        raise ProtocolError(f"{ctx} must be one of: {allowed_str}")
    return s


def _no_extra_keys(obj: dict[str, Any], *, ctx: str, allowed_keys: set[str]) -> None:
    extras = set(obj.keys()) - allowed_keys
    if extras:
        extra_str = ", ".join(sorted(extras))
        allowed_str = ", ".join(sorted(allowed_keys))
        raise ProtocolError(f"{ctx} has unknown keys: {extra_str} (allowed: {allowed_str})")


@dataclass(frozen=True)
class SelectedSpecialist:
    role: str
    runner: str
    model: str | None = None

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "selected_specialist") -> SelectedSpecialist:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"role", "runner", "model"})
        role = _expect_enum(obj.get("role"), ctx=f"{ctx}.role", allowed=_SPECIALIST_ROLES)
        runner = _expect_enum(obj.get("runner"), ctx=f"{ctx}.runner", allowed=_ALLOWED_RUNNERS)
        model = obj.get("model")
        if model is not None:
            model = _expect_str(model, ctx=f"{ctx}.model")
        return SelectedSpecialist(role=role, runner=runner, model=model)


@dataclass(frozen=True)
class WorkItemAcceptance:
    must: list[str]
    must_not: list[str]
    gates: list[str]

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "work_item.acceptance") -> WorkItemAcceptance:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"must", "must_not", "gates"})

        must_raw = _expect_list(obj.get("must"), ctx=f"{ctx}.must")
        must = [_expect_str(x, ctx=f"{ctx}.must[]") for x in must_raw]

        must_not_raw = _expect_list(obj.get("must_not"), ctx=f"{ctx}.must_not")
        must_not = [_expect_str(x, ctx=f"{ctx}.must_not[]") for x in must_not_raw]

        gates_raw = _expect_list(obj.get("gates"), ctx=f"{ctx}.gates")
        gates: list[str] = []
        for g in gates_raw:
            gs = _expect_enum(g, ctx=f"{ctx}.gates[]", allowed=_ACCEPTANCE_GATES)
            gates.append(gs)

        return WorkItemAcceptance(must=must, must_not=must_not, gates=gates)


@dataclass(frozen=True)
class WorkItemLimits:
    max_diff_lines: int
    max_commits: int

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "work_item.limits") -> WorkItemLimits:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"max_diff_lines", "max_commits"})

        max_diff_lines = _expect_int(obj.get("max_diff_lines"), ctx=f"{ctx}.max_diff_lines")
        if max_diff_lines not in _ALLOWED_MAX_DIFF_LINES:
            allowed_str = ", ".join(str(x) for x in sorted(_ALLOWED_MAX_DIFF_LINES))
            raise ProtocolError(f"{ctx}.max_diff_lines must be one of: {allowed_str}")

        max_commits = _expect_int(obj.get("max_commits"), ctx=f"{ctx}.max_commits")
        if max_commits != 1:
            raise ProtocolError(f"{ctx}.max_commits must be 1 in protocol v1")

        return WorkItemLimits(max_diff_lines=max_diff_lines, max_commits=max_commits)


@dataclass(frozen=True)
class WorkItemCommit:
    message: str
    footer: dict[str, Any]

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "work_item.commit") -> WorkItemCommit:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"message", "footer"})

        message = _expect_str(obj.get("message"), ctx=f"{ctx}.message")
        footer_obj = _expect_dict(obj.get("footer"), ctx=f"{ctx}.footer")

        # Required footer keys.
        run_id = _expect_str(footer_obj.get("VELORA_RUN_ID"), ctx=f"{ctx}.footer.VELORA_RUN_ID")
        iteration = _expect_int(footer_obj.get("VELORA_ITERATION"), ctx=f"{ctx}.footer.VELORA_ITERATION")
        work_item_id = _expect_str(footer_obj.get("WORK_ITEM_ID"), ctx=f"{ctx}.footer.WORK_ITEM_ID")

        # Preserve any additional footer keys for forward-compat, but enforce required ones.
        footer = dict(footer_obj)
        footer["VELORA_RUN_ID"] = run_id
        footer["VELORA_ITERATION"] = iteration
        footer["WORK_ITEM_ID"] = work_item_id

        return WorkItemCommit(message=message, footer=footer)


@dataclass(frozen=True)
class ReviewScope:
    kind: str
    base_ref: str
    head_sha: str
    files: list[str]

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "review_scope") -> ReviewScope:
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
    def from_dict(raw: object, *, ctx: str = "ReviewBrief") -> ReviewBrief:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(
            obj,
            ctx=ctx,
            allowed_keys={
                "id",
                "reviewer",
                "model",
                "objective",
                "acceptance_criteria",
                "rejection_criteria",
                "areas_of_concern",
                "scope",
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
            id=bid,
            reviewer=reviewer,
            model=model,
            objective=objective,
            acceptance_criteria=acceptance_criteria,
            rejection_criteria=rejection_criteria,
            areas_of_concern=areas_of_concern,
            scope=scope,
        )


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    severity: str
    category: str
    location: str
    description: str
    criterion_id: int | None

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "ReviewFinding") -> ReviewFinding:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(
            obj,
            ctx=ctx,
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
            id=fid,
            severity=severity,
            category=category,
            location=location,
            description=description,
            criterion_id=criterion_id,
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
        _no_extra_keys(
            obj,
            ctx=ctx,
            allowed_keys={"review_brief_id", "verdict", "findings", "summary"},
        )

        review_brief_id = _expect_str(obj.get("review_brief_id"), ctx=f"{ctx}.review_brief_id")
        verdict = _expect_enum(obj.get("verdict"), ctx=f"{ctx}.verdict", allowed=_REVIEW_VERDICTS)
        summary = _expect_str(obj.get("summary"), ctx=f"{ctx}.summary")

        findings_raw = _expect_list(obj.get("findings"), ctx=f"{ctx}.findings")
        findings = [ReviewFinding.from_dict(x, ctx=f"{ctx}.findings[]") for x in findings_raw]

        # Coherence checks.
        has_blocker = any(f.severity == "blocker" for f in findings)
        if verdict == "approve" and has_blocker:
            raise ProtocolError(f"{ctx}: verdict=approve is not allowed with blocker-severity findings")
        if verdict == "reject" and not has_blocker:
            raise ProtocolError(f"{ctx}: verdict=reject requires at least one blocker-severity finding")

        return ReviewResult(
            review_brief_id=review_brief_id,
            verdict=verdict,
            findings=findings,
            summary=summary,
        )


@dataclass(frozen=True)
class FindingDismissal:
    finding_ids: list[str]
    justification: str

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "FindingDismissal") -> FindingDismissal:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"finding_ids", "justification"})

        finding_ids_raw = _expect_list(obj.get("finding_ids"), ctx=f"{ctx}.finding_ids")
        finding_ids = [_expect_str(x, ctx=f"{ctx}.finding_ids[]") for x in finding_ids_raw]
        if not finding_ids:
            raise ProtocolError(f"{ctx}.finding_ids must contain at least one finding ID")

        justification = _expect_str(obj.get("justification"), ctx=f"{ctx}.justification")

        return FindingDismissal(finding_ids=finding_ids, justification=justification)


@dataclass(frozen=True)
class WorkItemScopeHints:
    likely_files: list[str]
    search_terms: list[str]

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "work_item.scope_hints") -> WorkItemScopeHints:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"likely_files", "search_terms"})

        likely_files_raw = _expect_list(obj.get("likely_files"), ctx=f"{ctx}.likely_files")
        likely_files = [_expect_str(x, ctx=f"{ctx}.likely_files[]") for x in likely_files_raw]

        search_terms_raw = _expect_list(obj.get("search_terms"), ctx=f"{ctx}.search_terms")
        search_terms = [_expect_str(x, ctx=f"{ctx}.search_terms[]") for x in search_terms_raw]

        return WorkItemScopeHints(likely_files=likely_files, search_terms=search_terms)


@dataclass(frozen=True)
class WorkItem:
    id: str
    kind: str
    rationale: str
    instructions: list[str]
    scope_hints: WorkItemScopeHints
    acceptance: WorkItemAcceptance
    limits: WorkItemLimits
    commit: WorkItemCommit

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "work_item") -> WorkItem:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(
            obj,
            ctx=ctx,
            allowed_keys={
                "id",
                "kind",
                "rationale",
                "instructions",
                "scope_hints",
                "acceptance",
                "limits",
                "commit",
            },
        )

        wid = _expect_str(obj.get("id"), ctx=f"{ctx}.id")
        kind = _expect_enum(obj.get("kind"), ctx=f"{ctx}.kind", allowed=_WORK_ITEM_KINDS)
        rationale = _expect_str(obj.get("rationale"), ctx=f"{ctx}.rationale")

        instructions_raw = _expect_list(obj.get("instructions"), ctx=f"{ctx}.instructions")
        instructions = [_expect_str(x, ctx=f"{ctx}.instructions[]") for x in instructions_raw]
        if not instructions:
            raise ProtocolError(f"{ctx}.instructions must contain at least one instruction")

        scope_hints = WorkItemScopeHints.from_dict(obj.get("scope_hints"), ctx=f"{ctx}.scope_hints")
        acceptance = WorkItemAcceptance.from_dict(obj.get("acceptance"), ctx=f"{ctx}.acceptance")
        limits = WorkItemLimits.from_dict(obj.get("limits"), ctx=f"{ctx}.limits")
        commit = WorkItemCommit.from_dict(obj.get("commit"), ctx=f"{ctx}.commit")

        # Basic coherence checks.
        if commit.footer.get("WORK_ITEM_ID") != wid:
            raise ProtocolError(f"{ctx}.commit.footer.WORK_ITEM_ID must match {ctx}.id")

        return WorkItem(
            id=wid,
            kind=kind,
            rationale=rationale,
            instructions=instructions,
            scope_hints=scope_hints,
            acceptance=acceptance,
            limits=limits,
            commit=commit,
        )


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
            obj,
            ctx=ctx,
            allowed_keys={
                "protocol_version",
                "decision",
                "reason",
                "selected_specialist",
                "work_item",
                "review_brief",
                "finding_dismissal",
            },
        )

        protocol_version = _expect_int(obj.get("protocol_version"), ctx=f"{ctx}.protocol_version")
        if protocol_version != 1:
            raise ProtocolError(f"{ctx}.protocol_version must be 1")

        decision = _expect_enum(obj.get("decision"), ctx=f"{ctx}.decision", allowed=_DECISIONS)
        reason = _expect_str(obj.get("reason"), ctx=f"{ctx}.reason")

        selected_specialist = SelectedSpecialist.from_dict(obj.get("selected_specialist"), ctx=f"{ctx}.selected_specialist")

        # Decision → required-payload mapping.  Each decision requires exactly one
        # payload field (or none for terminal decisions).  All non-matched payload
        # fields must be absent.
        _DECISION_PAYLOAD = {
            "execute_work_item": "work_item",
            "request_review": "review_brief",
            "dismiss_finding": "finding_dismissal",
        }
        _PAYLOAD_FIELDS = {"work_item", "review_brief", "finding_dismissal"}

        required_field = _DECISION_PAYLOAD.get(decision)

        # Parse the payload field that matches this decision (if any).
        work_item: WorkItem | None = None
        review_brief: ReviewBrief | None = None
        finding_dismissal: FindingDismissal | None = None

        if required_field == "work_item":
            raw_val = obj.get("work_item")
            if raw_val is None:
                raise ProtocolError(f"{ctx}.work_item is required when decision={decision}")
            work_item = WorkItem.from_dict(raw_val, ctx=f"{ctx}.work_item")
        elif required_field == "review_brief":
            raw_val = obj.get("review_brief")
            if raw_val is None:
                raise ProtocolError(f"{ctx}.review_brief is required when decision={decision}")
            review_brief = ReviewBrief.from_dict(raw_val, ctx=f"{ctx}.review_brief")
        elif required_field == "finding_dismissal":
            raw_val = obj.get("finding_dismissal")
            if raw_val is None:
                raise ProtocolError(f"{ctx}.finding_dismissal is required when decision={decision}")
            finding_dismissal = FindingDismissal.from_dict(raw_val, ctx=f"{ctx}.finding_dismissal")

        # Reject payload fields that do not belong to this decision.
        for field in _PAYLOAD_FIELDS:
            if field == required_field:
                continue
            if obj.get(field) is not None:
                raise ProtocolError(f"{ctx}.{field} must be omitted when decision={decision}")

        return CoordinatorResponse(
            protocol_version=protocol_version,
            decision=decision,
            reason=reason,
            selected_specialist=selected_specialist,
            work_item=work_item,
            review_brief=review_brief,
            finding_dismissal=finding_dismissal,
        )


@dataclass(frozen=True)
class WorkResultTestRun:
    command: str
    status: str
    details: str

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "WorkResult.tests_run[]") -> WorkResultTestRun:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(obj, ctx=ctx, allowed_keys={"command", "status", "details"})

        command = _expect_str(obj.get("command"), ctx=f"{ctx}.command", non_empty=False)
        status = _expect_enum(obj.get("status"), ctx=f"{ctx}.status", allowed=_WORK_RESULT_TEST_STATUS)
        details = _expect_str(obj.get("details"), ctx=f"{ctx}.details", non_empty=False)
        return WorkResultTestRun(command=command, status=status, details=details)


@dataclass(frozen=True)
class WorkResult:
    protocol_version: int
    work_item_id: str
    status: str
    summary: str
    branch: str
    head_sha: str
    files_touched: list[str]
    tests_run: list[WorkResultTestRun]
    blockers: list[str]
    follow_up: list[str]
    evidence: list[str]

    @staticmethod
    def from_dict(raw: object, *, ctx: str = "WorkResult") -> WorkResult:
        obj = _expect_dict(raw, ctx=ctx)
        _no_extra_keys(
            obj,
            ctx=ctx,
            allowed_keys={
                "protocol_version",
                "work_item_id",
                "status",
                "summary",
                "branch",
                "head_sha",
                "files_touched",
                "tests_run",
                "blockers",
                "follow_up",
                "evidence",
            },
        )

        protocol_version = _expect_int(obj.get("protocol_version"), ctx=f"{ctx}.protocol_version")
        if protocol_version != 1:
            raise ProtocolError(f"{ctx}.protocol_version must be 1")

        work_item_id = _expect_str(obj.get("work_item_id"), ctx=f"{ctx}.work_item_id")
        status = _expect_enum(obj.get("status"), ctx=f"{ctx}.status", allowed=_WORK_RESULT_STATUS)
        summary = _expect_str(obj.get("summary"), ctx=f"{ctx}.summary")
        branch = _expect_str(obj.get("branch"), ctx=f"{ctx}.branch", non_empty=False)
        head_sha = _expect_str(obj.get("head_sha"), ctx=f"{ctx}.head_sha", non_empty=False)

        files_touched_raw = _expect_list(obj.get("files_touched"), ctx=f"{ctx}.files_touched")
        files_touched = [_expect_str(x, ctx=f"{ctx}.files_touched[]") for x in files_touched_raw]

        tests_run_raw = _expect_list(obj.get("tests_run"), ctx=f"{ctx}.tests_run")
        tests_run = [WorkResultTestRun.from_dict(x, ctx=f"{ctx}.tests_run[]") for x in tests_run_raw]

        blockers_raw = _expect_list(obj.get("blockers"), ctx=f"{ctx}.blockers")
        blockers = [_expect_str(x, ctx=f"{ctx}.blockers[]") for x in blockers_raw]

        follow_up_raw = _expect_list(obj.get("follow_up"), ctx=f"{ctx}.follow_up")
        follow_up = [_expect_str(x, ctx=f"{ctx}.follow_up[]") for x in follow_up_raw]

        evidence_raw = _expect_list(obj.get("evidence"), ctx=f"{ctx}.evidence")
        evidence = [_expect_str(x, ctx=f"{ctx}.evidence[]") for x in evidence_raw]

        if status == "completed":
            if not branch:
                raise ProtocolError(f"{ctx}.branch must be non-empty when status=completed")
            if not head_sha:
                raise ProtocolError(f"{ctx}.head_sha must be non-empty when status=completed")
            if blockers:
                raise ProtocolError(f"{ctx}.blockers must be empty when status=completed")
        else:
            if not blockers:
                raise ProtocolError(f"{ctx}.blockers must be non-empty when status={status}")
            if branch:
                raise ProtocolError(f"{ctx}.branch must be empty when status={status}")
            if head_sha:
                raise ProtocolError(f"{ctx}.head_sha must be empty when status={status}")

        return WorkResult(
            protocol_version=protocol_version,
            work_item_id=work_item_id,
            status=status,
            summary=summary,
            branch=branch,
            head_sha=head_sha,
            files_touched=files_touched,
            tests_run=tests_run,
            blockers=blockers,
            follow_up=follow_up,
            evidence=evidence,
        )


def validate_coordinator_response(payload: object) -> CoordinatorResponse:
    """Validate and parse a coordinator response.

    This is the hard gate that prevents "AI drift" from turning into execution.
    Any violation is a hard failure.
    """

    return CoordinatorResponse.from_dict(payload)


def validate_work_result(payload: object) -> WorkResult:
    """Validate and parse a worker work-result payload."""

    return WorkResult.from_dict(payload)


def validate_review_brief(payload: object) -> ReviewBrief:
    """Validate and parse a review brief payload."""

    return ReviewBrief.from_dict(payload)


def validate_review_result(payload: object) -> ReviewResult:
    """Validate and parse a review result payload."""

    return ReviewResult.from_dict(payload)


def validate_finding_dismissal(payload: object) -> FindingDismissal:
    """Validate and parse a finding dismissal payload."""

    return FindingDismissal.from_dict(payload)


def enforce_specialist_matrix(resp: CoordinatorResponse, matrix: object) -> None:
    """Enforce role→runner/model allowlists.

    This is a *hard-fail* policy gate: out-of-bounds coordinator selections raise
    ProtocolError rather than being remapped.

    Matrix shape (JSON-serializable):
      { role: {"runners": ["codex"|"claude"], "models": ["..."]} }

    Models list is optional; if empty, model overrides are treated as disallowed.
    """

    if matrix is None:
        return
    if not isinstance(matrix, dict):
        raise ProtocolError("policy.specialist_matrix must be an object")

    role = resp.selected_specialist.role
    rule = matrix.get(role)
    if not isinstance(rule, dict):
        raise ProtocolError(f"policy.specialist_matrix missing rule for role: {role}")

    runners_raw = rule.get("runners")
    if not isinstance(runners_raw, list) or not runners_raw:
        raise ProtocolError(f"policy.specialist_matrix[{role}].runners must be a non-empty list")
    allowed_runners = {str(r).strip().lower() for r in runners_raw if isinstance(r, str) and str(r).strip()}

    if resp.selected_specialist.runner not in allowed_runners:
        allowed_str = ", ".join(sorted(allowed_runners))
        raise ProtocolError(
            f"selected_specialist.runner '{resp.selected_specialist.runner}' is not allowed for role '{role}' (allowed: {allowed_str})"
        )

    if resp.selected_specialist.model is None:
        return

    models_raw = rule.get("models", [])
    if models_raw is None:
        models_raw = []
    if not isinstance(models_raw, list):
        raise ProtocolError(f"policy.specialist_matrix[{role}].models must be a list")
    allowed_models = {str(m).strip() for m in models_raw if isinstance(m, str) and str(m).strip()}

    if not allowed_models:
        raise ProtocolError(f"selected_specialist.model is not allowed for role '{role}' (no model overrides permitted)")

    if resp.selected_specialist.model not in allowed_models:
        allowed_str = ", ".join(sorted(allowed_models))
        raise ProtocolError(
            f"selected_specialist.model '{resp.selected_specialist.model}' is not allowed for role '{role}' (allowed: {allowed_str})"
        )
