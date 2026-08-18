# Experiments

Tracked experiment families for the accepted scientific and engineering
campaigns. Each family directory has a short note answering what it was and
what was learned.

- **Executive summary across all experiments:** [`RESULTS_SUMMARY.md`](RESULTS_SUMMARY.md)
- **Canonical conclusion map:** [`docs/results-index.md`](../docs/results-index.md)

Raw live-run trees under most `runs/` directories are gitignored. Accepted
numbers live in `docs/project-closeout.md` and `docs/final-report.md`. The
R6 frozen evidence capsule is the exception: it is tracked under
`r6_debugger_training/runs/frozen/`.

| Family | What it is | Entry |
| --- | --- | --- |
| `debugger_interaction_v2_r1/` | R1 repaired-interface breakpoint / first PDB observation | [README](debugger_interaction_v2_r1/README.md) |
| `debugger_interaction_v2_r2/` | R2 staged multi-turn debugger loop | [README](debugger_interaction_v2_r2/README.md) |
| `debugger_interaction_v2_r3/` | R3 debugger-informed patch → independent verifier | [README](debugger_interaction_v2_r3/README.md) |
| `model_generated_test_probe_r4/` | R4 model-generated regression test | [README](model_generated_test_probe_r4/README.md) |
| `debugger_interaction_v2_r5/` | R5 clean base-14B five-task holdout (plus historical r5.x matrices) | [README](debugger_interaction_v2_r5/README.md) |
| `r6_debugger_training/` | R6 debugger-oriented QLoRA SFT + disjoint validation; incomplete holdout | [README](r6_debugger_training/README.md) |
| `cp118_rag_definitive/` | S4 cp118 + frozen RAG; partial / NOT_EVALUATED | [RESULT](cp118_rag_definitive/RESULT.md) |
| `tuned_debugger_pilot_v1/` | Earlier frozen tuned-vs-RAW debugger pilot contract | [README](tuned_debugger_pilot_v1/README.md) |
| `local_inference_perf/` | Efficient-SDPA local inference packaging | [README](local_inference_perf/README.md) |
| `nemotron_3_nano_model_capability_probe/` | Selected Nemotron 3 Nano product-path capability probe; Harness V2 five-task 1/5 | [README](nemotron_3_nano_model_capability_probe/README.md) |
| `gpt_oss_swerebench_v2_pilot10/` | Frozen GPT-OSS SWE-rebench V2 Pilot-10 evaluation path; provider inference not run | [README](gpt_oss_swerebench_v2_pilot10/README.md) |

Related tracked synthesis (not runners):
[`analysis/s5_final_controlled_comparison/`](../analysis/s5_final_controlled_comparison/README.md),
[`research/quixbugs/`](../research/quixbugs/README.md),
[`docs/professor_traces/`](../docs/professor_traces/README.md).
