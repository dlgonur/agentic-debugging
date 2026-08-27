"""Local Project dummy — scripted model without PDB, for calculator fix.

Drives controller Reproduce -> Understand -> Patch -> Validate -> Done
using the same JSONL protocol as dummy_command_model.py but never
enters RuntimeEvidence, so PDB is not required. The patch file is read
from --data's patch_file.
"""

import json
import sys
from pathlib import Path

def _arg(name, default=""):
    for i, v in enumerate(sys.argv):
        if v == name and i+1 < len(sys.argv):
            return sys.argv[i+1]
    return default

def _read_request():
    line = sys.stdin.buffer.readline()
    return json.loads(line.decode("utf-8")) if line else {}

def _emit(payload):
    sys.stdout.write(json.dumps(payload)+"\n")
    sys.stdout.flush()

def _load_phase(state_dir):
    p = Path(state_dir) / "phase.json"
    return json.loads(p.read_text(encoding="utf-8")).get("phase","reproduce") if p.is_file() else "reproduce"

def _save_phase(state_dir, phase):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    (Path(state_dir)/"phase.json").write_text(json.dumps({"phase":phase}), encoding="utf-8")

def main():
    state_dir = _arg("--state-dir")
    data_path = _arg("--data")
    data = json.loads(Path(data_path).read_text(encoding="utf-8")) if data_path and Path(data_path).is_file() else {}
    req = _read_request()
    state = (req.get("controller") or {}).get("state")
    phase = _load_phase(state_dir)
    # Simple flow without PDB
    if state == "Reproduce":
        if phase == "reproduce":
            _save_phase(state_dir, "reproduce-check")
            _emit({"kind":"action","name":"run_reproduction","arguments":{"phase":"baseline"}})
            return 0
        _save_phase(state_dir, "understand-locate")
        _emit({"kind":"transition","target_state":"Understand","reason":"failure reproduced"})
        return 0
    if state == "Understand":
        if phase == "understand-locate":
            _save_phase(state_dir, "understand-window")
            _emit({"kind":"action","name":"find_function","arguments":{"name": data.get("symbol","add"), "path": data.get("file","calculator.py")}})
            return 0
        if phase == "understand-window":
            _save_phase(state_dir, "understand-hypothesis")
            _emit({"kind":"action","name":"get_source_window","arguments":{"path": data.get("file","calculator.py"), "line":1}})
            return 0
        if phase == "understand-hypothesis":
            _save_phase(state_dir, "understand-declare")
            _emit({"kind":"add_hypothesis","hypothesis_id": data.get("hypothesis_id","h1"),"statement": data.get("statement","bug"),"confidence":"low","evidence_refs":["observation:get_source_window"],"requires_runtime_evidence": False})
            return 0
        if phase == "understand-declare":
            _save_phase(state_dir, "understand-gate")
            _emit({"kind":"action","name":"express_root_cause_hypothesis","arguments":{"hypothesis_id": data.get("hypothesis_id","h1"),"statement": data.get("statement","bug"),"target_file": data.get("file","calculator.py"),"target_symbol": data.get("symbol","add"),"confidence":"low"}})
            return 0
        if phase == "understand-gate":
            _save_phase(state_dir, "patch-apply")
            _emit({"kind":"transition","target_state":"Patch","reason":"ready to patch"})
            return 0
    if state == "Patch":
        if phase == "patch-apply":
            patch_file = data.get("patch_file","")
            patch_text = Path(patch_file).read_text(encoding="utf-8") if patch_file and Path(patch_file).is_file() else ""
            _save_phase(state_dir, "patch-syntax")
            _emit({"kind":"action","name":"apply_patch","arguments":{"patch": patch_text}})
            return 0
        if phase == "patch-syntax":
            _save_phase(state_dir, "patch-validate")
            _emit({"kind":"action","name":"syntax_check","arguments":{}})
            return 0
        if phase == "patch-validate":
            _save_phase(state_dir, "validate-reproduce")
            _emit({"kind":"transition","target_state":"Validate","reason":"patch applied"})
            return 0
    if state == "Validate":
        if phase == "validate-reproduce":
            _save_phase(state_dir, "validate-regression")
            _emit({"kind":"action","name":"run_reproduction","arguments":{"phase":"post_patch"}})
            return 0
        if phase == "validate-regression":
            _save_phase(state_dir, "validate-classify")
            _emit({"kind":"action","name":"run_regression_tests","arguments":{}})
            return 0
        if phase == "validate-classify":
            _save_phase(state_dir, "validate-finish")
            _emit({"kind":"action","name":"classify_outcome","arguments":{}})
            return 0
        if phase == "validate-finish":
            obs = (req.get("controller") or {}).get("last_observation") or {}
            payload = obs.get("payload") if isinstance(obs, dict) else {}
            outcome = payload.get("outcome") if isinstance(payload, dict) else None
            if outcome == "RESOLVED":
                _emit({"kind":"transition","target_state":"Done","reason":"candidate resolved"})
            else:
                _emit({"kind":"transition","target_state":"Failed","reason": f"outcome {outcome}"})
            return 0
    _emit({"kind":"transition","target_state":"Failed","reason":"state mismatch"})
    return 0

if __name__ == "__main__":
    sys.exit(main())
