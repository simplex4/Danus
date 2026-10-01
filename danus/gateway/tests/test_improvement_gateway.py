"""Exercise CLI -> gateway HTTP -> verifier prompt -> accepted history offline."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient
from danus import improvement
from danus.gateway import server
from danus.gateway.roles import tools_for
from danus.orchestration.cli import main
from danus.verify import service, launcher


class ImprovementGatewayTests(unittest.TestCase):
    def test_large_verifier_context_uses_file_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(launcher, "_results_root", return_value=Path(tmp)), \
             patch.object(launcher, "ensure_agent_home"), \
             patch.object(launcher, "_agent_home", return_value=Path(tmp)):
            context = {"baseline_sha256": "baseline", "candidate_sha256": "candidate",
                       "baseline": {"proof": "x" * 500000}}
            def fake_run(cmd, **kwargs):
                self.assertLess(len(cmd[-1]), 10000)
                directory = Path(tmp) / "large"
                self.assertEqual(json.loads((directory / "improvement_input.json").read_text())["context"], context)
                (directory / "verification.json").write_text('{"verdict":"wrong"}')
                return SimpleNamespace(returncode=0)
            with patch.object(launcher.subprocess, "run", fake_run):
                self.assertEqual(launcher.run_codex_verification("large", "S", "P", context)["verdict"], "wrong")
            def tamper(cmd, **kwargs):
                result = fake_run(cmd, **kwargs)
                (Path(tmp) / "large" / "improvement_input.json").write_text("changed")
                return result
            with patch.object(launcher.subprocess, "run", tamper):
                from fastapi import HTTPException
                with self.assertRaises(HTTPException) as raised:
                    launcher.run_codex_verification("large", "S", "P", context)
                self.assertIn("input changed", raised.exception.detail)

    def test_end_to_end_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "bounds"
            project.mkdir()
            (project / "PROBLEM.md").write_text("Improve the upper bound on C.")
            baseline = root / "baseline.txt"
            baseline.write_text("C <= 10")
            with patch.dict(os.environ, DANUS_AGENTS_ROOT=tmp, DANUS_PROJECT_DIR=str(project),
                            DANUS_AUTHOR="high", DANUS_VERIFY_URL="http://test/verify"):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    main(["improvement", "init", "bounds", "--baseline-file", str(baseline),
                          "--criterion", "Smaller upper bound"])
                snapshot = json.loads(output.getvalue())
                self.assertEqual(snapshot, server.improvement_status("bounds"))
                def fake_run(run_id, statement, proof, improvement_context):
                    prompt = launcher.build_prompt(run_id, statement, proof, improvement_context)
                    self.assertIn("strict_improvement|not_improvement|unresolved", prompt)
                    self.assertIn(snapshot["baseline_sha256"], prompt)
                    self.assertEqual(improvement.digest(improvement_context["candidate"]),
                                     improvement_context["candidate_sha256"])
                    return {"verdict": "correct", "verification_report": {"gaps": [], "critical_errors": []},
                            "improvement_assessment": {
                                "baseline_sha256": improvement_context["baseline_sha256"],
                                "candidate_sha256": improvement_context["candidate_sha256"],
                                "verdict": "strict_improvement", "explanation": "The bound is smaller."}}
                def urlopen(request, timeout):
                    with TestClient(service.app) as client:
                        response = client.post("/verify", json=json.loads(request.data))
                    self.assertEqual(response.status_code, 200)
                    return io.BytesIO(response.content)
                with patch.object(service, "run_codex_verification", fake_run), \
                     patch.object(service, "_allocate_run_id", return_value="offline"), \
                     patch.object(server.urllib.request, "urlopen", urlopen):
                    result = server.improvement_submit("For every case in D, C <= 9.",
                        "By the complete assumed test derivation the bound is nine in every case in D.",
                        "Nine is strictly less than ten.", snapshot["baseline_sha256"])
                self.assertTrue(result["accepted"])
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    main(["improvement", "status", "bounds"])
                self.assertEqual(json.loads(output.getvalue())["accepted"][0]["fact_id"], result["fact_id"])

    def test_roles_preserve_worker_only_submission(self):
        self.assertIn("improvement_submit", tools_for("worker"))
        self.assertNotIn("improvement_submit", tools_for("main"))
        self.assertNotIn("fact_submit", tools_for("main"))
        self.assertIn("improvement_status", tools_for("main"))
        self.assertEqual(tools_for("verifier"), ["search_arxiv_theorems"])


if __name__ == "__main__":
    unittest.main()
