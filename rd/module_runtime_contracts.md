# Host activity and local worker stop contract

One `ModuleHost` owns a cooperative synchronous activity gate. `ModuleServices.activity()` and the bound stock backend, Google service and calendar engine enter that gate. Root activity is counted once; same-thread nested work completes under its original admission. Standalone services retain the previous no-gate behavior.

`ModuleHost.quiesce(drain_timeout=5, stop_timeout=5)` is an internal Python API. Both timeouts are finite exact int/float seconds in [0,30]. Calling it from an admitted activity is refused without changing admission. Concurrent lifecycle controls return busy. The existing browser GUI is retained; this increment adds no HTTP execution endpoint or GUI stop button.

The host checks current stock package digest, activity API 1, startup and runtime ownership before pausing. Pausing rejects new roots and waits for admitted work. A drain timeout starts no stop and leaves admission closed; `resume_activity()` may explicitly cancel that pause. After drain, participant/ownership checks run again because admitted work may change ownership. Failed revalidation starts no stop.

After successful drain and revalidation, backends stop in reverse dependency order using one shared worker-join budget. Only an explicit `True` confirms strict stop. False, None and exceptions leave completion unverified; retry observes actual completion. Once any stop starts, this host cannot resume admission or start workers again. Use a new host/process generation. Caller-owned runtimes, older/different packages and extensions without this supported stock protocol are rejected before stopping.

Runtime status is schema version 1 with `scope=host_mediated_runtime`, admission/count, phase, stop observations and `stop_verified`. **`snapshot_ready` is always false**, including verified stop. Phases are running, draining, stopping, stop_failed, stopped and closed. Ordinary legacy close drains first but retains its existing cleanup compatibility and never supplies strict stop proof.

HTTP business operations, including GET paths that may create/cache files, and configuration/module plan/apply mutations are admitted. A paused root receives JSON HTTP 503 with `runtime_reason`. Module status, core configuration reads, static UI and module assets remain available. Google refresh and the complete OAuth callback enter activity before effects. Parsing an incomplete OAuth HTTP header precedes callback activity, so a drained gate does not by itself prove listener termination; actual worker stop must be observed.

This contract covers one host's mediated synchronous work and its cooperating owned stock workers. It does not fence raw child-store/private calls, other hosts/processes or uncooperative extensions, cancel external requests, create a consistent SQLite/file snapshot, write a transition journal, migrate data or recover activation after a crash. Those require later continuity stages.

Acceptance tests live in `tests/test_runtime_activity.py`, `tests/test_package_activity.py` and `tests/test_runtime_quiescence.py`. CI counts/selectors are pinned in the current profile and runtime activity mutation specification. Synthetic local HTTP/provider fixtures are separate from real-account acceptance.
