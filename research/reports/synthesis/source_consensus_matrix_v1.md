# Source Consensus Matrix v1

This file consolidates sources found across Gemini, ChatGPT, and Claude reports.

Legend:

- G = Gemini
- C = ChatGPT
- Cl = Claude
- Priority = our cleaned priority after comparing the three reports

---

## 1. Strong Consensus Sources

| Priority | Source | Identifier | Found by | Why it matters | Manual action |
|---|---|---|---|---|---|
| Tier 1 | ChatDBG: Augmenting Debugging with Large Language Models | arXiv:2403.16354 | G, C, Cl | Closest direct prior art; LLM controls debugger and inspects runtime state | Read full |
| Tier 1 | debug-gym: A Text-Based Environment for Interactive Debugging | arXiv:2503.21557 | C, Cl | PDB-based environment for interactive debugging agents | Read full |
| Tier 1 | Agentless: Demystifying LLM-based Software Engineering Agents | arXiv:2407.01489 | G, C, Cl | Strong simple baseline against agentic complexity | Read full |
| Tier 1 | SWE-bench | arXiv:2310.06770 | C, Cl | Repository-level evaluation foundation | Read full |
| Tier 2 | LDB: Large Language Model Debugger / Debug Like a Human | arXiv:2402.16906 | C, Cl | Runtime-state-assisted debugging without full interactive debugger control | Read core method |
| Tier 2 | RepairAgent | arXiv:2403.17134 | G, C, Cl | Tool-using APR agent; SBFL/tool loop reference | Read method/tools |
| Tier 2 | AutoCodeRover | arXiv:2404.05427 | G, C, Cl | Structure-aware repository retrieval + optional SBFL | Read retrieval/SBFL sections |
| Tier 2 | SWE-agent | arXiv:2405.15793 | G, C, Cl | ACI design and repo-agent baseline | Read ACI sections |
| Tier 2 | OpenHands | arXiv:2407.16741 | G, C, Cl | Sandbox/event-stream platform; not debugger-first | Read architecture |
| Tier 2 | GenProg | DOI / TSE 2012 | G, C, Cl | Foundational generate-and-validate APR | Read core |
| Tier 2 | Zeller cause-effect chains / delta debugging | FSE 2002 | C, Cl | RCA/counterfactual foundation | Read full or core sections |
| Tier 2 | Defects4J | DOI: 10.1145/2610384.2628055 | G, C, Cl | Java FL/APR benchmark | Archive |
| Tier 2 | BugsInPy | ESEC/FSE 2020 | C, Cl | Most relevant real Python bug corpus | Archive |
| Tier 3 | QuixBugs | DOI: 10.1145/3135932.3135941 | G, C, Cl | Small Python/Java bug benchmark | Archive |
| Tier 3 | DebugBench | arXiv:2401.04621 | C, Cl | LLM debugging benchmark; leakage-aware | Read later |
| Tier 3 | Self-Debugging | arXiv:2304.05128 | C, Cl | LLM self-repair with execution feedback | Read later |
| Tier 3 | ChatRepair | DOI: 10.1145/3650212.3680323 | G, C | Conversational APR / test feedback | Read later |
| Tier 3 | SBFL survey | arXiv:1607.04347 or IEEE Access 2022 survey | G, C, Cl | Fault localization metrics and taxonomy | Read selected sections |
| Tier 3 | LLM4APR systematic review | arXiv:2405.01466 | C, Cl | Survey for LLM-based APR | Read taxonomy sections |

---

## 2. Claude-Heavy Frontier Sources to Verify

These are important but must be manually verified because Claude marked several as metadata/subagent-level.

| Priority | Source | Identifier | Report status | Why verify |
|---|---|---|---|---|
| Frontier | EnIGMA: Interactive Tools Substantially Assist LM Agents in Finding Security Vulnerabilities | arXiv:2409.16165 | Metadata / later web-verified existence | GDB/interactive tools, but CTF domain |
| Frontier | FramePilot / ADI: Empowering Autonomous Debugging Agents with Efficient Dynamic Analysis | arXiv:2604.24212 | Metadata / needs PDF | PDB/function-level dynamic analysis; very relevant if valid |
| Frontier | Debug2Fix | arXiv:2602.18571 | Metadata / later web-verified existence | Java + Python debugger subagent |
| Frontier | SWE-Doctor | arXiv:2607.00990 | Metadata / later web-verified existence | Runtime diagnosis from bug reproduction tests |
| Frontier | FixAgent / UniDebugger | arXiv:2404.17153 | Metadata / later web-verified existence | Multi-agent debugging claims |
| Frontier | MASAI | arXiv:2406.11638 | Metadata / later web-verified existence | Multi-agent SWE baseline |
| Frontier | AgentFL | arXiv:2403.16362 | Needs verification | Multi-agent fault localization |
| Frontier | LLM4FL | OpenReview/arXiv | Needs verification | Graph/RAG FL |

---

## 3. Sources That Are Useful But Not Central Now

| Source | Why not central now |
|---|---|
| HumanEvalFix | Function-level repair benchmark; cheaper but less realistic |
| GitBug-Java | Java-focused; useful later |
| ConDefects | Leakage-aware; useful for evaluation design |
| InferFix | RAG/static-analysis repair; useful for RAG comparison |
| Whyline | Foundational HCI/debugging explanation system |
| Liblit statistical debugging | Foundational statistical diagnosis |
| Daikon / dynamic invariants | Background for invariant-based debugging |
| SemFix / PAR / TBar / SequenceR | APR history; read through surveys first |

---

## 4. Metadata Corrections Required

| Issue | Status |
|---|---|
| ChatDBG authors differ across reports | Use official arXiv/FSE metadata, not Gemini’s source ledger |
| ChatDBG title appears as both “AI-Powered Debugging Assistant” and “Augmenting Debugging with Large Language Models” | Use the official FSE/arXiv version selected during manual download |
| Several 2026 papers only appeared through Claude/subagent metadata | Must download and read before citing as settled evidence |
| Gemini treats project TODO/workflow as source-ledger entries | Keep them as project context only, not scientific evidence |
