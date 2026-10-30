# Workspace deployment content and writer contract

Template/skill deployment keeps its existing preview, overwrite, protection and partial-failure semantics. Before each existing deep file comparison, clear filecmp's process cache so a same-size/same-mtime edit is not decided from an earlier comparison in a sequential operation. This is not an atomic file snapshot or a guarantee against concurrent non-cooperating editors.

The public raw deploy.apply call enters the existing writer_function boundary before template callbacks or owner effects and retains it through all projects, final copied_total calculation and return. Same-domain nesting shares the current root. Empty/error/no-op calls are admitted; raw plan remains read-only with respect to writer admission. Existing callback exceptions propagate, while copy errors retain existing per-project failed rows. Epoch completion does not mean business success or an authorized snapshot.

Stock browser HTTP deployment retains its existing host/backend outer activity. Raw return, host return and JSON/socket response phases are observed separately. The raw decorator alone does not promise protection until a remote client receives the response.

Verification uses real file bytes, same-signature edits, duplicate targets, actual copy and final response windows, failure/no-op/nesting, and stock HTTP requests with owned fixtures. Negative controls must fail only after their unmodified command passes. Live business providers and ERP are outside these tests.
