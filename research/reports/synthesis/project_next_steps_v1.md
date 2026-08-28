# Project Next Steps v1

## Immediate Goal

Move from AI-generated literature reports to verified, locally archived primary sources and manual reading notes.

---

## Step 1 — Create paper folders

Run in PowerShell:

```powershell
cd "<repository-root>"

New-Item -ItemType Directory -Path "research\papers\tier1_must_read" -Force | Out-Null
New-Item -ItemType Directory -Path "research\papers\tier2_core_sections" -Force | Out-Null
New-Item -ItemType Directory -Path "research\papers\tier3_supporting" -Force | Out-Null
New-Item -ItemType Directory -Path "research\papers\frontier_to_verify" -Force | Out-Null
New-Item -ItemType Directory -Path "research\notes" -Force | Out-Null
New-Item -ItemType Directory -Path "research\synthesis" -Force | Out-Null
```

---

## Step 2 — Download Tier 1 PDFs

Download these first:

1. `2024_chatdbg_augmented_debugging_llms.pdf`
2. `2025_debug_gym_text_based_interactive_debugging.pdf`
3. `2024_agentless_demystifying_llm_se_agents.pdf`
4. `2023_swe_bench_can_lms_resolve_github_issues.pdf`

Place them under:

```text
research\papers\tier1_must_read
```

---

## Step 3 — Create reading note template

```powershell
@'
# Paper Notes — <Paper Title>

## Bibliography

- Title:
- Authors:
- Year:
- Venue:
- DOI/arXiv:
- PDF path:
- Access level:

## Why this paper matters

## Problem

## Method

## System / architecture

## Tools

## Dataset / benchmark

## Metrics

## Key findings

## Limitations

## What applies to our project

## What does not apply

## Claims to verify

## One-paragraph summary in Turkish for my own understanding
'@ | Set-Content -Encoding UTF8 "research\notes\paper_notes_template.md"
```

---

## Step 4 — First manual reading task

Read ChatDBG first.

Create:

```text
research\notes\2024_chatdbg_notes.md
```

Focus sections:

- architecture,
- debugger command interface,
- “take the wheel” mechanism,
- PDB integration,
- evaluation setup,
- safety/security limits,
- what can be reused,
- what cannot be reused.

---

## Step 5 — First synthesis target

After reading ChatDBG + debug-gym + Agentless + SWE-bench, write:

```text
research\synthesis\pdb_debugger_agent_mvp_rationale.md
```

The synthesis should answer:

1. Why PDB first?
2. What exactly does debugger access add?
3. What is the strongest non-debugger baseline?
4. How should we measure improvement?
5. What is the smallest credible MVP?
