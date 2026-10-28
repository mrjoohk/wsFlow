# Business module CI contract

The frozen profile qualifies Windows Python 3.10.11 and 3.14.3 with Node 24.14.0 and the locked Playwright/tzdata dependencies. Exact expected counts and cases are pinned in ci/profile-baseline.json, ci/browser-baseline.json and ci/modules-browser-baseline.json.

The module browser suite records the actual Python version/executable for every generation and compares them to the declared profile. It uses synthetic projects/credentials, actual local HTTP and a real browser. Its twelve case identities, eight server generations, provider attempts from every shutdown/final observation, preserved originals and successful worker shutdowns are required. Both source maps must equal all owned package/domain/vendor/UI inputs plus the fixture/browser/example sources. PNG chunks must have valid CRCs and a complete image structure.

The profile seals module evidence immediately before later mutation descendants run and checks both original/sealed trees again before success. Output-path and Python overrides are isolated. Original provider artifact ZIP digests, safe member paths, exact bytes, workflow commit/run/attempt and both Python profile runtime identities are required when consuming CI results. Expected-failure jobs qualify only if the intended negative fixture is rejected at its designated step.

Registry replacement retries only PermissionError with Windows winerror 5/32/33, six attempts and at most .31 seconds of backoff. Permanent denial propagates and other errors/operating systems are not retried. A synthetic native reader tests actual delete-sharing semantics; no business files are used.
