# GPT-OSS SWE-rebench V2 DEVQUAL-10 V3

Development qualification only. V3 is the final GPT-OSS 20B treatment child
of immutable DEVQUAL V2, using the same already-observed first ten tasks.

V3 freezes typed command-adapter errors, source-context-before-Patch for
external isolated repositories, state-specific process guidance, and
`reasoning_effort=high`. Provider execution remains explicitly gated; the
default validation, preflight, authorization, and smoke paths perform zero
generation calls. PDB is unavailable by treatment contract and the official
independent verifier remains the sole correctness authority.
