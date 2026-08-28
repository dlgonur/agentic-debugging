# Security policy

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue. Contact the
repository owner privately through the security contact shown on the GitHub
profile that owns this repository. Include affected versions, reproduction
steps, impact, and any suggested mitigation.

Do not include live credentials, private datasets, or sensitive machine output
in the report. Redact tokens and personal paths.

## Supported version

Security fixes target the current default branch. Historical research
artifacts and frozen evidence are retained for reproducibility and are not
maintained as deployable services.

## Scope

The project runs model-provided commands only through explicit operator
configuration and documented containment boundaries. Reports about path
escape, command isolation, credential exposure, unsafe patch application,
verifier bypass, or cleanup failure are in scope.
