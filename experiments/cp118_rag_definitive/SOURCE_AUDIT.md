# S4 — SOURCE AUDIT

## Scope and boundary

This experiment adds files only under `experiments/cp118_rag_definitive/`
plus its own focused tests.  The following are **not modified**:

- `agentic_debugger/rag/` — the frozen RAG implementation (corpus modes,
  retrieval policy, budgets, exclusions, identities) is used as-is;
- `agentic_debugger/comparison/` — the frozen comparison harness;
- production controller / debugger / verifier code;
- the historical raw-pilot protocol source under
  `experiments/raw-pilot-v1.1/scripts/` and `state/` (read-only inputs,
  consumed by path + import, never edited);
- `docs/`, `TODO.md`, `tests/` (root), `pyproject.toml`.

## Authoritative sources reused (read-only)

| Source | Used for |
|---|---|
| `agentic_debugger/rag/{schema,corpus,indexer,retrieval,context}.py` | frozen corpus/index/retrieval/context — imported, not copied |
| `agentic_debugger/comparison/native.py::build_task_query` | frozen query construction |
| `agentic_debugger/evaluation/task_schema.py::DebugTask` | issue projection vehicle |
| `experiments/raw-pilot-v1.1/scripts/RAW_C9_5Model_40Task_Protocol_v1_2_1_GPU_GENERATE_v1.py` | `OUTPUT_REQUIREMENTS_V121` + `build_v12_payload` (verbatim copies, drift-tested), frozen constants |
| `experiments/raw-pilot-v1.1/scripts/RAW_C9_5Model_40Task_Protocol_v1_2_1_CPU_EVAL_v1.py` | primary evaluation semantics (imported at runtime) |
| `experiments/raw-pilot-v1.1/state/quix40-v1/pilot_manifest_frozen_v1.jsonl` + `payloads/` + `artifacts/quix40-v1-state.zip` | frozen cohort (hash-verified) |
| commit `1bd90a2` (branch `experiment/cp118-debugger-d1`), read via `git show` | accepted S2 cp118 adapter-identity convention (re-implemented in `s4_identity.py`, same semantics) |
| `experiments/tuned_debugger_pilot_v1/run_pilot.py` | established tuned-pilot loading mechanism (4-bit NF4 double-quant + PeftModel) |

## Decisions recorded

1. **Corpus mode = `repo`** (Amendment 1).  The accepted RAG
   implementation supports indexing an actual repository tree with
   `build_corpus(mode="repo")`; the S4 treatment measures repository RAG
   over the frozen QuixBugs revision `4257f44b`, scoped to
   `python_programs/` + `python_testcases/` so that gold/fixed code
   (`correct_python_programs/` etc.) is structurally absent.  Fixture mode
   was not used: repository evidence shows it is the previously frozen
   treatment only for the curated two-task offline comparison demo, and
   quix40 tasks carry no `task.json`.
2. **`PUBLIC_REQUEST_BYTE_BUDGET = 20 000` scope** (Amendment 2).  Source
   (`agentic_debugger/evaluation/live.py` guard + tests) proves it bounds
   only the `LiveModelAdapter` agentic public-request mapping.  The
   one-shot generation prompt keeps the frozen v1.2.1 budget
   (`max_prompt_tokens = 24 576`, `max_new_tokens = 4096`); base/context/
   assembled byte and token sizes are recorded independently and the
   runner fails closed if the frozen constraints cannot coexist.
3. **Primary evaluation** (Amendment 3, Repair Pass 1).  The exact frozen
   v1.2.1 CPU evaluation functions are imported from the frozen script
   whose SHA-256 is pinned in the contract and verified fail-closed before
   import; only the orchestration shape adapts (single model condition,
   local worktree root, oracle-sanity preserved).  `test_pass` is the
   fail-to-pass basis; P2P is NOT_RECORDED (the frozen evaluator never ran
   pass-to-pass).
4. **Adapter identity** (Amendment 4).  `s4_identity.py` replicates the
   accepted S2 convention (per-file SHA-256 + size, no extra files, tree
   identity over `rel\0digest\0` in the accepted sort order, declared
   base) and must reproduce `65b5ed9a...`; the contract block is the
   single source of truth.
5. **Subprocess contract** (Repair Pass 1).  `s4_quixbugs.run_cmd` returns
   `subprocess.CompletedProcess`; every `s4_eval` call site reads
   `.returncode`/`.stdout`/`.stderr` (regression-tested).  The frozen
   script's own helper returns `(rc, merged)` — the semantics are
   identical.
6. **LF-explicit evidence writes** (Repair Pass 1).  On Windows,
   `Path.write_text` translates `\n` → `\r\n`, which broke `git apply` on
   the patch file; all S4 text artifacts (raw, meta, retrieval records,
   patch files, identities, markers) are written LF-explicit via
   `s4_payload.atomic_write_text`, and the QuixBugs checkout forces
   `core.autocrlf=false` so the corpus/worktree bytes equal the canonical
   frozen-revision content.
7. **Generation→eval source binding + resume/retry** (Repair Pass 1).
   `generate` records an immutable `run-identity.json` (source_commit_sha,
   branch, contract SHA256, adapter tree identity, cohort manifest,
   protocol identity); `eval` fails closed unless the generation matches
   the current authorized identities.  Valid completed pairs are never
   regenerated; partial/corrupt pairs fail closed; infrastructure-only
   failures retry the current task up to the frozen limit with identical
   settings; the completion marker is written only after all 40 valid
   pairs exist.  Timing bounds are declared as observational/stop-policy
   limits (no hard enforcement mechanism is claimed).
8. **Immutable run identity on resume** (Repair Pass 2, Blocker 1).  The
   identity (including `created_at` and `run_identity_sha256`) is created
   exactly once; `write_or_verify_run_identity` validates an existing
   stored identity (self-consistency of the stored hash included) and
   REUSES it verbatim — resumed task metas and the completion marker bind
   the same stored SHA.  Regression: two-invocation byte-identical hash.
9. **Pre-completion retry vs post-completion no-retry** (Repair Pass 2,
   Blocker 2).  Retries wrap ONLY the model-generation call (CUDA OOM
   normalized into `TransportError`); deterministic assembly precedes and
   post-completion persistence/validation follows, executing EXACTLY once
   — any post-completion failure aborts without regenerating.  Contract
   `retry_scope` matches the implemented policy.
10. **Pair provenance** (Repair Pass 2, Blocker 3).  `pair_is_valid` binds
    `meta.run_identity_sha256` to the immutable run identity and validates
    retrieval evidence (parseable JSON, task_id match, RAG provenance
    consistent with meta); stale/foreign pairs fail closed.
11. **Clean eval source state** (Repair Pass 2, Blocker 4).  `eval` fails
    closed on a dirty tracked tree before any evaluation.

## Provenance chain

`quix40 manifest (57208248...) + payloads (per-task payload_sha256) →
v1.2.1 payloads (frozen builder) → scoped frozen-revision corpus
(tree identity) → shared repo-mode index (index_id, revision-bound) →
per-task retrieval (retrieval_id, query_identity) → RagContext
(context_identity) → assembled request (assembled_prompt_sha256) →
raw/meta/retrieval evidence → frozen-semantics evaluation rows.`
