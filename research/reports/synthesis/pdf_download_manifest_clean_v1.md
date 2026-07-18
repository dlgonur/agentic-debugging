# Clean PDF Download Manifest v1

Download these into:

```text
C:\Users\benya\Desktop\Projects\agentic-debugging-internship\research\papers
```

Suggested folder structure:

```text
research\papers\tier1_must_read
research\papers\tier2_core_sections
research\papers\tier3_supporting
research\papers\frontier_to_verify
```

---

## Tier 1 — Must Read Completely

| Priority | Suggested filename | Title | Authors | Year | Identifier | Folder | Why download |
|---|---|---|---|---:|---|---|---|
| 1 | 2024_chatdbg_augmented_debugging_llms.pdf | ChatDBG: Augmenting Debugging with Large Language Models | Kyla Levin, Nicolas van Kempen, Emery D. Berger, Stephen N. Freund | 2024/2025 | arXiv:2403.16354 | tier1_must_read | Direct prior art for debugger-controlling LLM |
| 1 | 2025_debug_gym_text_based_interactive_debugging.pdf | debug-gym: A Text-Based Environment for Interactive Debugging | Xingdi Yuan et al. | 2025 | arXiv:2503.21557 | tier1_must_read | PDB environment / interactive debugging benchmark |
| 1 | 2024_agentless_demystifying_llm_se_agents.pdf | Agentless: Demystifying LLM-based Software Engineering Agents | Chunqiu Steven Xia et al. | 2024/2025 | arXiv:2407.01489 | tier1_must_read | Strong non-debugger baseline |
| 1 | 2023_swe_bench_can_lms_resolve_github_issues.pdf | SWE-bench: Can Language Models Resolve Real-World GitHub Issues? | Carlos E. Jimenez et al. | 2023/2024 | arXiv:2310.06770 | tier1_must_read | Evaluation foundation |

---

## Tier 2 — Read Core Sections

| Priority | Suggested filename | Title | Authors | Year | Identifier | Folder | Why download |
|---|---|---|---|---:|---|---|---|
| 2 | 2024_ldb_debug_like_a_human.pdf | LDB: A Large Language Model Debugger via Verifying Runtime Execution Step by Step | Zhong, Wang, Shang | 2024 | arXiv:2402.16906 | tier2_core_sections | Runtime-state extraction before full debugger control |
| 2 | 2024_repairagent_autonomous_llm_program_repair.pdf | RepairAgent: An Autonomous, LLM-Based Agent for Program Repair | Bouzenia, Devanbu, Pradel | 2024/2025 | arXiv:2403.17134 | tier2_core_sections | Tool-using APR loop + Defects4J |
| 2 | 2024_autocoderover_autonomous_program_improvement.pdf | AutoCodeRover: Autonomous Program Improvement | Yuntong Zhang, Haifeng Ruan, Zhiyu Fan, Abhik Roychoudhury | 2024 | arXiv:2404.05427 | tier2_core_sections | Structure-aware retrieval + SBFL |
| 2 | 2024_swe_agent_agent_computer_interfaces.pdf | SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering | John Yang et al. | 2024 | arXiv:2405.15793 | tier2_core_sections | ACI design / repo-agent baseline |
| 2 | 2002_zeller_isolating_cause_effect_chains.pdf | Isolating Cause-Effect Chains from Computer Programs | Andreas Zeller | 2002 | FSE 2002 | tier2_core_sections | Causal/RCA foundation |
| 2 | 2012_genprog_automatic_program_repair.pdf | GenProg / TSE automated program repair lineage | Le Goues, Nguyen, Forrest, Weimer | 2012 | DOI: 10.1109/TSE.2011.104 | tier2_core_sections | APR foundation |
| 2 | 2024_llm4apr_systematic_review.pdf | A Systematic Literature Review on Large Language Models for Automated Program Repair | Zhang et al. | 2024 | arXiv:2405.01466 | tier2_core_sections | LLM APR taxonomy |
| 2 | 2024_openhands_generalist_agents.pdf | OpenHands: An Open Platform for AI Software Developers as Generalist Agents | Wang et al. | 2024/2025 | arXiv:2407.16741 | tier2_core_sections | Sandbox/event-stream architecture |
| 2 | 2020_bugsinpy_benchmark.pdf | BugsInPy | Widyasari et al. | 2020 | ESEC/FSE 2020 | tier2_core_sections | Python real-bug benchmark |
| 2 | 2014_defects4j_database.pdf | Defects4J | Just, Jalali, Ernst | 2014 | DOI: 10.1145/2610384.2628055 | tier2_core_sections | Canonical Java FL/APR benchmark |

---

## Tier 3 — Supporting Sources

| Priority | Suggested filename | Title | Identifier | Folder | Why download |
|---|---|---|---|---|---|
| 3 | 2024_debugbench_llm_debugging.pdf | DebugBench | arXiv:2401.04621 | tier3_supporting | Debugging benchmark / leakage-aware |
| 3 | 2023_self_debugging_llm.pdf | Self-Debugging | arXiv:2304.05128 | tier3_supporting | Execution-feedback LLM debugging |
| 3 | 2024_chatrepair_conversational_apr.pdf | Automated Program Repair via Conversation | DOI: 10.1145/3650212.3680323 | tier3_supporting | Conversational APR |
| 3 | 2016_sbfl_survey.pdf | Spectrum-Based Software Fault Localization Survey | arXiv:1607.04347 | tier3_supporting | FL foundations |
| 3 | 2017_quixbugs.pdf | QuixBugs | DOI: 10.1145/3135932.3135941 | tier3_supporting | Small Python/Java bug set |
| 3 | 2023_inferfix_rag_program_repair.pdf | InferFix | arXiv:2303.07263 | tier3_supporting | RAG/static-analysis repair |
| 3 | 2008_whyline_debugging_reinvented.pdf | Whyline / Debugging Reinvented | Ko and Myers | tier3_supporting | Debugging explanation foundation |
| 3 | 2005_scalable_statistical_bug_isolation.pdf | Scalable Statistical Bug Isolation | Liblit et al. | tier3_supporting | Statistical debugging foundation |

---

## Frontier to Verify

Do not use these as settled evidence until manually downloaded and read.

| Priority | Suggested filename | Title | Identifier | Folder | Why verify |
|---|---|---|---|---|---|
| F | 2024_enigma_interactive_tools_security.pdf | EnIGMA: Interactive Tools Substantially Assist LM Agents in Finding Security Vulnerabilities | arXiv:2409.16165 | frontier_to_verify | Interactive debugger tools, but CTF domain |
| F | 2026_framepilot_adi_dynamic_analysis.pdf | Empowering Autonomous Debugging Agents with Efficient Dynamic Analysis / FramePilot ADI | arXiv:2604.24212 | frontier_to_verify | PDB/function-level dynamic analysis |
| F | 2026_debug2fix_interactive_debugging_subagent.pdf | Debug2Fix | arXiv:2602.18571 | frontier_to_verify | Python/Java debugger subagent |
| F | 2026_swe_doctor_runtime_diagnosis.pdf | SWE-Doctor | arXiv:2607.00990 | frontier_to_verify | Runtime diagnosis from bug reproduction tests |
| F | 2024_fixagent_unidebugger_multi_agent.pdf | A Unified Debugging Approach via LLM-Based Multi-Agent Synergy / FixAgent | arXiv:2404.17153 | frontier_to_verify | Multi-agent debugging claims |
| F | 2024_masai_modular_se_agents.pdf | MASAI | arXiv:2406.11638 | frontier_to_verify | Multi-agent SWE baseline |
| F | 2024_agentfl_project_level_fault_localization.pdf | AgentFL | arXiv:2403.16362 | frontier_to_verify | Multi-agent fault localization |
| F | 2025_llm4fl_graph_rag_fault_localization.pdf | LLM4FL | OpenReview/arXiv | frontier_to_verify | Graph/RAG FL |
