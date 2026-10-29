# Cooperative writer admission (C3-2A)

The core writer domain is the canonical configured userdata directory's
`writer-admission` directory. Participating synchronous roots serialize across
objects, hosts, threads and processes. Same-thread nesting reuses the outer
root; explicit nesting across domains is refused. Different domains and direct
uncooperative file/SQLite/private operations remain outside this boundary.

Host activity retains a domain lease; explicit writes promote it before effects.
Raw registry mutations, operations write transactions, common memo/Git sagas,
memo document functions and calendar preview/apply also participate. Registry
mutations reload current disk data under the lease so stale Store instances do
not overwrite prior registrations. Other raw workspace/provider paths require
future coverage; this contract does not claim all application writes.

Durable metadata records a UUID admission namespace and begin/end sequence,
not a business-data generation. An interrupted root leaves an unknown marker.
Ordinary writes may continue while preserving that marker; barriers refuse it.
No automatic recovery, reset or remote replay is provided. Missing metadata
creates a fresh namespace; malformed existing metadata is refused.
If an end replacement succeeds but its acknowledgment and compensation storage
both fail, the process retains a local refusal latch; durable cross-process
unknown state cannot be guaranteed while storage is failing.

A host writer barrier requires verified stock-worker stop, closed admission
and zero in-flight host roots. Its opaque lease is lexical and thread-bound.
All snapshot, activation and complete-coverage readiness values remain false.
It does not authorize copies, schema migration, data adoption or activation.

Timeout values are finite exact int/float seconds in [0,30]. Cooperative lock
waiting is budgeted; arbitrary filesystem I/O is not a hard bounded operation.
Metadata replacement retries the same encoded bytes at most six times for
Windows PermissionError codes 5/32/33, with .01/.02/.04/.08/.16 second delays.
It never replays business effects; other errors and persistent failures refuse.
Serializing whole roots can delay unrelated participating work in one domain.
Atomic replacement and fsync do not assert power-loss durability.
