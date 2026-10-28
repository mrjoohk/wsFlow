# V0/V1 frozen verification

This branch contains a source-only acceptance candidate. User profiles, skills, credentials, business data and historical evidence are excluded. Historical source and evidence are retained separately; changed application, package and CI code requires new source-qualified evidence.

Use an isolated Windows Python **3.10.11** compatibility environment or **3.14.3**, Node **24.14.0**, and the dependencies locked here. Install Node packages outside the source tree and set `PLAYWRIGHT_MODULE` and `PLAYWRIGHT_BROWSERS_PATH`. Python tzdata is installed with `pip install --require-hashes -r ci/python-requirements.txt`. Python 3.10 is a historical compatibility target, not a production recommendation.

Calendar/Jira features using IANA zones need system timezone data or the tzdata distribution at runtime. Windows acceptance explicitly installs that data. Python source imports only the standard library; this does not eliminate the timezone-data requirement. Node/Playwright/browser are test-only dependencies.

Run `python tools/ci/runner.py check` against the newly sealed candidate, then `python tools/ci/runner.py run --out <new-evidence-directory> --expected-python <exact-version> --jobs 4`. The complete run includes CI tool acceptance, vendor hashes, all application tests, the browser profile, the strict mutations in `ci/selection.json`, and three controlled gate fixtures. Partial `--step` runs are labelled and cannot alone satisfy V1. The root `ci/source-manifest.json` remains a historical manifest; seal and check a fresh candidate before execution.

Builtin business implementation and UI are owned by independent packages. The module stage explicitly selects package/update/UI/generation tests. The full all/browser profile also executes the actual module browser fixture and immediately seals its snapshot in the regression artifact. Exact case identity, eight server generations, observed provider/worker/preservation fields, all package input hashes and the fixture Python executable/version are required. Current profile counts are pinned in ci/profile-baseline.json. Functional details are in rd/module_ci_contracts.md.

The mutation runner's local CI profile requires fresh output. Existing results and resume/budget/summary are rejected because cached proof cannot establish the current inherited runtime. All-skipped unittest baselines are rejected. The original shared skill remains unchanged; patch provenance is recorded separately.

The verification branch push produces two expected **successful** regression jobs and three expected **failed** gate jobs. The workflow conclusion is therefore expected to be failure. Accept each controlled failure only when the job artifact contains `gate.json` with the matching mode/source and `rejected: true`; a setup crash or missing artifact is not a successful negative control. Pull request runs omit these intentionally failed jobs.

These are synthetic Jira/Google tests and real local Git fixtures. Actual external accounts and tool recognition require V2 onward. Git preserves the explicit legacy command paths and ff-only safety; common immutable plan/operation UUID/reconciliation for Git remains F-032.

Windows acceptance uses canonical full-path NTFS temporary directories. The runner expands TEMP/TMP/TMPDIR for child processes and records the result. The original application tests remain unchanged. Direct short-alias input to the read model has a known fail-closed limitation; passing this profile does not qualify all Windows path aliases.
