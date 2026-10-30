# Workspace public writer roots

The workspace package binds context and session services to their owned operation
store's cooperative writer domain. Context preview/apply (including explicit
recovery), session prepare, evidence upload and receipt observation each keep one
complete synchronous root. Admission precedes resource-lock/file effects and
ends after all file/SQL/audit/response work, including final readback.

Session methods first repeat the existing read-only storage authority check before
admission can create control files, and retain the original check inside the root.
Rejected storage inside a workspace/project stays untouched. Reader construction
and inspection remain available without eager constructor rejection.

Read-only inspect/status/catalog methods retain their existing behavior. Source
or target edits are still protected by the original validation/recovery rules.
The change does not launch an agent, native app, ERP or provider operation.

Same-domain nesting reuses the existing host root. Only participating roots are
covered; other raw adapters, uncooperative I/O and unknown descendants remain
outside this boundary. Admission epochs are not business generations. Snapshot,
migration, data adoption and activation readiness remain false.
