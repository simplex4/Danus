"""Offline improvement protocol tests; no mathematical truth or live model assumed."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from danus import improvement as imp
from danus.core import FactGraph


def correct(statement, proof, context):
    return {"verdict": "correct", "verification_report": {"critical_errors": [], "gaps": []},
            "improvement_assessment": {"baseline_sha256": context["baseline_sha256"],
                "candidate_sha256": context["candidate_sha256"],
                "verdict": "strict_improvement", "explanation": "Strict gain on the fixed domain."}}


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / "PROBLEM.md").write_text("Improve the upper bound on C, on domain D.")
        imp.initialize(self.project, "C <= 10 on D", "Smaller upper bound on the same domain D")

    def submit(self, **kwargs):
        args = dict(statement="C <= 9 on D", proof="A complete candidate proof.",
                    improvement="9 < 10", author="worker", verify=correct,
                    baseline_sha256=imp.status(self.project)["baseline_sha256"])
        args.update(kwargs)
        return imp.submit(self.project, **args)

    def test_accept_resume_and_second_improvement(self):
        first = self.submit()
        self.assertTrue(first["accepted"])
        second = self.submit(statement="C <= 8 on D", improvement="8 < 9")
        self.assertTrue(second["accepted"])
        snapshot = imp.status(self.project)
        self.assertEqual(len(snapshot["accepted"]), 2)
        self.assertFalse(snapshot["optimality_proven"])
        self.assertEqual(len(snapshot["baseline"]["accepted_results"]), 2)
        self.assertEqual(snapshot["accepted"][1]["context"]["baseline"]["accepted_results"][0]["fact_id"], first["fact_id"])

    def test_equivalent_wrong_unresolved_and_unbound_never_advance(self):
        self.submit()
        baseline = imp.status(self.project)
        for change in ("equivalent", "wrong", "unresolved", "stale_hash", "candidate_hash", "gaps", "legacy"):
            with self.subTest(change=change):
                def verify(s, p, c):
                    result = correct(s, p, c)
                    if change == "wrong": result["verdict"] = "wrong"
                    elif change == "legacy": result.pop("improvement_assessment")
                    elif change == "gaps": result["verification_report"]["gaps"] = ["gap"]
                    elif change == "stale_hash": result["improvement_assessment"]["baseline_sha256"] = "old"
                    elif change == "candidate_hash": result["improvement_assessment"]["candidate_sha256"] = "other"
                    else: result["improvement_assessment"]["verdict"] = "not_improvement" if change == "equivalent" else change
                    return result
                self.assertFalse(self.submit(verify=verify)["accepted"])
                self.assertEqual(imp.status(self.project), baseline)

    def test_error_preserves_best_and_records_attempt(self):
        self.submit()
        with patch.object(imp, "now", return_value="test-time"):
            def fail(*args): raise TimeoutError("budget exhausted")
            result = self.submit(verify=fail)
        self.assertEqual(result["verdict"], "error")
        attempt = json.loads((self.project / "improvement" / "attempts" / (result["attempt_id"] + ".json")).read_text())
        self.assertEqual(attempt["status"], "error")
        self.assertEqual(len(imp.status(self.project)["accepted"]), 1)

    def test_stale_submission_does_not_call_verifier(self):
        old = imp.status(self.project)["baseline_sha256"]
        self.submit()
        with patch.object(imp, "comparison_passes") as compare:
            result = self.submit(baseline_sha256=old, verify=lambda *a: self.fail("must not call"))
        self.assertEqual(result["verdict"], "stale_baseline")
        compare.assert_not_called()

    def test_concurrent_candidates_only_one_promoted(self):
        barrier = threading.Barrier(2)
        baseline = imp.status(self.project)["baseline_sha256"]
        results = []
        def verify(s, p, c):
            barrier.wait(timeout=5)
            return correct(s, p, c)
        def run(bound):
            results.append(self.submit(statement=f"C <= {bound} on D", verify=verify,
                                       baseline_sha256=baseline))
        threads = [threading.Thread(target=run, args=(n,)) for n in (8, 9)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertEqual(sorted(r["verdict"] for r in results), ["accepted", "stale_baseline"])
        self.assertEqual(len(imp.status(self.project)["accepted"]), 1)

    def test_problem_change_and_reinitialization_refused(self):
        with self.assertRaises(ValueError): imp.initialize(self.project, "new", "new")
        (self.project / "PROBLEM.md").write_text("different question")
        with self.assertRaisesRegex(ValueError, "PROBLEM"): imp.status(self.project)

    def test_missing_predecessor_refused_before_verification(self):
        with self.assertRaisesRegex(ValueError, "missing or revoked"):
            self.submit(predecessors=["0" * 16])

    def test_revoked_or_changed_accepted_dependency_fails_closed(self):
        fg = FactGraph(self.project)
        pred = fg.add(problem_id="test", author="w", statement="Lemma", proof="Proof")
        self.submit(predecessors=[pred])
        fg.revoke(pred, "invalid")
        with self.assertRaisesRegex(ValueError, "changed or revoked"): imp.status(self.project)

    def test_predecessor_changed_during_verification(self):
        fg = FactGraph(self.project)
        pred = fg.add(problem_id="test", author="w", statement="Lemma", proof="Proof")
        def verify(s, p, c):
            (fg.facts_dir / (pred + ".md")).write_text("changed")
            return correct(s, p, c)
        self.assertEqual(self.submit(predecessors=[pred], verify=verify)["verdict"], "changed_predecessor")
        self.assertEqual(len(imp.status(self.project)["accepted"]), 0)

    def test_fixed_project_is_unchanged(self):
        self.assertEqual(imp.status(self.project / "other"), {"mode": "fixed"})


if __name__ == "__main__":
    unittest.main()
