"""Offline tests of the experimental exact-package relay and its trust gate."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from danus import improvement
from danus.core import FactGraph, GlobalMemory
from danus.gateway import server
from danus.gateway.roles import tools_for
from danus.verify import launcher, service
from fastapi.testclient import TestClient


class CandidateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "bounds"
        self.project.mkdir()
        (self.project / "PROBLEM.md").write_text("Improve C on D")
        self.package = {"producer": "/root/scout", "statement": "For every case in D, C <= 9.",
                        "proof": "A complete, sufficiently long mathematical proof supplied by the scout.",
                        "source_id": "scout-report-1"}
        self.source = self.project / "scout.json"
        self.source.write_text(json.dumps(self.package))
        env = patch.dict(os.environ, DANUS_ROLE="main", DANUS_AGENTS_ROOT=str(self.root),
                         DANUS_AUTHOR="main_agent", DANUS_PROJECT_DIR="")
        env.start()
        self.addCleanup(env.stop)

    def accepted(self, statement, proof, **kwargs):
        self.assertEqual(statement, self.package["statement"])
        self.assertEqual(proof, self.package["proof"])
        self.assertIn("predecessor_facts", kwargs["candidate_context"])
        return {"verdict": "correct", "verification_report": {"critical_errors": [], "gaps": []},
                "candidate_sha256": kwargs["candidate_context"]["package_sha256"]}

    def relay(self, verify=None):
        with patch.object(server, "_verify", verify or self.accepted):
            return server.candidate_submit("bounds", "scout.json")

    def test_exact_proof_and_producer_preserved(self):
        raw = self.source.read_bytes()
        result = self.relay()
        self.assertTrue(result["accepted"])
        receipt_path = Path(result["receipt_file"])
        self.assertEqual((receipt_path.parent / "package.json").read_bytes(), raw)
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["submitter"], "main_agent")
        self.assertEqual(receipt["producer"], "/root/scout")
        self.assertEqual(receipt["route"], "scout_direct")
        self.assertIn("author: /root/scout", FactGraph(self.project).get_raw(result["fact_id"]))
        self.assertEqual(GlobalMemory(self.project).read("verification")[-1]["links"]["source_id"], "scout-report-1")

    def test_source_edit_does_not_change_frozen_proof(self):
        raw = self.source.read_bytes()
        def verify(s, p, **kw):
            self.source.write_text('{"proof":"changed"}')
            return self.accepted(s, p, **kw)
        result = self.relay(verify)
        self.assertTrue(result["accepted"])
        self.assertEqual((Path(result["receipt_file"]).parent / "package.json").read_bytes(), raw)
        self.assertIn(self.package["proof"], FactGraph(self.project).get_raw(result["fact_id"]))

    def test_wrong_and_malformed_verdicts_do_not_publish(self):
        for verdict in ({"verdict": "wrong"}, {"verdict": "correct"},
                        {"verdict": "correct", "verification_report": {"critical_errors": [], "gaps": []}},
                        {"verdict": "correct", "verification_report": {"critical_errors": [], "gaps": ["gap"]}}):
            result = self.relay(lambda *a, **k: verdict)
            self.assertFalse(result["accepted"])
        self.assertEqual(FactGraph(self.project).list(), [])

    def test_failure_is_logged(self):
        def fail(*a, **k): raise TimeoutError("timed out")
        result = self.relay(fail)
        self.assertFalse(result["accepted"])
        self.assertIn("timed out", json.loads(Path(result["receipt_file"]).read_text())["result"]["error"])

    def test_changed_predecessor_blocks_publication(self):
        fg = FactGraph(self.project)
        fid = fg.add(problem_id="bounds", author="w", statement="Lemma", proof="Proof")
        self.package["predecessors"] = [fid]
        self.source.write_text(json.dumps(self.package))
        def verify(s, p, **kw):
            self.assertIn(fid, kw["candidate_context"]["predecessor_facts"])
            fg.revoke(fid, "wrong")
            return self.accepted(s, p, **kw)
        self.assertFalse(self.relay(verify)["accepted"])
        self.assertEqual(fg.list(), [])

    def test_improvement_uses_same_strict_gate(self):
        snapshot = improvement.initialize(self.project, "C <= 10", "smaller upper bound")
        self.package.update(improvement="9 < 10", baseline_sha256=snapshot["baseline_sha256"])
        self.source.write_text(json.dumps(self.package))
        def verify(s, p, context):
            return {"verdict": "correct", "verification_report": {"critical_errors": [], "gaps": []},
                    "improvement_assessment": {"verdict": "strict_improvement", "explanation": "9 < 10",
                        "baseline_sha256": context["baseline_sha256"],
                        "candidate_sha256": context["candidate_sha256"]}}
        result = self.relay(verify)
        self.assertTrue(result["accepted"])
        self.assertEqual(improvement.status(self.project)["accepted"][0]["author"], "/root/scout")
        self.assertEqual(self.relay(verify)["verdict"], "stale_baseline")

    def test_package_cannot_escape_project(self):
        outside = self.root / "other.json"
        outside.write_text(json.dumps(self.package))
        (self.project / "link.json").symlink_to(outside)
        for path in ("../other.json", str(outside), "link.json"):
            with self.assertRaises(ValueError): server.candidate_submit("bounds", path)

    def test_unknown_fields_and_partial_comparison_refused(self):
        for extra in ({"force_accept": True}, {"improvement": "better"}):
            self.source.write_text(json.dumps({**self.package, **extra}))
            with self.assertRaises(ValueError): self.relay()

    def test_only_main_exposes_relay_and_verifier_remains_read_only(self):
        self.assertIn("candidate_submit", tools_for("main"))
        for role in ("worker", "verifier", "unknown"):
            self.assertNotIn("candidate_submit", tools_for(role))
        with patch.dict(os.environ, DANUS_ROLE="worker"):
            with self.assertRaises(PermissionError): self.relay()
        self.assertNotIn("fact_submit", tools_for("main"))
        self.assertEqual(tools_for("verifier"), ["search_arxiv_theorems"])

    def test_service_forwards_candidate_context(self):
        context = {"producer": "/root/scout", "predecessor_facts": {}}
        def fake_run(run_id, statement, proof, candidate_context):
            self.assertEqual(candidate_context, context)
            return {"verdict": "wrong"}
        with patch.object(service, "run_codex_verification", fake_run), \
             patch.object(service, "_allocate_run_id", return_value="offline"):
            with TestClient(service.app) as client:
                response = client.post("/verify", json={"statement": self.package["statement"],
                    "proof": self.package["proof"], "candidate_context": context})
        self.assertEqual(response.status_code, 200)

    def test_launcher_freezes_large_candidate_input(self):
        with patch.object(launcher, "_results_root", return_value=self.root), \
             patch.object(launcher, "ensure_agent_home"), \
             patch.object(launcher, "_agent_home", return_value=self.root):
            context = {"package_sha256": "package-hash", "predecessor_facts": {"lemma": "x" * 500000}}
            def fake_run(cmd, **kwargs):
                self.assertLess(len(cmd[-1]), 10000)
                frozen = json.loads((self.root / "run" / "candidate_input.json").read_text())
                self.assertEqual(frozen["proof"], self.package["proof"])
                self.assertEqual(frozen["context"], context)
                (self.root / "run" / "verification.json").write_text('{"verdict":"wrong"}')
                return SimpleNamespace(returncode=0)
            with patch.object(launcher.subprocess, "run", fake_run):
                self.assertEqual(launcher.run_codex_verification("run", self.package["statement"],
                    self.package["proof"], candidate_context=context)["verdict"], "wrong")


if __name__ == "__main__":
    unittest.main()
