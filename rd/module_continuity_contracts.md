# Business-module data preflight

The browser module manager keeps active and next-start configurations separate.
Code update, replacement, installation and historical restoration receive a
bounded, read-only data preflight before reservation. This feature does not
migrate business data or provide atomic startup, backup or rollback.

An optional package manifest field declares recognized preserved formats:

```json
"data_compatibility": {
  "version": 1,
  "preserves": ["work-calendar-v1"],
  "pending_policy": "block"
}
```

Recognized format IDs are work-calendar-v1, workspace-operations-v1 and
module-data-v1. Unknown fields, versions, formats and policies are refused.
Existing builtin data requires its target package's corresponding preservation
declaration. Such a declaration describes trusted package compatibility; it is
not a sandbox or proof that arbitrary package code preserves data.

The core preflight locates the registry, shared operations, calendar and Google
stores, WF_HOME material, external project/workspace identities, context/session
metadata and module namespaces. Shared operations can contain Jira, Git and memo
facts and remain core-owned. Builtin services use their existing storage paths;
the generic extension data_dir is not their complete business-data directory.
Credentials and OAuth client configuration are references only: their bodies are
neither read nor hashed.

Original SQLite databases are never opened by SQLite. Bounded observed database,
WAL and SHM bytes are interpreted in an owned temporary clone, including committed
uncheckpointed WAL rows. Source bytes and directory/reference identities are
checked again. Unknown schema, unsupported state, read/path/link error, changing
source or exceeded bounds blocks reservation instead of becoming an empty count.

Unfinished operation states planned/approved/executing/uncertain and Google
queued/syncing/uncertain/error/conflict block code transitions. Queued Google
operations can send automatically on synchronization. Terminal states, legacy
approval exports, successful results requiring local recording and interrupted
workflow batches use their actual storage contracts. Dedicated session_receipt
metadata is distinguished from executable pending operations. The exception does
not apply to shared operations or arbitrary kinds/states. Nonempty opaque module
data remains blocked pending an explicit migration contract.

The plan API returns continuity.scope=preflight_only, ready, affected_modules,
stores, blockers, observation fingerprint and limits, together with can_apply.
Blocked previews are inspectable but cannot be reserved. The host rechecks the
report before package staging and before configuration history/state writes.
Missing legacy preflight, newly blocked state or changed observation requires a
new preview and returns HTTP409. Already reserved plan IDs remain idempotent.
Disabling/removing a module retains business data and permits separate recovery.

Current bounds are file4MiB, total observed/rechecked bytes64MiB, registered
projects64, roots64, source files2048, descriptors1024, external namespace
files256 and SQLite rows100000 per inspected table. Limits are reported and
exceeding them blocks the preflight. Cooperative path/content checks do not
serialize hostile or non-cooperating writers. Writer admission, worker shutdown,
consistent snapshots, migration, generation switching and crash recovery remain
separate requirements. The browser states this limit and disables reservation
for blocked or missing preflight results.

Same-ID schema changes and cross-implementation data adoption continue to be
refused. A different-ID configuration replacement retains the old namespace;
it does not transfer old business records to the new implementation or resend
unconfirmed operations.

CI preserves the previous named browser cases and adds actual-server preflight
cases. Declared source hashes, runtime profiles, lifecycle/preservation evidence,
fresh mutation baselines and original artifact ZIP bytes remain required. The
normal evidence upload explicitly includes generated hidden witnesses so the
declared byte index and exported members match.
