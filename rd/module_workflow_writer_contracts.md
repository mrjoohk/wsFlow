# Workflow initialization and daily batch writer roots

The work-calendar package admits raw `wfinit.write_state`,
`wfinit.gitignore_apply` and `workflow.run` through the existing cooperative
writer domain. Admission precedes owner/resource-lock effects and covers the
complete synchronous function, including state and exclusion reports, known
vendor subprocess completion, terminal receipt or warning, and returned data.

Same-thread host nesting reuses the current domain/root. Standalone functions
resolve the configured userdata domain at call time. No-op writer calls remain
admitted; preview/report/status readers retain their existing behavior.

State merge/progress preservation, gitignore text/newline behavior, batch
watermark and memo protections, failure/receipt recovery, transport and timeout
policies are unchanged. A completed admission epoch is not a successful business
transaction or an atomic multi-file commit.

Only participating roots in one configured domain are fenced. Other raw
adapters, uncooperative I/O, different domains and unknown descendants remain
outside that boundary. The change grants no snapshot, migration, adoption or
activation permit, and does not execute real providers, ERP or user native apps.
