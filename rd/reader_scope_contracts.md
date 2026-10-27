# Reader scope contract

Reader observes explicitly permitted files and directory trees. An exclusion takes precedence over both kinds of permission. The result of `allowed` is one boolean for a resolved path. Directory membership uses lexical pathlib ancestry, including the path itself. Scope lists remain live: append, replacement and clearing take effect at the next observation. Explicit file permission retains exact Path membership.

Logical directory roots remain unresolved when registered. A link must not enlarge its own declared scope. Each read resolves its pathname and checks both the permitted scope and any explicit boundary before opening the file. The implementation builds the resolved path's ancestor set once per permission check; it does not retain a path resolution cache.

The read limit is 4 MiB. Successful bytes carry their SHA-256. File identity, size and modification time are compared before/after reading and against the pathname after close. The resolved pathname is checked again after reading and during `finish`. A previously missing source appearing before `finish` invalidates the observation. These guards detect ordinary concurrent changes; they do not constitute an OS sandbox or an atomic filesystem transaction.

Regression tests cover live permission lists, exclusion precedence, prefix siblings, lexical Windows boundaries, unopened denied paths, file replacement, size limits, missing-file creation, changes during reading, and link retargeting with unchanged file identity. Branch-specific negative controls remove pathname stat, read/finish resolution checks and unresolved-root handling independently. A baseline must pass before a mutant's assertion failure is accepted. Timeouts, import failures and unmatched edits are not successful detections.

The Windows acceptance environment uses an NTFS fixture volume, including directory junctions and hardlinks. Evidence destinations are separate from source and user data. Tests do not automatically relax Git ownership settings or grant permission to additional paths. Performance comparisons are observational; a functional regression result does not establish an HTTP/UI latency SLO.
