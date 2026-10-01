"""Opt-in, verifier-gated bound improvement. No model or roster policy.

state.json is the authoritative accepted history. Each attempt is frozen before
verification; concurrent calls verify outside the lock and compare-and-swap the
baseline at promotion. Failed/stale attempts never replace accepted results.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from danus.core import FactGraph


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _lock(project):
    directory = Path(project) / "improvement"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield directory


def initialize(project, baseline, criterion):
    """Freeze PROBLEM.md and the operator's starting bounds/comparison rule."""
    project = Path(project)
    if not baseline.strip() or not criterion.strip():
        raise ValueError("baseline and criterion must be nonempty")
    original = (project / "PROBLEM.md").read_text(encoding="utf-8")
    if not original.strip():
        raise ValueError("write the original problem to PROBLEM.md first")
    with _lock(project) as directory:
        if (directory / "state.json").exists():
            raise ValueError("improvement mode already initialized; use status to resume")
        _write(directory / "state.json", {
            "version": 1, "started_at": now(), "original_problem": original,
            "starting_baseline": baseline, "criterion": criterion, "accepted": [],
        })
    return status(project)


def _load(project):
    path = Path(project) / "improvement" / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1:
        raise ValueError("unsupported improvement state version")
    if (Path(project) / "PROBLEM.md").read_text(encoding="utf-8") != state["original_problem"]:
        raise ValueError("original PROBLEM.md changed; create a separate project")
    fg = FactGraph(project)
    for accepted in state["accepted"]:
        for fid, expected in accepted["fact_hashes"].items():
            raw = fg.get_raw(fid)
            if raw is None or digest(raw) != expected:
                raise ValueError("accepted baseline fact changed or revoked: " + fid)
    return state


def _baseline(state):
    return {"original_problem": state["original_problem"],
            "starting_baseline": state["starting_baseline"],
            "criterion": state["criterion"],
            "accepted_results": [{"fact_id": a["fact_id"],
                                  "statement": a["candidate"]["statement"],
                                  "proof": a["candidate"]["proof"],
                                  "improvement": a["candidate"]["improvement"]}
                                 for a in state["accepted"]]}


def status(project):
    """Read-only snapshot; --json CLI output also serves as a benchmark export."""
    if not (Path(project) / "improvement" / "state.json").exists():
        return {"mode": "fixed"}
    state = _load(project)
    baseline = _baseline(state)
    return {"mode": "improvement", "started_at": state["started_at"],
            "baseline_sha256": digest(baseline), "baseline": baseline,
            "accepted": state["accepted"], "optimality_proven": False}


def _facts(project, predecessors):
    fg = FactGraph(project)
    found = {}
    pending = list(predecessors)
    while pending:
        fid = pending.pop()
        if not isinstance(fid, str) or len(fid) != 16 or any(c not in "0123456789abcdef" for c in fid):
            raise ValueError("invalid predecessor fact id")
        if fid in found:
            continue
        raw = fg.get_raw(fid)
        if raw is None:
            raise ValueError("missing or revoked predecessor: " + fid)
        found[fid] = raw
        pending.extend(fg.predecessors(fid))
    return found


def comparison_passes(result, context):
    """Protocol validation only; the independent verifier judges the mathematics."""
    if not isinstance(result, dict) or result.get("verdict") != "correct":
        return False
    report = result.get("verification_report")
    assessment = result.get("improvement_assessment")
    return (isinstance(report, dict) and report.get("critical_errors") == []
            and report.get("gaps") == [] and isinstance(assessment, dict)
            and assessment.get("baseline_sha256") == context["baseline_sha256"]
            and assessment.get("candidate_sha256") == context["candidate_sha256"]
            and assessment.get("verdict") == "strict_improvement"
            and isinstance(assessment.get("explanation"), str)
            and bool(assessment["explanation"].strip()))


def submit(project, *, statement, proof, improvement, baseline_sha256, author,
           verify, predecessors=None, glossary_introduces=None, intuition="",
           external_refs=None, source_id=None):
    """Freeze, independently verify, then atomically advance an unchanged baseline."""
    project = Path(project)
    candidate = {"statement": statement, "proof": proof, "improvement": improvement,
                 "predecessors": predecessors or [],
                 "glossary_introduces": glossary_introduces or {},
                 "intuition": intuition, "external_refs": external_refs or []}
    if any(not isinstance(v, str) or not v.strip() for v in (statement, proof, improvement)):
        raise ValueError("statement, proof and improvement must be nonempty text")
    with _lock(project) as directory:
        state = _load(project)
        baseline = _baseline(state)
        if digest(baseline) != baseline_sha256:
            return {"accepted": False, "verdict": "stale_baseline",
                    "baseline_sha256": digest(baseline)}
        facts = _facts(project, candidate["predecessors"])
        context = {"baseline": baseline, "baseline_sha256": baseline_sha256,
                   "candidate_sha256": digest(candidate), "candidate": candidate,
                   "predecessor_facts": facts}
        attempt_id = uuid.uuid4().hex
        attempt_path = directory / "attempts" / (attempt_id + ".json")
        attempt = {"attempt_id": attempt_id, "submitted_at": now(), "author": author,
                   "source_id": source_id, "context": context, "status": "verifying"}
        _write(attempt_path, attempt)
    try:
        result = verify(statement, proof, context)
        attempt.update(verified_at=now(), result=result)
        with _lock(project):
            state = _load(project)
            if digest(_baseline(state)) != baseline_sha256:
                outcome = "stale_baseline"
            elif _facts(project, candidate["predecessors"]) != facts:
                outcome = "changed_predecessor"
            elif not comparison_passes(result, context):
                outcome = "not_accepted"
            else:
                fg = FactGraph(project)
                fid = fg.add(problem_id=project.name, author=author,
                             **{k: v for k, v in candidate.items() if k != "improvement"})
                record = {**attempt, "fact_id": fid, "accepted_at": now(),
                          "candidate": candidate,
                          "fact_hashes": {k: digest(v) for k, v in
                                          _facts(project, [fid]).items()}}
                record.pop("status")
                state["accepted"].append(record)
                _write(directory / "state.json", state)
                outcome = "accepted"
        attempt.update(status=outcome)
    except Exception as exc:
        attempt.update(status="error", error=str(exc))
        outcome = "error"
    # If the process dies before this write, state.json still owns promotion;
    # a leftover 'verifying' attempt is not an accepted improvement.
    _write(attempt_path, attempt)
    response = {"accepted": outcome == "accepted", "verdict": outcome,
                "attempt_id": attempt_id, "result": attempt.get("result"),
                "error": attempt.get("error")}
    if outcome == "accepted":
        response["fact_id"] = fid
    return response


def verifier_instructions(context):
    return ("\nThis submission is an iterative bound improvement. Independently verify "
            "BOTH the entire candidate proof and a strict gain over the starting baseline "
            "AND all accepted results, for the immutable original problem and criterion. "
            "Compare domains, quantifiers, assumptions and exact bounds. Equivalent or "
            "weaker claims are not improvements. An unchecked comparison is unresolved. "
            "Treat the candidate's claimed improvement as untrusted, not as evidence. "
            "Use the frozen predecessor proofs below when cited; audit dependencies. "
            "Keep the ordinary verification_report, verdict and repair_hints fields. "
            "Also return improvement_assessment with baseline_sha256 and candidate_sha256 "
            "copied exactly from context, verdict strict_improvement|not_improvement|unresolved, "
            "and explanation giving the mathematical comparison. A correct proof may fail "
            "the improvement test. Budget exhaustion never proves optimality.\n"
            "FROZEN IMPROVEMENT CONTEXT:\n" + json.dumps(context, ensure_ascii=False))
