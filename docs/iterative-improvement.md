# Iterative bound improvement

This opt-in mode searches for progressively stronger bounds while preserving the
original question and every accepted result. Fixed-problem behavior is unchanged.
Worker counts (`danus new --roles ...`) and models (`config/danus.env`) retain
their existing controls and defaults. Ask the main agent to select your roster.

## Start

Ask the main agent to create a fresh project for your bound problem, using your
chosen workers and exploratory subagents. Supply the starting bound, its domain,
hypotheses and source, a precise definition of improvement, and the run budget.
After writing PROBLEM.md and a baseline text file, the main agent runs:

```bash
bin/danus improvement init <project> --baseline-file <baseline.txt> \
  --criterion "Strictly smaller upper bound on the same domain and hypotheses"
bin/danus improvement status <project>
```

Initialization does not start workers. Start and stop them with the normal Danus
controls. Restart the verify service and open a fresh main-agent session after
switching to this branch so both the new tools and verifier contract are loaded.
The main agent can run in Codex Desktop; non-interactive workers are unchanged.

## Acceptance and continuation

`improvement_status` supplies the frozen problem, baseline, criterion, accepted
proofs and `baseline_sha256`. Workers submit improved bounds through
`improvement_submit(statement, proof, improvement, baseline_sha256, ...)`.
The fresh verifier checks both proof correctness and strict improvement. It must
return a comparison bound to the exact candidate and baseline hashes, with no
proof errors or gaps. A legacy verifier omitting the comparison cannot promote.
Ordinary supporting lemmas continue through `fact_submit`; they do not change
the benchmark history. Starting bounds are operator-supplied context, not newly
verified claims; include their provenance and relevant proof/source material.

Each concurrent proposal uses a frozen snapshot. At promotion, a project lock
checks that the accepted baseline and cited predecessor proofs are unchanged.
Only one proposal against a given baseline can advance it. A stale proposal must
be compared again against the new history. Rejects, errors and unresolved
comparisons preserve the last accepted result. Accepted facts or dependencies
that are changed/revoked block further status/promotion until explicitly repaired;
there is no silent fallback to a possibly invalid bound.

Accepted improvements do not end the research loop. Use the existing deadline,
round limits and stop controls for the agreed budget. Exhaustion records best
found so far, never optimality. Resume the same project without reinitializing
or editing its original problem. A changed question requires a fresh project.

## Evidence and measurement

`<project>/improvement/state.json` is the authoritative accepted history. It
contains exact statements/proofs, baseline and candidate hashes, verifier
reports, source IDs, authors, and submission/verification/acceptance timestamps.
`improvement/attempts/*.json` records every dispatched candidate and outcome,
including failures and stale comparisons. A crash may leave an attempt marked
verifying; only entries in state.json count as accepted improvements. A crash
between fact writing and history promotion may leave a supporting fact without
an accepted bound; it must be resubmitted against the current baseline.

`danus improvement status <project>` prints JSON suitable for export. Compare
best accepted bounds at equal elapsed times and discovery-to-acceptance delays.
Use Codex rollout logs for model/effort, token usage, assignments and repeated
proof development; token counters are not subscription-quota percentages. Run
comparison projects from the same starting material with separate project state.
No extra debug logger, roster change or model change is required.

Verification is by an independent LLM, not a formal proof assistant. These tests
validate protocol and persistence; a live research benchmark is still required.
