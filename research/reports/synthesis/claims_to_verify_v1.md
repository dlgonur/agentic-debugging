# Claims to Verify v1

This file lists claims that should not be copied into the final technical report until manually verified from primary sources.

---

## 1. Metadata Corrections

### CTV-001 — ChatDBG authors

Issue: Gemini’s report appears to list ChatDBG with a different author set than official metadata.

Current resolution: use official arXiv/FSE metadata.

Action:
- Download ChatDBG PDF.
- Record exact title, authors, venue, DOI/arXiv.
- Correct all local notes.

---

## 2. Frontier Debugger-Control Claims

### CTV-002 — debug-gym benchmark numbers

Claim: Claude reports that Claude 3.7 Sonnet improves from 37.2% to 48.4% or 52.1% with delayed PDB access.

Status: plausible, but must read table in debug-gym PDF.

Action:
- Download debug-gym.
- Capture exact table, benchmark, model, and setting.

### CTV-003 — FramePilot / ADI

Claim: function-level PDB/ADI approach reaches 63.8% on SWE-bench Verified and improves mini-SWE-agent / AutoCodeRover.

Status: frontier claim; needs full PDF verification.

Action:
- Download arXiv:2604.24212.
- Verify authors, venue, exact benchmark setup, and whether it is truly PDB-based.

### CTV-004 — Debug2Fix

Claim: Python + Java debugger subagent improves coding-agent repair performance.

Status: arXiv exists; needs full reading.

Action:
- Download arXiv:2602.18571.
- Verify debugger tools, datasets, models, and improvement numbers.

### CTV-005 — SWE-Doctor

Claim: runtime diagnoses from multi-faceted bug reproduction tests improve SWE-bench Verified / Pro.

Status: arXiv exists; very recent.

Action:
- Download arXiv:2607.00990.
- Verify whether it actually uses PDB/Delve or only test-derived diagnosis.

### CTV-006 — EnIGMA

Claim: SWE-agent extension with interactive debugger tools improves CTF vulnerability solving.

Status: arXiv/OpenReview exists; domain is CTF, not repo bug repair.

Action:
- Download arXiv:2409.16165.
- Treat as tool-interface evidence, not direct bug-fix evidence.

---

## 3. Benchmark Validity Claims

### CTV-007 — SWE-bench leakage / weak tests

Claims:
- SWE-bench+ and related analyses report leakage / suspicious patches / weak tests.
- Some claims mention percentages such as 32.67%, 31.08%, 59.4%, or 94%.

Status: important but needs careful primary-source verification.

Action:
- Download SWE-Bench+ / contamination papers.
- Separate:
  - SWE-bench Full
  - SWE-bench Lite
  - SWE-bench Verified
  - SWE-bench Pro
  - SWE-rebench / related variants
- Do not merge numbers across datasets.

---

## 4. Architecture Claims

### CTV-008 — Fine-tuning necessity

Claim: SFT/DPO is mandatory for small local code models to use debugger tools.

Status: not yet supported strongly enough for MVP.

Decision:
- Keep SFT/DPO in long-term TODO.
- Do not start with it.
- Revisit after we have debugger trajectories and baseline results.

### CTV-009 — Multi-agent superiority

Claim: multi-agent systems improve debugging.

Status: mixed evidence.

Decision:
- Do not use multi-agent as default.
- Baseline first with single controller + deterministic tools + verifier.
- Later ablate multi-agent if needed.

### CTV-010 — Dynamic debugging always better

Claim: dynamic diagnosis/debugger access is absolutely superior.

Status: too strong.

Decision:
- Correct framing: debugger access is a potentially valuable evidence channel, but its benefit is model-, task-, and controller-dependent.
- Must measure against Agentless/AutoCodeRover-style baselines.

---

## 5. Questions Requiring Experiments

1. Does PDB state improve correct-fix rate over static/test-feedback baselines?
2. Does it improve root-cause explanation faithfulness, not just patch success?
3. Which runtime observations matter most: stack, locals, watch expressions, step traces, breakpoints?
4. When should the controller invoke PDB: immediately, after failed repair attempts, or only on uncertainty?
5. Can a small local model use debugger tools without harming performance?
6. What verifier best detects plausible-but-wrong patches?
