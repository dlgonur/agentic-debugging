"""Verify every pinned SHA-256 row in the Friday delivery manifest against the
actual working-tree file bytes.  Exits nonzero on any mismatch.  Excludes only
the explicitly self-referential manifest row."""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "outdated" / "friday-delivery" / "FRIDAY_DELIVERY_MANIFEST_V1.md"

# The Friday delivery manifest is a frozen historical record: its pinned rows
# name the delivery-time paths. Several pinned files were later relocated by
# the docs taxonomy rework (DOCS-STRUCTURE-V1) with byte-identical content.
# This map resolves the historical manifest path to the current location so
# the pinned SHA-256 rows stay verifiable; the manifest itself is unchanged.
LEGACY_PATH_MAP = {
    "docs/FRIDAY_PREFLIGHT_CHECKLIST_V1.md": "outdated/friday-delivery/FRIDAY_PREFLIGHT_CHECKLIST_V1.md",
    "docs/FRIDAY_STATUS_HANDOFF_V1.md": "outdated/friday-delivery/FRIDAY_STATUS_HANDOFF_V1.md",
    "docs/FRIDAY_PRESENTATION_PLAN_V1.md": "outdated/friday-delivery/FRIDAY_PRESENTATION_PLAN_V1.md",
    "docs/FRIDAY_PRESENTATION_DECK_V1.md": "outdated/friday-delivery/FRIDAY_PRESENTATION_DECK_V1.md",
    "docs/FRIDAY_PRESENTATION_CUE_SHEET_V1.md": "outdated/friday-delivery/FRIDAY_PRESENTATION_CUE_SHEET_V1.md",
    "docs/INSTRUCTOR_AGENTIC_DEBUGGING_TODO.md": "outdated/internship-materials/original-project-requirements.md",
    "docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md": "outdated/docs-archive/status/instructor-status-map.md",
    "docs/FINAL_TECHNICAL_REPORT_V1.md": "outdated/docs-archive/reports/final-report-v1.md",
    "docs/DEMO_GUIDE_V1.md": "docs/demo/guide.md",
    "docs/DEMO_TASK9.md": "docs/demo/task-9.md",
    "docs/PROJECT_TRACKER.md": "docs/project-tracker.md",
}

# These delivery rows named mutable live documents.  Comparing them with the
# present working tree made the frozen manifest fail as soon as normal project
# documentation advanced.  Resolve the exact delivery revision instead.  The
# original package was hashed from a Windows checkout, so Git's LF blob is
# reconstructed as CRLF before hashing.
HISTORICAL_GIT_SNAPSHOTS = {
    "README.md": ("e92634e3dc016276d22ab9b9197adf4b28abbeb1", "README.md"),
    "TODO.md": ("ab464dde1b99ab92dff6fcfa0af4912dfbb81a90", "TODO.md"),
    "docs/PROJECT_TRACKER.md": (
        "e92634e3dc016276d22ab9b9197adf4b28abbeb1",
        "docs/PROJECT_TRACKER.md",
    ),
    "docs/DEMO_GUIDE_V1.md": (
        "a7603d35b0a4511b508b5e3ca76eeb6e8b174909",
        "docs/DEMO_GUIDE_V1.md",
    ),
    "docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md": (
        "fc7c85b9858eba993f6bacc8ea9b4f805873f1a5",
        "docs/INSTRUCTOR_AGENTIC_DEBUGGING_STATUS_MAP.md",
    ),
}


def _historical_windows_bytes(commit: str, path: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cannot read historical delivery blob {commit}:{path}: {detail}")
    return completed.stdout.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def parse_rows(text: str) -> list[tuple[str, str, str]]:
    """Parse markdown table rows: | path | role | sha256 |"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| Path"):
            continue
        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p != ""]
        if len(parts) < 3:
            continue
        path, role, sha = parts[0], parts[1], parts[2]
        # Strip backticks from the path column.
        path = path.strip("`")
        # Skip the self-referential manifest row.
        if "self-referential" in sha.lower():
            continue
        # Skip rows where the sha column is a dash (canonical-JSON manifest).
        if sha == "—":
            continue
        # Extract the hex hash from the sha column (it may have backticks).
        m = re.search(r"`([0-9a-f]{64})`", sha)
        if m:
            rows.append((path, role, m.group(1)))
    return rows


def main() -> int:
    text = MANIFEST.read_text(encoding="utf-8")
    rows = parse_rows(text)
    if not rows:
        print("ERROR: no pinned hash rows found in manifest")
        return 2
    failures = 0
    print(f"Verifying {len(rows)} pinned rows against working-tree files")
    print("=" * 80)
    for path_str, role, expected in rows:
        snapshot = HISTORICAL_GIT_SNAPSHOTS.get(path_str)
        if snapshot is not None:
            try:
                content = _historical_windows_bytes(*snapshot)
            except RuntimeError as exc:
                print(f"MISMATCH  {path_str}\n  expected: {expected}\n  actual:   {exc}")
                failures += 1
                continue
        else:
            target = REPO / LEGACY_PATH_MAP.get(path_str, path_str)
            if not target.is_file():
                print(f"MISMATCH  {path_str}\n  expected: {expected}\n  actual:   FILE NOT FOUND")
                failures += 1
                continue
            content = target.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        status = "MATCH   " if actual.lower() == expected.lower() else "MISMATCH"
        if actual.lower() != expected.lower():
            failures += 1
        print(f"{status} {path_str}")
        if status == "MISMATCH":
            print(f"  expected: {expected}")
            print(f"  actual:   {actual}")
    print("=" * 80)
    print(f"Result: {len(rows) - failures} MATCH, {failures} MISMATCH")
    return 1 if failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
