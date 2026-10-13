# MinIO Diagnostics

An offline MinIO AIStor diagnostic Markdown analyzer. This is an R1
implementation candidate, not a complete diagnostic platform or an accepted
production release.

## Current Scope

- Import the official summary and individually supplied check results.
- Preserve vendor statuses separately from local explanatory priorities.
- Produce self-contained HTML, Markdown, JSON and a SHA-256 manifest.
- Store each run in a new generation with owner-only POSIX permissions.
- Preserve previous generations when updating `CURRENT.json`.
- Apply best-effort redaction; manually review results before sharing them.

The analyzer does not connect to MinIO, execute commands contained in reports,
or reconstruct passed checks from aggregate counts. Health gzip input, custom
health rules, baseline comparison and metrics/log analysis are not implemented.

## Environment

Python standard library only; no package installation is required.
Tested with Python 3.11 and 3.14 on Linux/WSL. Report storage requires a POSIX
filesystem that enforces owner-only access; use a private Linux home directory,
not a Windows-mounted output directory. Windows-native storage is unsupported.

```bash
python3 analyze.py --check-env
python3 analyze.py --official-report /path/to/approved-report.md --mask strict
```

The default output root is `~/minio-airgap-diag-output`, or
`AISTOR_DIAG_OUTPUT_DIR` when set. `--out-dir` overrides this location.

Outputs are stored in `run-<uuid>/`. To select a new generation after an
existing result, add `--force`: this preserves previous generations and only
replaces `CURRENT.json` after validating the new files. Exit code `0` means
processing succeeded, not that the diagnosed system is healthy.

Open the generated `aistor-diag-report.html` in a browser. Assets are embedded;
no web server is required. A WSL report can be opened from Windows using
`\\wsl.localhost\<distribution>\home\<user>\minio-airgap-diag-output\<generation>\aistor-diag-report.html`.

## Tests

```bash
python3 run_tests.py
AISTOR_DIAG_TEST_REPORT=/path/to/approved-report.md python3 run_tests.py
```

Without an approved real fixture, the two real-sample tests are skipped, not
counted as validation passes. A fixture can also be provided locally at
`fixtures/official-report.md`. Synthetic test artifacts are retained under the
private output root. Synthetic writer units mock only the permission predicate;
they do not establish filesystem security. Production permission guards remain
enabled.

## Repository Boundary

Only analyzer code, tests and this public usage guide are tracked. Diagnostic
inputs, generated reports, local operational contracts, Router integration
utilities, credentials, logs and backups stay local. The root `.gitignore`
uses an explicit allowlist; review both staged content and any allowlist changes
before committing or publishing.

Never commit real diagnostic material or tokens, even after automatic masking.
The offline analyzer does not need GitHub credentials to run.
