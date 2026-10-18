# V0/V1 frozen verification

This branch contains a source-only acceptance candidate. User profiles, skills, credentials, business data and historical evidence are excluded. Existing application/test/vendor bytes are preserved; CI tools have their own acceptance tests.

Use an isolated Windows Python **3.10.11** compatibility environment or **3.14.3**, Node **24.14.0**, and the dependencies locked here. Install Node packages outside the source tree and set `PLAYWRIGHT_MODULE` and `PLAYWRIGHT_BROWSERS_PATH`. Python tzdata is installed with `pip install --require-hashes -r ci/python-requirements.txt`. Python 3.10 is a historical compatibility target, not a production recommendation.

Run `python tools/ci/runner.py check`, then `python tools/ci/runner.py run --out <new-evidence-directory> --expected-python <exact-version> --jobs 4`. The complete run includes CI tool acceptance, vendor hashes, 415 application tests, 82 named browser cases, 208 selected strict mutations, and three controlled gate fixtures. Partial `--step` runs are labelled and cannot alone satisfy V1.

The mutation runner's local CI profile requires fresh output. Existing results and resume/budget/summary are rejected because cached proof cannot establish the current inherited runtime. All-skipped unittest baselines are rejected. The original shared skill remains unchanged; patch provenance is recorded separately.

The verification branch push produces two expected **successful** regression jobs and three expected **failed** gate jobs. The workflow conclusion is therefore expected to be failure. Accept each controlled failure only when the job artifact contains `gate.json` with the matching mode/source and `rejected: true`; a setup crash or missing artifact is not a successful negative control. Pull request runs omit these intentionally failed jobs.

These are synthetic Jira/Google tests and real local Git fixtures. Actual external accounts and tool recognition require V2 onward. Git preserves the explicit legacy command paths and ff-only safety; common immutable plan/operation UUID/reconciliation for Git remains F-032.
