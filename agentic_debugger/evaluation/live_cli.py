"""Operator-only CLI for Task 10A live evaluation.

The command refuses to read live configuration unless both live selection and
explicit confirmation are present. It never prints provider output.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import LiveCaseStatus, LiveConfigurationError, LiveExecutionAuthorization, LiveModelConfig, LiveOptInError, LiveRunLimits, rejected_live_report, render_live_report, run_live_evaluation, validate_live_report

def build_parser():
    p=argparse.ArgumentParser(description="Explicitly authorized live model evaluation")
    p.add_argument("--repository-root",default="."); p.add_argument("--config",required=True); p.add_argument("--output",default="live-results.json"); p.add_argument("--human-output")
    p.add_argument("--live",action="store_true"); p.add_argument("--confirm-live-model-access",action="store_true")
    p.add_argument("--task-id",action="append"); p.add_argument("--policy",choices=[x.value for x in DemoPolicy],action="append"); p.add_argument("--run-label",help="safe report-visible label; a unique execution identity is appended")
    p.add_argument("--repetitions",type=int,default=1); p.add_argument("--max-model-requests",type=int,default=64); p.add_argument("--max-controller-steps",type=int,default=64); p.add_argument("--max-model-phase-seconds","--max-elapsed-seconds",dest="max_model_phase_seconds",type=int,default=900,help="per-case cumulative model-phase emergency guard checked between requests; not an active-response or whole-case deadline"); p.add_argument("--max-retries",type=int,default=2); p.add_argument("--max-response-bytes",type=int,default=1048576); p.add_argument("--stop-on-task-failure",action="store_true")
    return p

def main(argv=None):
    args=build_parser().parse_args(argv)
    human_path=Path(args.human_output) if args.human_output else Path(args.output).with_suffix(".txt")
    def write_validated(payload):
        validate_live_report(payload)
        Path(args.output).write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        human_path.write_text(render_live_report(payload),encoding="utf-8")
    if not args.live or not args.confirm_live_model_access:
        report=rejected_live_report("live execution rejected: explicit selection and confirmation are required")
        write_validated(report.to_mapping())
        print("live execution rejected; no model configuration was read",file=sys.stderr); return 2
    try:
        config=LiveModelConfig.from_file(args.config)
        limits=LiveRunLimits(args.max_model_requests,args.max_controller_steps,args.max_model_phase_seconds,args.max_retries,not args.stop_on_task_failure,args.max_response_bytes)
        report=run_live_evaluation(repository_root=args.repository_root,authorization=LiveExecutionAuthorization.authorize(True,True),config=config,limits=limits,task_ids=tuple(args.task_id) if args.task_id else None,policies=tuple(DemoPolicy(x) for x in args.policy) if args.policy else None,repetitions=args.repetitions,evaluation_id=args.run_label)
    except (LiveConfigurationError,LiveOptInError) as exc:
        report=rejected_live_report(str(exc))
        write_validated(report.to_mapping())
        print("live execution rejected; no provider was contacted",file=sys.stderr); return 2
    payload=report if isinstance(report,dict) else report.to_mapping()
    try:
        write_validated(payload)
    except LiveConfigurationError:
        print("live execution failed: internal report did not satisfy schema",file=sys.stderr); return 1
    if payload.get("evaluation_cleanup") == "failed" or any(case.get("status") == LiveCaseStatus.CLEANUP_FAILED.value for case in payload.get("cases",[])): return 1
    if payload.get("completion") != "complete": return 3
    if any(case.get("status") != LiveCaseStatus.RESOLVED.value for case in payload.get("cases",[])): return 1
    return 0

if __name__=="__main__":
    raise SystemExit(main())
