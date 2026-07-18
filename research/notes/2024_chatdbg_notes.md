# Paper Notes — ChatDBG

## Bibliography

- Primary title: ChatDBG: Augmenting Debugging with Large Language Models
- arXiv title: ChatDBG: An AI-Powered Debugging Assistant
- Authors: Kyla H. Levin, Nicolas van Kempen, Emery D. Berger, Stephen N. Freund
- Year: 2024 arXiv preprint; 2025 PACMSE/FSE publication
- Venue: Proceedings of the ACM on Software Engineering, FSE 2025
- DOI: 10.1145/3729355
- arXiv: 2403.16354
- Local PDF path: research/papers/tier1_must_read/2024_chatdbg_ai_powered_debugging_assistant.pdf
- Access level: MANUAL_READING_IN_PROGRESS

## Why this paper matters

ChatDBG is the closest direct prior art for this project because it gives an LLM controlled access to real debuggers. Unlike repository-level repair agents that mostly read files, run shell commands, inspect tests, and edit code, ChatDBG lets the model query debugger state, inspect stack frames, and reason over live program execution.

For this project, ChatDBG is not just background literature. It is the main reference point for designing a Python/PDB-first debugger-assisted agent.

## Problem

Traditional debuggers expose useful runtime state, but the programmer still has to decide what to inspect, form hypotheses, navigate stack frames, understand large amounts of state, and infer the root cause.

LLMs can help, but ordinary LLM code repair does not automatically have access to runtime debugger state. ChatDBG tries to close that gap by integrating an LLM into the debugger itself.

## Core idea

ChatDBG lets the user ask natural-language debugging questions such as:

- Why did this crash?
- Why is this variable null?
- Why is this value different than expected?

The LLM can then “take the wheel” and issue debugger commands through controlled function calls. It observes debugger outputs, reasons over them, and returns an explanation or fix suggestion to the programmer.

## System / architecture

To fill after full reading:

- How the user command enters ChatDBG:
- What prompt/context is constructed:
- What debugger state is included initially:
- What function calls/tools are exposed to the LLM:
- How debugger command results are returned to the LLM:
- How control is returned to the user:
- How history/context truncation is handled:

## Debugger support

From the paper:

- Pdb for Python
- LLDB for native code
- GDB support
- WinDBG partial/subset support depending on version/source
- IPython/Jupyter support for Python workflows

To verify during full read:

- Exact support level per debugger
- Which features are full vs partial
- Whether Java/JDB appears only as background debugger context or actual supported integration
- Required setup for Python
- Required setup for C/C++/Rust/native targets

## Take-the-wheel mechanism

To fill after full reading:

- What “take the wheel” means technically:
- Which function calls are available:
- How unsafe commands are filtered or constrained:
- Whether commands are whitelisted:
- How prompt injection / arbitrary code execution risk is handled:
- What happens if the model requests invalid commands:
- What is reusable for our PDB adapter:

## Evaluation

To fill after full reading:

### Python evaluation

- Dataset/source of programs:
- Number of programs:
- Type of bugs:
- Baseline:
- Success definition:
- Single-query result:
- Follow-up result:
- Cost:
- Limitations:

### C/C++ evaluation

- Dataset/source:
- Bug types:
- Debugger used:
- Root-cause vs proximate-cause result:
- Success definition:
- Leakage risk:
- Limitations:

## Key findings

Initial extracted findings to verify:

1. LLM-controlled debugger access can help root-cause diagnosis.
2. A debugger-integrated LLM can inspect runtime state instead of guessing from static code.
3. Python/PDB is a practical first target because Python exposes source and runtime state without native debug symbols.
4. The evaluation is promising but not equivalent to SWE-bench-style repository issue resolution.
5. ChatDBG is interactive and assistant-like; our project needs to turn this into a more autonomous repair-and-validation loop.

## Limitations

To fill after full reading:

- Evaluation scale:
- Dataset representativeness:
- Reliance on proprietary GPT-4/OpenAI API:
- Security risks:
- Generalization to large repositories:
- Generalization to non-Python languages:
- Lack of direct comparison against Agentless/SWE-Agent/AutoCodeRover:
- Patch correctness limitations:

## What applies to our project

Likely reusable:

- PDB-first direction
- Debugger command abstraction
- Stack/frame/local-variable extraction
- “why?” style root-cause prompt
- LLM as hypothesis generator
- Tool results returned as structured observations
- Safety restrictions around debugger commands

## What does not apply directly

Likely not directly reusable:

- ChatDBG’s user-in-the-loop interaction as-is
- OpenAI-only model assumption
- Case-study-style evaluation as final proof
- Notebook/student-script-heavy Python evaluation as sole benchmark
- Direct claims of root-cause correctness without independent verifier

## Claims to verify

- Exact paper title/version differences between arXiv and FSE.
- Exact debugger support list.
- Exact Python success rates.
- Exact C/C++ root-cause/proximate-cause numbers.
- Whether GDB/WinDBG support is full or partial.
- Whether security mitigation is command sanitization, whitelisting, or both.
- Whether the implementation is suitable to inspect for our own PDB adapter design.

## Project decisions after reading

- [ ] Decide which ChatDBG features should be cloned conceptually.
- [ ] Decide which features are out of scope for MVP.
- [ ] Extract PDB command schema candidates.
- [ ] Extract safety rules for debugger command execution.
- [ ] Extract evaluation limitations to avoid in our own experiment.
- [ ] Add final summary to research/synthesis/pdb_debugger_agent_mvp_rationale.md.

## One-paragraph Turkish explanation for my own understanding

ChatDBG, klasik debuggerların sunduğu stack frame, değişken, source code ve execution state gibi bilgileri LLM tarafından sorgulanabilir hale getiren bir AI debugging assistant sistemidir. Temel farkı, modelin sadece statik kod veya test çıktısına bakmaması; debugger üzerinden canlı program durumunu inceleyebilmesidir. Bu yüzden bizim proje için en doğrudan önceki çalışma olarak görülmelidir. Ancak ChatDBG daha çok kullanıcıyla diyalog kuran bir debugging assistant’tır; bizim hedefimiz bunu PDB tabanlı, deterministic tool wrapper kullanan, patch üreten ve test/verifier ile sonucu doğrulayan daha araştırma-odaklı bir agentic debugging prototipine çevirmektir.
