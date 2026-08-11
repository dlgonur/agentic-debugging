"""R5 — generalized raw debugger-informed repair matrix.

Self-contained experiment package derived from the accepted R3 treatment
(experiments/debugger_interaction_v2_r3, immutable).  R5 generalizes the
single-task R3 treatment to the complete tracked five-task curated set using:

- a neutral cwd-safe pytest launcher mechanically generated from the task's
  frozen reproduction argv (no hand-authored semantic runtime probe);
- original-source-region breakpoint derivation and stack filtering;
- a parameterized system prompt / patch affordance bound to the per-task
  writable production path;
- the accepted R3 staged debugger state machine, B->C metadata-only patch
  serialization normalization, real PatchManager, and the independent
  EvaluationVerifier.

R5 adapter binds exclusively to the R5 bridge.
"""
