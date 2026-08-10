# S4 — Definitive cp118 + Frozen RAG Treatment

This directory implements the missing definitive experiment condition:
**cp118 (the accepted tuned checkpoint) + the frozen repository RAG
treatment** over the frozen quix40 40-task cohort under the frozen v1.2.1
one-shot protocol.

## Purpose

Measurement, not rescue.  The only treatment difference vs the accepted
cp118 RAG-OFF result (40/40 extracted, 0/40 applied, 0/40 RESOLVED) is
**RAG OFF → frozen repository RAG ON**.  Positive, neutral and negative
results are all accepted and preserved.

## Files

| File | Role |
|---|---|
| `s4_contract.json` | The frozen contract (cp118 identity, cohort identities, RAG policy, protocol, budgets, STOP rule). Single source of truth. |
| `s4_identity.py` | cp118 adapter identity verification (accepted S2 convention; tree `65b5ed9a...`). |
| `s4_quixbugs.py` | QuixBugs frozen-revision acquisition (`4257f44b`) + anti-oracle scoped corpus (`python_programs/` + `python_testcases/` only). |
| `s4_corpus.py` | quix40 manifest/payload identity validation, per-task DebugTask projection, shared repo-mode index, frozen retrieval + RagContext assembly. |
| `s4_payload.py` | Verbatim frozen v1.2.1 payload builder, additive `RETRIEVED_CONTEXT` block injection, prompt/context budget recording (Amendment 2). |
| `s4_transport.py` | Identity-verified local cp118 one-shot transport (lazy load; greedy 4096). |
| `s4_eval.py` | Primary evaluation with the exact frozen C9 v1.2.1 CPU eval semantics (single-condition orchestration; P2P = NOT_RECORDED). |
| `s4_runner.py` | CLI: `validate` (offline, no model) / `generate` (live 40×1) / `eval` (frozen semantics). |
| `tests/unit/` | Focused offline tests. |

## Usage

```text
python experiments/cp118_rag_definitive/s4_runner.py validate \
    --output-dir experiments/cp118_rag_definitive/runs/run-1 \
    [--adapter-path <cp118 adapter dir>] [--count-tokens]

python experiments/cp118_rag_definitive/s4_runner.py generate \
    --output-dir experiments/cp118_rag_definitive/runs/run-1 \
    [--adapter-path <cp118 adapter dir>]

python experiments/cp118_rag_definitive/s4_runner.py eval \
    --output-dir experiments/cp118_rag_definitive/runs/run-1

python experiments/cp118_rag_definitive/s4_runner.py smoke-eval \
    --output-dir experiments/cp118_rag_definitive/runs/smoke
```

`generate` is the live stage and refuses to run on a dirty tracked working
tree — the owner source-freeze commit is the prerequisite (the accepted S1
flow: owner commit → committed-head validate-only → live run).  The
generation is bound to the source-freeze identity via `run-identity.json`
(source_commit_sha, branch, contract SHA256, adapter tree identity, cohort
identity, protocol identity); `eval` fails closed unless the generation
matches the current authorized source/contract identities.  `smoke-eval`
is a bounded offline evaluator smoke (canned candidate, no model) for
engineering validation only.

## Anti-oracle scoping

The frozen QuixBugs revision ships gold/fixed code
(`correct_python_programs/`, `correct_java_programs/`).  The retrieval
corpus is a scoped view of the frozen revision containing exactly
`python_programs/` (buggy source) and `python_testcases/` (tests);
everything else — including all gold code — is structurally absent and
unit-tested.  QuixBugs tests live under `python_testcases/`, which the
frozen repo-mode rule classifies as `kind=source` (the rule keys on a
literal `tests/` prefix); provenance records this honestly.

## Run outputs

`runs/` (gitignored): `raw/` + `meta/` (C9-compatible), `retrieval/`
(per-task RAG provenance), `S4_GENERATION_COMPLETE.json`,
`details.csv` / `summary.csv` / `failure_taxonomy_counts.csv`,
`evidence.json`, `RUN_SUMMARY.md`, `SHA256SUMS.txt`.
