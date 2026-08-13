# RAG / Comparison / Preference Decision Record v2

**Date:** 2026-08-06
**Branch:** `goal/friday-rag-comparison-v1`
**Baseline:** `e92634e3dc016276d22ab9b9197adf4b28abbeb1`
**Supersedes (partially):** `docs/evaluation/model-rag-sft-dpo.md`
(2026-07-31) — RAG decision line only.

## 1. Status of the v1 decision gate

The v1 gate recorded **Repository RAG: NO-GO-FOR-NOW** with the reason "no
real-model baseline exists yet for retrieval to be compared against". That
verdict applied to the earlier scope: *research* RAG, i.e. a RAG system whose
value would be measured against a real model's performance.

## 2. What this sprint authorizes

The current task explicitly authorizes an **offline, repository-native,
infrastructure-only RAG sprint**: deterministic lexical indexing/retrieval,
optional context injection at the agent/model boundary, a unified comparison
harness, and a preference-pair exporter — all provider-free and evaluated on
curated deterministic tasks.

**The v1 NO-GO is superseded only for this authorized offline infrastructure
scope.** Nothing in this sprint changes the standing positions on:

* real-model comparison (still no real base/tuned generation has been
  imported and verified — synthetic `offline-deterministic-demo` identities
  are infrastructure evidence only);
* fine-tuned-plus-RAG performance (no claim is made; the fine-tuned
  condition in the demo is a synthetic stand-in);
* SFT (still DEFER);
* DPO/RLHF (still NO-GO-FOR-NOW; the exporter prepares future DPO work and
  performs none);
* live campaigns, provider access, model training, BugsInPy execution
  (all still gated exactly as before).

## 3. Decisions recorded

| Decision | Verdict | Scope |
|---|---|---|
| Repository-native lexical RAG (offline) | **APPROVED (infrastructure)** | deterministic index/retrieval over fixture-scoped corpus (default) and declared roots; documented bounds; provenance; no provider/network/vector DB |
| RAG context injection | **APPROVED (additive, opt-in)** | explicit `rag_context` at the model-adapter boundary; default requests byte-identical; 20 KiB public-request bound enforced; no model-family code |
| Unified comparison harness | **APPROVED (infrastructure)** | imported + native conditions through the existing verifier/controller/demo paths; normalized metrics; JSON/CSV/Markdown; delta vs baseline |
| Preference-pair exporter | **APPROVED (infrastructure)** | ordered verifier-backed rules; strict schema; leakage guards; JSONL + audit; no DPO/RLHF |
| RAG/agentic performance claims | **NOT AUTHORIZED** | scripted parity demo explicitly does not establish causal RAG improvement |

## 4. Evidence

Focused unit + integration tests and the deterministic two-task demo
(`python -m agentic_debugger.comparison demo`) record: zero provider and
zero network attempts, replay validity, cleanup, canonical fixture
immutability, byte-stable deterministic views, and verifier-decided outcomes
for correct and non-repair attempts. See `docs/architecture/repository-rag.md`,
`docs/evaluation/comparison-harness.md`, `docs/architecture/preference-export.md`.

## 5. Repair 1 (2026-08-06) — contract hardening

The same authorized scope received one contract-hardening pass that changed
no decision in this record:

* the candidate patch is strictly bound to the recorded raw output
  (`patch_extraction` exact/substring contract with reconstruction and
  hash verification at load);
* primary `evaluation` attempts are separated from `preference-fixture`
  attempts in aggregates, deltas and reports (the old synthetic
  `base 0.50 vs tuned 1.00` result is impossible by construction);
* RAG index/retrieval/RagContext artifacts recompute and verify every
  identity field on build and load, with tampering tests;
* free-form JSON payloads are recursively bounded (nested NaN/Infinity fails
  at schema load);
* imported-attempt telemetry and response bounds are reconciled (any valid
  artifact yields a valid attempt record; external generation provider/
  network telemetry is separated from local verification counters);
* preference-pair identity binds response/patch/evidence hashes and is
  verified on load; oracle contamination is checked on the full response
  before any storage bound;
* chunking preserves full module line coverage via deterministic gap
  chunks.

None of the standing gates changed: no real base-versus-tuned result, no
fine-tuned+RAG claim, no production preference corpus, no DPO/RLHF, no live
campaign, no provider execution.
