"""Verify every pinned SHA-256 row in the Friday delivery manifest against the
actual working-tree file bytes.  Exits nonzero on any mismatch.  Excludes only
the explicitly self-referential manifest row."""
import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "docs" / "FRIDAY_DELIVERY_MANIFEST_V1.md"


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
        target = REPO / path_str
        if not target.is_file():
            print(f"MISMATCH  {path_str}\n  expected: {expected}\n  actual:   FILE NOT FOUND")
            failures += 1
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
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