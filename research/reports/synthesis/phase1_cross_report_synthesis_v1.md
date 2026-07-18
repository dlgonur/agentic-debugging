# Phase 1 Cross-Report Synthesis v1

Date: 2026-07-18  
Inputs:

- Gemini_Agentic_Debugging_Literature_Review.pdf
- ChatGPT_Agentic_Debugging_Literature_Review.pdf
- Claude_Agentic_Debugging_Systems_Deep_Research_Literature_Review.pdf

Purpose: compare the three independently generated deep-research reports, extract consensus, mark disagreements, clean the reading list, and define the next research steps for the Agentic Debugging internship/project.

---

## 1. Executive Verdict

The three reports converge on the same core direction:

> The project should not become another static repository repair agent. The scientifically interesting target is a dynamic, debugger-assisted agent that can inspect runtime state through PDB first, then later potentially through GDB/LLDB or DAP.

Strong consensus:

1. **ChatDBG is the closest direct prior art.**
   - It is the clearest example of an LLM controlling a real debugger and inspecting runtime state.
   - It is the first paper to read fully.

2. **SWE-Agent, OpenHands, AutoCodeRover, Agentless, and RepairAgent are relevant but not equivalent to debugger agents.**
   - They mostly operate through repo navigation, shell commands, tests, and patching.
   - They are baselines and architectural references, not direct dynamic-debugging equivalents.

3. **Fault localization is not root-cause analysis.**
   - FL usually returns suspicious locations.
   - RCA requires causal explanation grounded in runtime evidence, data flow, control flow, or counterfactual verification.

4. **Passing tests does not prove patch correctness.**
   - APR literature repeatedly warns about plausible-but-incorrect patches and test-suite overfitting.
   - Any prototype needs a verifier, not just a patch generator.

5. **Python + PDB is the right first prototype target.**
   - It has the simplest debugger integration path.
   - ChatDBG supports PDB.
   - debug-gym is PDB-based.
   - BugsInPy gives real Python bugs.

6. **The MVP should be single-agent, deterministic-tool-heavy, and verifier-backed.**
   - Do not start with multi-agent orchestration.
   - Do not start with multi-language debugger adapters.
   - Do not start with DPO/RLHF.
   - Do not make fine-tuning the first engineering step.

---

## 2. Report-by-Report Assessment

### 2.1 Gemini Report

Strengths:

- Broad taxonomy and strong narrative from manual debugging to FL/APR/LLM/agentic debugging.
- Useful system coverage for ChatDBG, SWE-Agent, OpenHands, AutoCodeRover, Agentless, RepairAgent, and related work.
- Practical project implications section.
- Recognizes that mainstream SWE agents lack debugger-level runtime-state inspection.

Weaknesses / caution:

- Overassertive language in several places, e.g. dynamic diagnosis as “absolute superiority” and SFT/DPO as mandatory.
- Treats internal project documents as source-ledger items, which is acceptable for project context but not scientific evidence.
- Some table extraction / formatting is corrupted in the PDF.
- ChatDBG author metadata appears inconsistent with the official arXiv/FSE metadata.
- Several claims need source-level verification before being reused in a formal report.

Use Gemini mainly for:
- broad structure,
- terminology,
- project narrative,
- initial source discovery.

Do not use Gemini alone for:
- author metadata,
- exact benchmark numbers,
- claims that a technique is “scientifically proven” or “mandatory.”

### 2.2 ChatGPT Report

Strengths:

- Best balanced conceptual framing.
- Strong distinction between repository repair agents and debugger-mediated runtime diagnosis.
- Good caution around version drift: repo READMEs may advertise newer scores than paper-era results.
- Strong treatment of FL vs RCA and test-passing vs correctness.
- Good dataset framing: SWE-bench, BugsInPy, Defects4J, QuixBugs, DebugBench, HumanEvalFix, debug-gym.

Weaknesses / caution:

- Less aggressive than Claude in discovering 2025–2026 frontier debugger-control systems.
- Some source-ledger rows are PDF-extraction noisy.
- Several newer systems are treated cautiously or left for later reading.

Use ChatGPT mainly for:
- synthesis backbone,
- definitions,
- reliable comparison logic,
- research gap framing,
- initial MVP rationale.

### 2.3 Claude Report

Strengths:

- Best frontier-system discovery.
- Explicitly identifies two lineages:
  1. static/repository repair agents,
  2. interactive debugger-control systems.
- Captures debug-gym, EnIGMA, FramePilot/ADI, Debug2Fix, SWE-Doctor, FixAgent/UniDebugger, MASAI.
- Gives the strongest immediate project recommendation:
  - Python/PDB first,
  - single-agent first,
  - deterministic tools,
  - verifier,
  - defer fine-tuning and multi-agent until after baseline.
- Clearly labels some frontier sources as metadata/subagent-level rather than fully read.

Weaknesses / caution:

- Several frontier papers were not personally read by Claude; they were metadata/subagent-relayed.
- Claims around 2026 systems must be manually verified from the PDFs before becoming core evidence.
- Some very recent benchmark claims may shift or be contested.

Use Claude mainly for:
- frontier-source discovery,
- next-step priorities,
- “what not to build first” discipline,
- risk flags.

---

## 3. Cross-Report Consensus Claims

| Claim | Gemini | ChatGPT | Claude | Current Confidence | Notes |
|---|---:|---:|---:|---|---|
| ChatDBG is the closest direct prior art | Yes | Yes | Yes | High | Must read first. |
| Mainstream SWE agents are mostly repo/test repair agents, not debugger agents | Yes | Yes | Yes | High | Key conceptual boundary. |
| Fault localization does not equal root-cause analysis | Yes | Yes | Yes | High | Central thesis. |
| Passing tests does not prove semantic correctness | Yes | Yes | Yes | High | Requires verifier. |
| Dynamic/runtime evidence is valuable | Yes | Yes | Yes | Medium-High | Need experiments to quantify. |
| Python/PDB should be first prototype | Yes | Yes | Yes | High | Strongest project-level convergence. |
| Use Agentless as baseline | Yes | Yes | Yes | High | It is the simplicity/cost baseline. |
| Start with multi-agent design | No/unclear | No | No | Medium-High | Defer multi-agent. |
| Start with fine-tuning | Gemini leans yes | ChatGPT cautious | Claude says no | Mixed | Defer until baseline exists. |
| RAG is useful | Yes | Yes | Yes | Medium | Useful for repo context, not a replacement for debugger state. |
| DPO/RLHF is needed soon | Gemini says later/yes | Not near-term | No near-term | Low | Keep as late-phase TODO only. |

---

## 4. Key Disagreements and Resolution

### 4.1 Fine-tuning

Gemini suggests SFT/DPO is required if the final target is a small local open-source model.

Claude says fine-tuning is not justified initially; first prove tool-use and debugger value with prompting and strong models.

Resolution:

> Fine-tuning remains in the long-term TODO, but it is not part of the first MVP. First build and measure a debugger-assisted baseline. Then decide whether small-model SFT is worth the cost.

### 4.2 Multi-agent design

Gemini discusses multi-agent roles. Claude and ChatGPT both warn against assuming multi-agent superiority.

Resolution:

> Start with one controller agent plus deterministic tools plus a verifier. Multi-agent decomposition is a later ablation, not the first architecture.

### 4.3 Debugger-control frontier papers

Claude found several newer papers that ChatGPT/Gemini did not fully cover.

Resolution:

> Add them to the frontier verification queue. Do not treat their numbers as settled until PDFs are downloaded and read.

### 4.4 ChatDBG metadata

Gemini’s source ledger appears to list a different author set than the official metadata.

Resolution:

> Use official arXiv/FSE metadata for the final bibliography. Mark Gemini’s ChatDBG metadata as unreliable until corrected.

---

## 5. Unified Technical Position

The first real system should be:

```text
Python project / failing test or crashing script
  -> reproduce failure
  -> collect static context
  -> optionally run SBFL / simple localization
  -> enter PDB or post-mortem PDB
  -> collect stack, frame, locals, expressions, source snippets
  -> model forms hypothesis
  -> model requests more deterministic evidence if needed
  -> model proposes root-cause explanation
  -> model proposes patch
  -> deterministic patch application
  -> tests / verifier
  -> one or two bounded repair attempts
```

Recommended division of responsibility:

| Component | Responsibility |
|---|---|
| Deterministic tools | PDB commands, stack extraction, local variables, source snippets, tests, patch application, SBFL, sandboxing |
| Model | Hypothesis formation, evidence prioritization, causal explanation, patch drafting |
| Controller | Loop policy, budget, stopping, retry limits, safety gates |
| Verifier | Reproduction test, regression test, consistency between explanation and observed state, overfitting checks |

---

## 6. MVP Scope

In scope:

- Python only.
- PDB only.
- Reproducible failures.
- BugsInPy or curated small Python bug set.
- Static baseline comparison.
- Agentless-style baseline comparison.
- Runtime-state ablation: same task with and without PDB evidence.
- Metrics: localization, root-cause explanation, patch correctness, tests, cost, time, number of debugger actions.

Out of scope initially:

- GDB / LLDB / DAP portability.
- Multi-agent debate/reviewer systems.
- DPO/RLHF.
- Fine-tuning.
- Production observability/RCA.
- Full SWE-bench leaderboard pursuit.
- Non-reproducible bugs.
- Large-scale local-model deployment.

---

## 7. Immediate Research Plan

### Step 1 — Build the local paper library

Create:

```text
research/papers/
  tier1_must_read/
  tier2_core_sections/
  tier3_supporting/
  frontier_to_verify/
```

Download the Tier 1 PDFs first.

### Step 2 — Manual reading order

1. ChatDBG
2. debug-gym
3. Agentless
4. SWE-bench
5. LDB
6. RepairAgent
7. AutoCodeRover
8. SWE-agent
9. Zeller cause-effect chains
10. GenProg

### Step 3 — Produce reading notes

For each paper, create:

```text
research/notes/<year>_<shortname>_notes.md
```

Use this template:

```md
# Paper Notes — <title>

## Bibliography
## Why this paper matters
## Problem
## Method
## Tools / architecture
## Dataset / benchmark
## Metrics
## Key findings
## Limitations
## What applies to our project
## What does not apply
## Claims to verify
```

### Step 4 — First synthesis target

After the first four papers are read manually:

- ChatDBG
- debug-gym
- Agentless
- SWE-bench

write:

```text
research/synthesis/pdb_debugger_agent_mvp_rationale.md
```

---

## 8. Bottom Line

The correct near-term thesis is:

> Build a Python/PDB-first single-agent debugging prototype that tests whether interactive debugger access improves root-cause explanation and correct patch generation over strong repository/test-feedback baselines.

This gives a cleaner research contribution than simply fine-tuning another code model or building another SWE-bench agent.
