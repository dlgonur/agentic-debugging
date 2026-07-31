# BugsInPy Licensing and Redistribution Gate v1

Status: BLOCKED

This is an engineering compliance gate, not legal advice. The gate uses only
the vocabulary CLEAR, CLEAR_WITH_CONDITIONS, BLOCKED, and UNKNOWN. Public
visibility is not treated as permission.

## Scope and authority

This review covers the eight selected BugsInPy pilot tasks:

| Project | Tasks |
| --- | --- |
| FastAPI | bugs 1 and 9 |
| HTTPie | bugs 1 and 2 |
| tqdm | bugs 2 and 3 |
| thefuck | bugs 1 and 2 |

The authority is research/bugsinpy/PILOT_ELIGIBILITY_MANIFEST_V1.json, with
BugsInPy pinned to 11c5f1eea954a42132cfd06bf257766a7963e0fd. The selected
project URLs and buggy/fixed revisions are taken from that manifest. This pass
used bounded public GitHub API tree metadata and individual raw files at exact
revisions. Credential prompts were disabled; no complete repository was cloned
or refreshed, no dependency was installed, and no benchmark or upstream code
was executed.

The manifest remains the task-selection authority. This report is the
licensing and evidence-handling authority for the pilot.

The canonical machine-readable compliance record is
research/bugsinpy/BUGSINPY_LICENSE_GATE_V1.json. The ignored review-package
matrix is only a hash-verifiable copy of that tracked record; it is not a
separate authority. The manifest and validator use the tracked path by
default, so a clean checkout can resolve every dataset, project, and file
record without _ai-review/.

The offline validator (scripts/validate_bugsinpy_license_gate.py) is the
fail-closed enforcement layer for this gate. It requires exact equality
between the manifest authority revision, the resolved dataset record
revision, and the locked BugsInPy revision
11c5f1eea954a42132cfd06bf257766a7963e0fd, whose resolved tree evidence must
have returned HTTP 200 with no truncation and valid evidence hashes. Every
project record carries a stable repository identity (tiangolo/fastapi,
httpie/httpie, tqdm/tqdm, nvbn/thefuck); every file record must use its
project's identity, and each task's project name and URL must agree with its
resolved project record. Each task's licensing.project_verdict must exactly
equal its project record verdict, and its reviewed_license_record_ids must
cover exactly the project records for its own buggy and fixed revisions —
records from another bug or revision in the same project are rejected.

Expected license evidence is contract-derived, not mutable-record-derived.
One project-level artifact contract binds each project: FastAPI requires
LICENSE (record_kind license, SPDX MIT) per selected revision; HTTPie
requires LICENSE (license, SPDX BSD-3-Clause) and AUTHORS.rst
(attribution_notice, no SPDX) per selected revision; tqdm requires LICENCE
(license) per selected revision with the accepted file-scoped MIT/MPL-2.0
spdx_identifiers plus a non-empty project-level mixed-license scope note;
thefuck requires LICENSE.md (license, SPDX MIT). Expected records are
derived from each project's repository, its manifest-selected revisions, and
the required artifact paths — never from a task-ID-to-record-ID lookup
table. Every file record must carry a canonical lowercase 40-hex revision,
its project's repository, one of the required artifact paths with the
matching record_kind and SPDX metadata, and the exact URLs
https://raw.githubusercontent.com/<repository>/<revision>/<path> and
https://github.com/<repository>/tree/<revision>; each
repository/revision/path combination must be unique. Project records must
cover exactly the required artifact records for every selected revision
(rejecting missing artifacts, extra unselected revisions, and unused
records), and each task's reviewed_license_record_ids must be unique and
exactly equal the required artifact records for its own buggy and fixed
revisions, with HTTPie always carrying LICENSE and AUTHORS.rst for both
revisions.

## Reviewed revision matrix

The hashes below are SHA-256 hashes of the exact downloaded file bytes. A
repeated hash across buggy and fixed revisions is evidence that the reviewed
license or notice contents were identical at those pins; it is not a claim
that the project source was identical.

| Source | Role | Exact revision | Reviewed path | Exact source URL | SHA-256 | SPDX evidence |
| --- | --- | --- | --- | --- | --- | --- |
| BugsInPy | authority | 11c5f1eea954a42132cfd06bf257766a7963e0fd | no conventional license/notice path | https://github.com/soarsmu/BugsInPy/tree/11c5f1eea954a42132cfd06bf257766a7963e0fd | — | UNKNOWN |
| BugsInPy | authority metadata | same | README.md | https://raw.githubusercontent.com/soarsmu/BugsInPy/11c5f1eea954a42132cfd06bf257766a7963e0fd/README.md | 3e356958a2869c1a84511e8b7a4fd84bdbef9069f2d34cb18d4985fcb07efa83 | no license statement |
| FastAPI | buggy, bug 1 | 766157bfb4e7dfccba09ab398e8ec444d14e947c | LICENSE | https://raw.githubusercontent.com/tiangolo/fastapi/766157bfb4e7dfccba09ab398e8ec444d14e947c/LICENSE | 4ec89ffc81485b97fec584b2d4a961032eeffe834453894fd9c1274906cc744e | MIT |
| FastAPI | fixed, bug 1 | 3397d4d69a9c2d64c1219fcbf291ea5697a4abb8 | LICENSE | https://raw.githubusercontent.com/tiangolo/fastapi/3397d4d69a9c2d64c1219fcbf291ea5697a4abb8/LICENSE | 4ec89ffc81485b97fec584b2d4a961032eeffe834453894fd9c1274906cc744e | MIT |
| FastAPI | buggy, bug 9 | a7a92bc63768ccee3f3afc2b73b2c581928dfe75 | LICENSE | https://raw.githubusercontent.com/tiangolo/fastapi/a7a92bc63768ccee3f3afc2b73b2c581928dfe75/LICENSE | 4ec89ffc81485b97fec584b2d4a961032eeffe834453894fd9c1274906cc744e | MIT |
| FastAPI | fixed, bug 9 | c5817912d2be25bb310bf9da517882f57bbe7bb5 | LICENSE | https://raw.githubusercontent.com/tiangolo/fastapi/c5817912d2be25bb310bf9da517882f57bbe7bb5/LICENSE | 4ec89ffc81485b97fec584b2d4a961032eeffe834453894fd9c1274906cc744e | MIT |
| HTTPie | buggy, bug 1 | 001bda19450ad85c91345eea3cfa3991e1d492ba | LICENSE; AUTHORS.rst | https://raw.githubusercontent.com/httpie/httpie/001bda19450ad85c91345eea3cfa3991e1d492ba/LICENSE; https://raw.githubusercontent.com/httpie/httpie/001bda19450ad85c91345eea3cfa3991e1d492ba/AUTHORS.rst | bf6c2c46fdaa00d4883540936f29acf26a43d937ba66151c75a1041b34f771be; 70609266f6ba97ff996d013a5043f2101ae4f6f3d80af0cec616d242b1af5819 | BSD-3-Clause |
| HTTPie | fixed, bug 1 | 5300b0b490b8db48fac30b5e32164be93dc574b7 | LICENSE; AUTHORS.rst | https://raw.githubusercontent.com/httpie/httpie/5300b0b490b8db48fac30b5e32164be93dc574b7/LICENSE; https://raw.githubusercontent.com/httpie/httpie/5300b0b490b8db48fac30b5e32164be93dc574b7/AUTHORS.rst | bf6c2c46fdaa00d4883540936f29acf26a43d937ba66151c75a1041b34f771be; 70609266f6ba97ff996d013a5043f2101ae4f6f3d80af0cec616d242b1af5819 | BSD-3-Clause |
| HTTPie | buggy, bug 2 | 356e0436510fee70b4071fac58be81c0a0a7db59 | LICENSE; AUTHORS.rst | https://raw.githubusercontent.com/httpie/httpie/356e0436510fee70b4071fac58be81c0a0a7db59/LICENSE; https://raw.githubusercontent.com/httpie/httpie/356e0436510fee70b4071fac58be81c0a0a7db59/AUTHORS.rst | bf6c2c46fdaa00d4883540936f29acf26a43d937ba66151c75a1041b34f771be; 9995bdb4617503d6df210baace2291f3bb53b4f769bbbc36df0f34eb91ce7de7 | BSD-3-Clause |
| HTTPie | fixed, bug 2 | e18b609ef7d867d6efa0efe42c832be5e0d09338 | LICENSE; AUTHORS.rst | https://raw.githubusercontent.com/httpie/httpie/e18b609ef7d867d6efa0efe42c832be5e0d09338/LICENSE; https://raw.githubusercontent.com/httpie/httpie/e18b609ef7d867d6efa0efe42c832be5e0d09338/AUTHORS.rst | bf6c2c46fdaa00d4883540936f29acf26a43d937ba66151c75a1041b34f771be; 9995bdb4617503d6df210baace2291f3bb53b4f769bbbc36df0f34eb91ce7de7 | BSD-3-Clause |
| tqdm | buggy, bug 2 | bef86db56654d271838b145ad77f7040a73a7b4d | LICENCE | https://raw.githubusercontent.com/tqdm/tqdm/bef86db56654d271838b145ad77f7040a73a7b4d/LICENCE | 1bbf12d09d437844527b3cdaba01d379dac651b5cbb5ebb0d764274684d2680b | MIT and MPL-2.0, file-scoped |
| tqdm | fixed, bug 2 | 127af5caf19e7d29c346f5ca8a9c7ef3004b664b | LICENCE | https://raw.githubusercontent.com/tqdm/tqdm/127af5caf19e7d29c346f5ca8a9c7ef3004b664b/LICENCE | 1bbf12d09d437844527b3cdaba01d379dac651b5cbb5ebb0d764274684d2680b | MIT and MPL-2.0, file-scoped |
| tqdm | buggy, bug 3 | c2599e3cd6087429f48bae34347ec5d2473c8392 | LICENCE | https://raw.githubusercontent.com/tqdm/tqdm/c2599e3cd6087429f48bae34347ec5d2473c8392/LICENCE | 1bbf12d09d437844527b3cdaba01d379dac651b5cbb5ebb0d764274684d2680b | MIT and MPL-2.0, file-scoped |
| tqdm | fixed, bug 3 | 73962a47026dd980ac0758820efc9c41cbf938e0 | LICENCE | https://raw.githubusercontent.com/tqdm/tqdm/73962a47026dd980ac0758820efc9c41cbf938e0/LICENCE | 1bbf12d09d437844527b3cdaba01d379dac651b5cbb5ebb0d764274684d2680b | MIT and MPL-2.0, file-scoped |
| thefuck | buggy, bug 1 | 2ced7a7f33ae0bec3ffc7a43ce95330bdf6cfcb9 | LICENSE.md | https://raw.githubusercontent.com/nvbn/thefuck/2ced7a7f33ae0bec3ffc7a43ce95330bdf6cfcb9/LICENSE.md | 7d3488ddac804320f8f5ee437bc5530dd2e698fb0d481b11e8da504f19229707 | MIT |
| thefuck | fixed, bug 1 | 444908ce1c17767ef4aaf9e0b4950497914f7f63 | LICENSE.md | https://raw.githubusercontent.com/nvbn/thefuck/444908ce1c17767ef4aaf9e0b4950497914f7f63/LICENSE.md | 7d3488ddac804320f8f5ee437bc5530dd2e698fb0d481b11e8da504f19229707 | MIT |
| thefuck | buggy, bug 2 | 40ab4eb62db57627bff10cf029d29c94704086a2 | LICENSE.md | https://raw.githubusercontent.com/nvbn/thefuck/40ab4eb62db57627bff10cf029d29c94704086a2/LICENSE.md | 7d3488ddac804320f8f5ee437bc5530dd2e698fb0d481b11e8da504f19229707 | MIT |
| thefuck | fixed, bug 2 | 78ef9eec88f43d5727986be2237f6e0e250cbbbc | LICENSE.md | https://raw.githubusercontent.com/nvbn/thefuck/78ef9eec88f43d5727986be2237f6e0e250cbbbc/LICENSE.md | 7d3488ddac804320f8f5ee437bc5530dd2e698fb0d481b11e8da504f19229707 | MIT |

### Exact negative-tree evidence

The refreshed bounded recursive tree request returned HTTP 200 for
https://api.github.com/repos/soarsmu/BugsInPy/git/trees/11c5f1eea954a42132cfd06bf257766a7963e0fd?recursive=1.
The resolved tree SHA was
11c5f1eea954a42132cfd06bf257766a7963e0fd and recursive_result_truncated was
false. The searched conventional filename pattern was
(?i)(^|/)(license|licence|copying|notice|copyright|authors?|contributors?)(\.[^/]*)?$.
The bounded matching-path inventory was empty. The raw response was 693919
bytes with SHA-256
1593a89c0374c9688d556b4db1bb037c38935f165431d09d3bed667f3ee7d6e5. The
sanitized evidence JSON has SHA-256
9f5922ee991180ef2a6150b84283795b2c93338cfd46f38f898015a409fadb82 and was
retrieved at 2026-07-31T19:07:24.9670304Z UTC with credential prompts
disabled. No complete repository was acquired. The exact README remains at
https://raw.githubusercontent.com/soarsmu/BugsInPy/11c5f1eea954a42132cfd06bf257766a7963e0fd/README.md
with SHA-256
3e356958a2869c1a84511e8b7a4fd84bdbef9069f2d34cb18d4985fcb07efa83.

### Interpretation

FastAPI and thefuck have exact-revision MIT text. HTTPie has exact-revision
BSD-3-Clause text plus AUTHORS.rst; the author and listed contributors must
remain attributed and the no-endorsement condition matters. tqdm's LICENCE
states that the work is collaborative, releases files under MIT unless
otherwise stated, and identifies a file-wide MPLv2.0 scope with named MIT
exceptions. Therefore tqdm/std.py and tqdm/utils.py in bug 2 require an
MPL-2.0-aware review, while tqdm/_tqdm.py in bug 3 is an explicitly named MIT
file. All reviewed buggy/fixed license and notice terms are the same within
each project.

The root tree metadata for the four projects showed no separate root NOTICE or
bundled third-party notice path. The reviewed contributor files describe
contribution mechanics but no CLA or assignment term was identified. This
does not clear dependency licenses, file-level headers, or licenses embedded
in a populated source checkout; those remain a later, exact-material review.

## Verdict tables

### Dataset

| Dependency | Verdict | Reason |
| --- | --- | --- |
| BugsInPy metadata revision 11c5f1eea954a42132cfd06bf257766a7963e0fd | BLOCKED | No conventional license/notice file and no explicit permission for metadata, patches, scripts, tests, or repository structure. |

The formal BugsInPy license status is UNKNOWN. Redistribution is BLOCKED
because no governing dataset license or explicit redistribution permission
was identified. The exact README expressly instructs users to clone,
configure, checkout, compile, and test the benchmark for reproducible
research; that is intended-use evidence, not a blanket open-source or
redistribution license. Private local research use therefore remains UNKNOWN.
The operational execution gate is BLOCKED because the project policy fails
closed while that ambiguity remains, Onur has not approved proceeding under
it, and containment and dependency gates are independently incomplete. This
is an operational decision, not a legal conclusion that local acquisition or
execution is prohibited.

### Projects

| Project | Verdict | Conditions |
| --- | --- | --- |
| FastAPI | CLEAR_WITH_CONDITIONS | Preserve the MIT notice; retain exact revision/hash evidence; review file headers and dependencies before redistribution. |
| HTTPie | CLEAR_WITH_CONDITIONS | Preserve BSD-3-Clause conditions and AUTHORS.rst; retain exact revision/hash evidence; review file headers and dependencies before redistribution. |
| tqdm | CLEAR_WITH_CONDITIONS | Apply file-scoped MIT/MPL-2.0 handling; bug 2 is MPL-2.0-sensitive and bug 3 changes an explicitly MIT-named file; review dependencies before redistribution. |
| thefuck | CLEAR_WITH_CONDITIONS | Preserve the MIT notice; retain exact revision/hash evidence; review file headers and dependencies before redistribution. |

### Tasks

Every selected task is BLOCKED. The project source conditions above are
necessary but not sufficient: each task also depends on BugsInPy's metadata,
isolated patch, script, test, and repository structure, for which
redistribution permission is unresolved.

| Task | Project terms | Dataset dependency | Task verdict |
| --- | --- | --- | --- |
| bugsinpy-fastapi-001 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |
| bugsinpy-fastapi-009 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |
| bugsinpy-httpie-001 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |
| bugsinpy-httpie-002 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |
| bugsinpy-tqdm-002 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |
| bugsinpy-tqdm-003 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |
| bugsinpy-thefuck-001 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |
| bugsinpy-thefuck-002 | CLEAR_WITH_CONDITIONS | BLOCKED | BLOCKED |

### Overall pilot

BLOCKED. The overall verdict is no broader than the weakest material
dependency: the BugsInPy dataset-level permission evidence.

## Local execution versus redistribution

These are separate decisions:

| Activity | Boundary under this gate |
| --- | --- |
| Private local research use | UNKNOWN. The README documents intended reproducible research use, but that does not resolve the formal license status or grant redistribution permission. |
| Operational BugsInPy acquisition and execution | BLOCKED by current project policy while private local-use status is UNKNOWN and containment/dependency prerequisites are incomplete. This is fail-closed authorization, not a legal prohibition. |
| Redistribution of upstream source | BLOCKED unless the relevant project notice/license is carried and file-level/dependency terms are reviewed. The project-level records are conditional, not blanket clearance. |
| Redistribution of BugsInPy metadata and patch files | BLOCKED; no explicit BugsInPy license or permission was found. |
| Committing third-party material here | Prohibited: no source tree, patch, test content, environment, cache, credential, or large artifact. |
| _ai-review/ contents | Only sanitized evidence: task IDs, revisions, URLs, paths, hashes, verdicts, bounded retrieval metadata, and aggregate results. No upstream text, source, patch, test content, or raw logs. |
| Publishing sanitized logs, hashes, task IDs, and aggregate results | CLEAR_WITH_CONDITIONS when the material contains no source, patch, test text, secrets, credentials, or identifying raw output, and the exact revision/hash mapping is retained. |
| Publishing model-generated candidate diffs | BLOCKED until both BugsInPy metadata/patch terms and the applicable upstream source terms are cleared; diffs can contain copyrighted source and context. |

Local execution is not a redistribution exception. If the ambiguity is
resolved and Onur separately approves proceeding, source and environments must
remain outside this repository, in an owned disposable workspace, with no
credentials, denied network during task execution, bounded resources, and
recorded cleanup. No benchmark execution is authorized by this report.

## Evidence-package rules

The review package must:

- identify the accepted baseline and the same final uncommitted candidate in
  every diff, status, hash, and copied-file record;
- store exact URL, revision, path, SHA-256, source role, SPDX evidence, and
  attribution scope for every reviewed license or notice file;
- record the BugsInPy missing-license finding and the retrieval failure/blocked
  source evidence rather than converting it into clearance;
- keep raw downloads outside the tracked repository and exclude complete
  repositories, patches, source trees, dependency environments, caches, and
  credentials;
- scan the candidate and evidence package for secrets, upstream source,
  patch markers, environment artifacts, and license-text inclusion;
- permit only bounded, sanitized evidence in _ai-review/.

## Attribution and notice obligations

If a future, separately authorized workflow redistributes project material,
FastAPI and thefuck require their MIT copyright and permission notices; HTTPie
requires its BSD-3-Clause conditions, disclaimer, and material AUTHORS.rst
attribution; and tqdm requires file-scope handling for its MIT/MPL-2.0 terms,
including MPL-2.0 obligations for the selected bug 2 paths. Do not use HTTPie
authors to endorse a derived product.

No BugsInPy attribution or notice obligation can be stated as permission
because no governing BugsInPy license or notice was found. If the maintainers
provide terms, those terms control and must be recorded before the verdict is
revisited.

## Unresolved questions

1. What license governs the exact BugsInPy revision's metadata, patches,
   scripts, tests, and repository structure?
2. Does the BugsInPy project or paper link to a separate authoritative
   permission notice for redistribution?
3. What file-level headers and dependency licenses are present in the exact
   project revisions once a future authorized source review is performed?
4. Has Onur approved proceeding under the unresolved private local-use status?
5. For tqdm bug 2, which exact changed-file portions are covered by the
   file-wide MPL-2.0 statement, and what distribution form will the future
   pilot use?
6. Are any generated logs or candidate diffs materially reproducing source or
   test content? Until checked, keep them outside the repository.

Each unresolved question remains UNKNOWN or BLOCKED; none is inferred clear
from repository publicity, package metadata, badges, or a moving default
branch.

## Exact implications for the next containment task

The next containment task must implement a metadata-only, fail-closed
preflight before any acquisition or execution:

1. Require this manifest's exact BugsInPy revision and BLOCKED licensing state
   to be visible in the preflight record; do not add an execution bypass.
2. Refuse source acquisition, dependency preparation, patch loading, and test
   execution while the dataset verdict is BLOCKED.
3. Keep any future source cache, dependency environment, disposable run,
   patch, and raw output outside the tracked repository and outside
   _ai-review/.
4. Require a separately approved BugsInPy permission record, exact source and
   notice hashes, attribution plan, and dependency provenance before changing
   the licensing gate.
5. When the licensing gate is eventually resolved, require an unprivileged
   process/filesystem boundary, denied credentials and network, resource
   limits, immutable source verification, evaluator-only gold patch access,
   no source/test writes by the agent, and cleanup proof.
6. Allow evidence publication only through the sanitized fields listed above;
   do not publish raw source, patch, test content, or model candidate diffs by
   default.

This task did not execute BugsInPy, prepare dependencies, run models, run
OpenCode, or test the containment boundary.

## Final gate decision

Dataset-level BugsInPy: BLOCKED.

FastAPI, HTTPie, tqdm, and thefuck project-level source terms:
CLEAR_WITH_CONDITIONS.

All eight selected tasks: BLOCKED.

Overall BugsInPy pilot: BLOCKED.

The gate can be revisited only with authoritative BugsInPy licensing or
written redistribution permission, resolved private-use ambiguity, Onur's
approval to proceed, exact evidence for that decision, and a new review that
preserves the source/patch/evidence boundaries.
