# GPT-OSS SWE-rebench V2 DEVQUAL-10 V4

Development qualification only. V4 is a new transport-envelope treatment
child of immutable DEVQUAL V3, using the same already-observed first ten tasks
in the same order.

V4 changes only the bounded command-adapter request envelope, durable numeric
request-size provenance, and preflight CLI plumbing. The provider route,
model identity, reasoning effort, timeout/retry policy, controller budgets,
source-context Patch gate, repository guidance, PatchManager, and official
verifier remain frozen from V3. Provider execution remains explicitly gated;
the default validation, preflight, authorization, and smoke paths perform
zero generation calls.
