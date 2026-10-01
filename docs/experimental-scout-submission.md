# Experimental scout proof relay

This child branch is intentionally separate from `iterative-improvement` and is
not intended for upstream merging. The verifier remains the sole correctness
gate. The change removes the mandatory worker reconstruction stage for a complete
scout proof. It does not change models, reasoning efforts, worker counts, or the
independent verifier. Incomplete arguments still benefit from worker development.

## Main-agent workflow

Use a fresh Desktop chat in this checkout after switching branches, and restart
the verifier service. Ask the main agent to run the same roster, models, effort
and budget as the comparison run, enabling iterative improvement for your bound
problem. Request separate project directories with identical starting material.
The main agent assigns distinct investigations; scouts do not launch workers or
recursively delegate. No Desktop-specific code or worker UI is required.

A scout saves a JSON file inside the selected project, for example
`candidates/scout-bound-1.json`, then sends its path to the main agent. Example
schema (replace the placeholder proof with a complete proof):

```json
{
  "producer": "/root/cube_topology",
  "statement": "A precise improved bound with full domain and hypotheses.",
  "proof": "The complete proof, including the comparison argument.",
  "predecessors": [],
  "glossary_introduces": {},
  "source_id": "optional shared-memory finding ID",
  "improvement": "Explain the strict gain over the starting and accepted bounds.",
  "baseline_sha256": "copy from improvement_status"
}
```

Optional fields also include `intuition`, `external_refs`, and `discovered_at`
(a producer-reported timestamp). For a supporting lemma, omit both `improvement`
and `baseline_sha256`; that uses the ordinary proof gate and does not advance
the benchmark. Only complete self-contained statements/proofs should be packaged.

The main invokes:

```text
candidate_submit(project="my-project", package_file="candidates/scout-bound-1.json")
```

The tool resolves the file inside that project (including symlink checks), freezes
its exact bytes, and preserves the statement/proof and producer attribution.
It supplies frozen predecessor proofs to a fresh verifier. A clean proof verdict
bound to the exact package hash is required; improved bounds additionally require the strict comparison and
unchanged-baseline checks from iterative-improvement. Errors, rejects, and stale
comparisons do not produce an accepted improvement. A source file edited after
submission cannot change the frozen proposal. The verifier's frozen input is also
checked for modification before its result is returned.

The main should relay the package without mathematical rewriting or a mandatory
independent derivation. It may assign repairs after rejection, or explicitly
commission adversarial checking where warranted. Facts remain speculative until
acceptance. This role routing is an orchestration policy, not an OS security
boundary; it does not constrain an agent with arbitrary filesystem permissions.

## Measurement

`<project>/candidate_submissions/<submission_id>/package.json` retains the exact
input. `receipt.json` records the producer, submitting gateway author, package
hash, route `scout_direct`, timestamps, source path, result, and any improvement
attempt ID. Declared discovery time is labeled separately from server timestamps.
The authoritative improvement timeline remains `improvement/state.json`; receipts
are provenance, not an alternative acceptance authority. A crash can leave a
receipt in verifying state: inspect the fact graph and improvement history before
retrying; do not treat the receipt itself as success.

Use these files together with Codex rollout logs to compare the two branches:
accepted bounds at equal elapsed times, candidate-to-acceptance delay, total
recorded usage, and repeated proof development. The logs support attribution of
specific handoffs; they cannot determine an exact percentage of wasted quota.
No temporary debug instrumentation needs to be removed later.

## Branch comparison

- `iterative-improvement`: workers submit improved bounds; existing scout-to-worker route.
- `experimental-scout-submission`: same improvement mode plus exact scout-package relay.

Restart the verifier and begin a fresh main chat on each branch. Do not run the
two variants concurrently from one checkout: the imported code and shared service
would not provide isolated architectures. Do not resume the same research project
as the other arm of a fair comparison; it already contains discoveries.
