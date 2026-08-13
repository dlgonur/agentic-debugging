# Repository-Native RAG v1

**Date:** 2026-08-06
**Branch:** `goal/friday-rag-comparison-v1`
**Baseline:** `e92634e3dc016276d22ab9b9197adf4b28abbeb1`
**Package:** `agentic_debugger/rag/`
**Scope:** deterministic, offline, repository-native lexical RAG. No provider,
no network, no vector database, no new large dependency (stdlib only).

## 1. Purpose and boundaries

The RAG subsystem retrieves bounded, provenance-bound context for the
debugging agent from repository source files, test files, safe task/issue
descriptions and captured baseline failure output. It is:

* **deterministic** — index and retrieval artifacts are byte-stable and
  replayable from the artifact alone;
* **offline by construction** — it contains no provider, network, or model
  code;
* **bounded** — every cap below is enforced and unit-tested (fail-closed,
  never silent truncation);
* **architecture-aligned** — it reuses the repository's strict-schema,
  canonical-JSON, typed-error style and the accepted source-inspection
  helpers; it is not uncontrolled file concatenation RAG.

## 2. Corpus modes and sources

| Mode | Root | Documents |
|---|---|---|
| `fixture` (default) | curated task fixture directory | `*.py` outside `tests/` → `source`; `tests/**/*.py` → `test`; `task.json` → one `issue` document built from the **safe projection**; captured failure output → one `failure` document |
| `repo` (optional) | any declared root | `*.py` → `source`/`test` by path; `*.md` → `doc` when `include_docs`; no task binding |

The task/issue projection is an explicit whitelist (`task_id`, `title`,
`description`, `tags`). Oracle fields (`root_cause_summary`,
`target_files`, `target_symbols`, `runtime_evidence_hint`), the expected
patch, ground-truth localization and the evaluator-only `fixed_revision` are
structurally absent; a unit test proves they cannot enter the index.

Failure output is derived with `project_failure_output`: stable diagnostic
lines only (assertions, exceptions, FAILED markers, traceback frames),
pytest duration lines dropped, disposable-workspace paths normalized, capped
at 32 KiB with an explicit truncation marker.

## 3. Exclusion rules (declared, never silent)

Excluded directories: `.git`, `.opencode`, virtual environments
(`.venv`/`venv`/`env`), `__pycache__`, `.pytest_cache`, `.mypy_cache`,
`.ruff_cache`, `.tox`, `node_modules`, `_ai-review`, `operator`, `runs`,
`outputs`, `artifacts`, `checkpoints`, `models`, `tmp`, `temp`, `demo-out`,
and all hidden directories. Excluded files: hidden files, bytecode
(`.pyc`/`.pyo`), weight/checkpoint suffixes (`.pt`, `.pth`, `.safetensors`,
`.bin`, `.ckpt`, `.onnx`, `.pkl`), archives (`.zip`, `.gz`, `.tar`, `.tgz`),
images, executables, wheel/db/lock/log suffixes, and symlinks (rejected with
a typed error). Files not selected by the mode's selection policy are
counted as `not_selected` (explicit statistics, never silent). A *selected*
file that is oversized (> 512 KiB), binary, or undecodable is a fail-closed
error.

## 4. Chunking

* Parseable `source`/`test` documents: top-level Python symbols (classes,
  functions incl. decorator start line) become symbol chunks via the
  standard-library `ast` module — source is never executed or imported.
* Unparseable documents (or symbol-free files) and `issue`/`failure`/`doc`
  documents: deterministic line windows of 40 lines.
* A symbol wider than 200 lines is subdivided; a window whose text exceeds
  8 KiB is subdivided further; a single line wider than 8 KiB is a
  fail-closed error.
* Chunk fields: `chunk_id` (SHA-256 over canonical content+provenance),
  `document_id`, `kind`, normalized relative `path`, `start_line`,
  `end_line`, `text`.

## 5. Index artifact (`repository-index-v1`)

Strict versioned schema; no unknown fields; canonical JSON;
`allow_nan=False`; stable `index_id`; revision binding (`revision` = the
enclosing repository Git HEAD observed at build time) and source-tree
identity; fixture task binding when applicable; embedded chunk identities.
Bounds: chunk count ≤ 10 000, per-chunk text ≤ 8 KiB, serialized artifact
≤ 5 MiB (documented and tested; the curated fixtures serialize to a few
KiB). Fail-closed on load for: stale revision, malformed schema, duplicate
chunk identities, unsupported paths, oversized corpus, undecodable input.

## 6. Retrieval (`retrieval-result-v1`)

Deterministic lexical retrieval: identifier-aware tokenization
(`[a-z0-9_]+` after lowercasing), normalized query token frequencies,
integer score = dot product of query/chunk token counts, dedup by chunk
identity, deterministic tie order `(score desc, path asc, start_line asc,
chunk_id asc)`. Budgets: max results (default 8), per-chunk bytes, max
total context bytes (default 4 KiB); a single selected chunk larger than
the total budget is a fail-closed error; truncation is reported explicitly
(`result_count_truncated` / `context_bytes_truncated`).

Result fields: `index_id`, `query_identity`, selected `chunk_id`/`path`/
line ranges/`score`/`bytes`/`text`, truncation state, `selected_bytes`,
deterministic `retrieval_id`. Wall-clock `latency_ms` is reported
separately and never affects the identity.

## 7. Agent-facing context (`RagContext`)

The only cross-package surface. Bounded (default 4 KiB of chunk text),
unique chunk identities, deterministic `context_identity` (latency
excluded). Two mappings:

* `to_request_mapping()` — the additive `retrieved_context` block embedded
  in the live model public request only when RAG is explicitly enabled;
* `to_record_mapping()` — compact case/attempt evidence (identities,
  locations, byte counts, latency) without the chunk text.

`PUBLIC_REQUEST_BYTE_BUDGET = 20 000` mirrors the frozen transport
public-evidence budget; the live adapter enforces request-plus-context
before any transport call.

## 8. Determinism evidence

Unit tests pin: canonical identity stability, chunk identity stability,
corpus digest sensitivity, index serialization stability, retrieval
determinism (two builds → identical results/identities), latency
independence, and a two-run byte-identical integration demo (modulo the
declared timing fields).

## 9. Integrity hardening (repair 1, 2026-08-06)

* **Index**: on build and load every integrity field is recomputed and
  verified — final serialized size including `index_id`; unique document
  identities; every chunk references an existing document with matching
  kind/path; every chunk ID equals its recomputed content/provenance
  identity; the corpus digest equals the recomputed documents digest;
  document/chunk caps; the final `index_id` equals its recomputed identity.
  Tampering tests cover every identity field. Oversized text is rejected
  before reading/parsing where possible (file-size gate at corpus ingestion,
  artifact-size gate at index load).
* **Retrieval result**: on load and construction the query identity is
  recomputed from the query, the retrieval identity is recomputed from the
  deterministic payload, every selection byte count is verified against its
  text, selections are unique, the selection count respects the declared
  `max_results`, the total respects the declared `max_context_bytes`, and
  every selection is verified against the bound index (path/line/text). An
  arbitrary retrieval ID never loads.
* **RagContext**: every chunk reference is a strictly validated
  `RagChunkRef` (types, relative path, positive line range, score, text byte
  count, unique identities); the context binds the source retrieval
  identity; `context_identity` is recomputed over the full payload; the
  demo and live boundaries accept only a validated `RagContext` and reject
  arbitrary lookalike objects.
* **Full line coverage**: symbol-aware chunking now emits deterministic gap
  chunks for module docstrings, imports, module-level assignments and
  constants, code between symbols and trailing module text — every non-empty
  source/test line is represented by at least one chunk and no line is
  silently lost (proven by a dedicated module-coverage test).

## 10. Limits

* Lexical retrieval has no semantic understanding; relevance is
  token-overlap based and evaluated only by the harness (retrieval hit-rate
  vs oracle), never fed to the agent.
* In-process offline guard does not cover child pytest subprocesses (same
  scope as the accepted demo).
* No RAG performance claim is made from scripted demonstrations.
