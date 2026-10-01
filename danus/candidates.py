"""Experimental exact-file relay for scout proofs; no independent worker rewrite.

The main-role gateway freezes the package, preserves its producer attribution,
and invokes the existing proof/improvement gates. This is not a fact-store API.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid

from danus import improvement


_FIELDS = {"producer", "statement", "proof", "predecessors", "glossary_introduces",
           "intuition", "external_refs", "source_id", "improvement", "baseline_sha256",
           "discovered_at"}


def _read(project, package_file):
    relative = Path(package_file)
    if relative.is_absolute():
        raise ValueError("package_file must be relative to the project directory")
    path = (project / relative).resolve()
    if not path.is_relative_to(project.resolve()) or not path.is_file():
        raise ValueError("package must be a file inside the selected project")
    raw = path.read_bytes()
    package = json.loads(raw)
    if not isinstance(package, dict) or set(package) - _FIELDS:
        raise ValueError("invalid candidate package fields")
    for key in ("producer", "statement", "proof"):
        if not isinstance(package.get(key), str) or not package[key].strip():
            raise ValueError("candidate package requires nonempty " + key)
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", package["producer"]):
        raise ValueError("producer must be a plain agent identifier")
    if ("improvement" in package) != ("baseline_sha256" in package):
        raise ValueError("bound packages require both improvement and baseline_sha256")
    return raw, package


def submit_package(project, package_file, *, submitter, verify, publish):
    project = Path(project)
    raw, package = _read(project, package_file)
    # Check dependencies before spending a verifier call. The frozen snapshots
    # are retained for provenance and rechecked before publication.
    facts = improvement._facts(project, package.get("predecessors", []))
    submission_id = uuid.uuid4().hex
    directory = project / "candidate_submissions" / submission_id
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "package.json").write_bytes(raw)
    receipt = {"submission_id": submission_id, "submitted_at": improvement.now(),
               "route": "scout_direct", "submitter": submitter,
               "producer": package["producer"], "package_file": package_file,
               "package_sha256": hashlib.sha256(raw).hexdigest(),
               "declared_discovered_at": package.get("discovered_at"),
               "predecessor_facts": facts, "status": "verifying"}
    improvement._write(directory / "receipt.json", receipt)
    kwargs = {k: package[k] for k in ("statement", "proof", "predecessors",
              "glossary_introduces", "intuition", "external_refs", "source_id") if k in package}
    kwargs["author"] = package["producer"]
    try:
        if "improvement" in package:
            result = improvement.submit(project, **kwargs, verify=verify,
                        improvement=package["improvement"],
                        baseline_sha256=package["baseline_sha256"])
        else:
            def checked_verify(statement, proof):
                verdict = verify(statement, proof, candidate_context={
                    "package_sha256": receipt["package_sha256"],
                    "producer": package["producer"], "predecessor_facts": facts})
                receipt["verification_result"] = verdict
                if improvement._facts(project, package.get("predecessors", [])) != facts:
                    raise ValueError("predecessor changed during verification")
                if isinstance(verdict, dict) and verdict.get("verdict") == "correct":
                    report = verdict.get("verification_report")
                    if not isinstance(report, dict) or report.get("critical_errors") != [] or report.get("gaps") != []:
                        raise ValueError("correct verdict lacks a complete clean verification report")
                    if verdict.get("candidate_sha256") != receipt["package_sha256"]:
                        raise ValueError("verifier result is not bound to the frozen scout package")
                return verdict
            result = publish(**kwargs, verify=checked_verify)
        receipt.update(status="finished", finished_at=improvement.now(), result=result)
    except Exception as exc:
        result = {"accepted": False, "verdict": "error", "error": str(exc)}
        receipt.update(status="error", finished_at=improvement.now(), result=result)
    improvement._write(directory / "receipt.json", receipt)
    return {**result, "submission_id": submission_id,
            "receipt_file": str(directory / "receipt.json")}
