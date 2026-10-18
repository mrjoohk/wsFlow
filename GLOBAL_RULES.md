# GLOBAL_RULES.md
## Execution Instructions for the Agent (project-agnostic)

You are tasked with executing the project defined in `PROJECT_RULES.md` at the repository root (see Rule 10). If `PROJECT_RULES.md` does not exist, ask the user for the project identity and create it before substantive work.

**Scope invariant**: This file is project-agnostic and is reused across projects. It must NEVER contain project-specific facts — no project names, domain values, ports, message IDs, or paths of a specific project's documents. If such a fact is found here, move it to `PROJECT_RULES.md` and record the move in the file log. (Rationale: a prior project's header survived here into a later project and was flagged by audit as a defect — global text rots silently when it carries project facts.)

You MUST follow the exact execution order below.

---

# PART 1 — CODING BEHAVIOR PRINCIPLES

> These principles apply to **all implementation tasks** regardless of skill or domain.
> Tradeoff: These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

# PART 2 — WORKFLOW RULES

### Rule 1 — Review Before Creating Output
> Before creating any final deliverable (document, file), first share the analysis/investigation summary with the user and wait for approval.

**Flow**: Start task → Perform analysis → Present findings to user → Await approval → Create output

### Rule 2 — Preserve Analysis as MD Files
> Save all analysis/investigation content created during tasks as MD files for reuse in future tasks.
> **Exception**: Review documents that contain diagrams (block diagrams, flowcharts, system structure, etc.) must be saved as `.html` instead, so the diagrams render properly (see Rule 5 and Rule 6).

**Filename convention**: `YYMMDD_HHMM_[description].md` (or `.html` for diagram-bearing review docs)
Example: `260318_1430_uf_status_analysis.md`, `260318_1430_uf_architecture.html`

### Rule 3 — State Judgment Rationale
> Every decision, choice, or recommendation must include the rationale (판단 근거).

**Apply**: Include a "판단 근거:" section in all analysis MD files and user reports.

### Rule 4 — File Creation Log
> All files created in the working folder must be logged in `logs/files.jsonl`.
> **If `logs/files.jsonl` does not exist, create it first, then record the entry.**

> **[Format Rule]** One JSON object per line (JSONL), UTF-8, **append-only**.
> Never rewrite, reorder, or delete existing lines — corrections are appended as new records.
> After appending, run `python tools/worklog/validate_worklog.py logs/files.jsonl logs/prompts.jsonl`.
> A non-zero exit is a rule violation and must be resolved before the request is closed.

| Key | 컬럼 | Content |
|-----|------|---------|
| `ts` | 일시 | `YYYY-MM-DD HH:MM` |
| `files` | 파일명 | **Array**, one filename per element. One record per work unit. **Wildcards (`src/*`) are forbidden** — records must stay queryable. A whole directory is written with a trailing `/` (`logs/P-007_tick_evidence/`), never `/*`. |
| `summary` | 요청 요약 | Core content of user's request |
| `req_id` | 요청 ID | `P-xxx` from `logs/prompts.jsonl` (Rule 8) |
| `basis_ids` | 근거 ID | **Array.** Ledger IDs this change serves — finding `F-xxx`, decision `D-xxx`, mapping `M-xxx` (Rule 11). Empty array only when no ledger item applies. |
| `verify` | 검증 | How the change was verified: test name(s), evidence file path, or the **explicit literal `"검증 없음"`**. An empty string is a rule violation — `"검증 없음"` is a conscious declaration and is queryable; empty is a hole. |
| `commit` | 커밋 ID | VCS commit hash once committed; `"미커밋"` until then |
| `schema` | — | `"v2"` for new records. `"v1"` marks records migrated from the pre-2026-08-26 workbook, where the four keys above did not yet exist. |

> **[Exception Fields]** Never invent data to satisfy the schema. When a fact cannot be recovered, state that fact instead.
> `legacy_note` — why a wildcard or a merged row could not be resolved. This is the **only** way a wildcard passes validation.
> `orphan_reason` — why a `req_id` has no matching record in `logs/prompts.jsonl`.
> Both are deliberate, queryable declarations — the same principle as `"검증 없음"` above.

```json
{"ts":"2026-08-25 17:30","files":["review_docs/260825_1730_claw_mode_scenario_test_plan.md","graph/edges.csv"],"summary":"CLAW 모드별 시나리오 시험 계획 수립","req_id":"P-050","basis_ids":["F-076","M-01"],"verify":"인용 3건 원문 재확인 OK. graph_checks 위반 0건","commit":"미커밋","schema":"v2"}
```

### Rule 5 — Output Format Standard
> Deliverables containing tables or figures must be created as `.docx`.
> **Review documents containing diagrams must be created as `.html`** (see Rule 6) so that block diagrams, flowcharts, and system structures render correctly. Use a self-contained single HTML file (embed CSS/JS; Mermaid or inline SVG recommended for diagrams).

| Condition | Format |
|-----------|--------|
| Review/analysis doc containing a diagram | `.html` |
| Contains table or figure (non-diagram deliverable) | `.docx` |
| Text-focused analysis/memo | `.md` |
| Data/numeric-focused | `.xlsx` |
| Slide presentation | `.pptx` |

> These are defaults. `PROJECT_RULES.md` may override format choices for its own artifacts (e.g., a code-centric project may keep table-bearing analyses in `.md`); when it does, the project binding wins and the override must be written there, not assumed.

### Rule 6 — Directory for Review Documents
> All files (html, docx, ppt, xlsx, md) created during analysis/investigation phases must be saved in the `review_docs` directory.
> **If `review_docs` does not exist, create it first.**
> **Diagram representation**: When a review document includes a diagram (block diagram, flowchart, system configuration, R&D concept, etc.), author it as `.html` rather than `.md` so the diagram renders properly. Keep it as a single self-contained HTML file. Use `.md` only for text-focused review docs without diagrams.

**Apply to**: Any file produced during analysis, review, or investigation steps (before user approval).

### Rule 7 — Directory for Output Documents
> All files (docx, ppt, xlsx, md) created as final responses, results, or action outputs must be saved in the `output_docs` directory.
> **If `output_docs` does not exist, create it first.**

**Apply to**: Any file produced as a deliverable, response, or remediation result (after user approval).

### Rule 8 — Prompt & Response Log
> Every user request prompt and the agent's corresponding response/result must be recorded in order in `logs/prompts.jsonl`.
> **If `logs/prompts.jsonl` does not exist, create it first.**

> **[Format Rule]** Same as Rule 4 — JSONL, UTF-8, append-only, validated after every append.

| Key | 컬럼 | Content |
|-----|------|---------|
| `ts` | 일시 | `YYYY-MM-DD HH:MM` |
| `req_id` | 요청 ID | `P-xxx`, non-decreasing. **This ID is the provenance root**: analysis docs, file-log records (Rule 4), and ledger entries (Rule 11) reference it, so every artifact answers "which request caused this". One request may span several exchanges, so **consecutive** records may repeat the same ID — but an ID must not reappear after a different one has intervened. |
| `prompt` | 요청 프롬프트 | Full text of the user's request, verbatim, newlines included |
| `response` | 응답/결과/대처 | Summary of the agent's response or action taken |
| `outputs` | 산출물 경로 | **Array.** Paths of documents/files produced for this request |
| `schema` | — | Same as Rule 4 |

### Rule 9 — Directory for Design Artifacts
> All design documents produced by design/engineering skills must be saved in the `rd` directory.
> **If `rd` does not exist, create it first.**

**Applies to** artifacts of this kind, **when and if they are produced** (a project is not required to have all of them; the project's actual canonical documents are declared in `PROJECT_RULES.md`):
- `requirements.md`, `problem_statement.md`, `assumptions_and_constraints.md`, `clarification_log.md`
- `if_list.md`, `if_decomposition.md`, `uf.md`, `uf_split/`
- any other design document produced by design/engineering skills

> **Note**: These files must NOT be placed in `review_docs/` or `output_docs/`. The `rd/` directory is the single canonical location for all design artifacts. Do not cite an artifact from this list as authoritative unless it actually exists — citing planned-but-absent documents as canon is an audit defect.

### Rule 10 — Project Binding (`PROJECT_RULES.md`)
> Every project root must contain `PROJECT_RULES.md` declaring: project identity, canonical documents, domain invariants (machine-checkable where possible), evidence conventions, format overrides (Rule 5), and the ledger epoch (원년). GLOBAL_RULES defines **how** to work; PROJECT_RULES defines **what** the project is.

- Precedence: for project-specific matters, PROJECT_RULES.md wins; for process discipline (logging, IDs, verification), GLOBAL_RULES wins.
- Anti-contamination: project facts never migrate into this file (see header Scope invariant).

### Rule 11 — Ledger and IDs
> Findings (`F-xxx`), decisions (`D-xxx`), and interface mappings (`M-xxx`) are registered in a single machine-readable ledger — `graph/edges.csv` — with a status: `open / fixed / verified / rejected / superseded / deferred`.

- **Supersede duty**: when a decision is reversed, do not silently edit old documents — add a `supersedes` reference in the ledger. Old documents remain as history; the ledger is the authority on what is current. (Rationale: an audit found three planning docs still declaring a superseded value as "confirmed".)
- Audits are redefined as **ledger updates**: an audit starts by re-checking `open` items (tagging RECURRING) and ends by registering new items. A report without ledger updates is not a completed audit.
- Analysis documents cite ledger IDs; ledger rows cite evidence (file:line, commit, test name).

### Rule 12 — Definition of "Fixed"
> A defect fix is `fixed` only when accompanied by a **negative-control test** — one that fails if the fix is reverted. It becomes `verified` after that test is seen passing in CI/ctest. If a test is infeasible, the reason must be recorded as the judgment rationale (Rule 3) in the ledger row.

(Rationale: a fix without a reverting test was the direct cause of silent recurrences; and a passing test alone proves nothing — only fails-when-reverted does.)

### Rule 13 — Evidence Citation
> Measured numbers in documents must cite their evidence file path (e.g., `logs/<run_id>/evidence.json`, a CSV, or a test log). Hand-copying numbers between documents is forbidden — copy the *reference*, not the value.
> The producing side has the mirror duty: **test/verification runs must emit a machine-readable evidence file** (counts, timings, pass/fail) so there is something to cite. A run that leaves only console output leaves nothing citable.
> Claims about code must cite `file:line` and quote verbatim; quotes must remain greppable.

(Rationale: about one third of one audit's WARN findings were the same measurement drifting across hand-copied documents, and one CRITICAL was a citation of code text that did not exist.)

### Rule 14 — Machine Gates
> A generic checker (`tools/graph_checks.py`) runs project-declared integrity queries before work is declared done: dangling declarations, citation existence, count-claims vs code, enum/contract consistency, ledger schema, and log↔commit reconciliation. Project-specific queries live in `graph/checks_config.json` (owned by the project, per Rule 10) — the checker itself stays generic.

- Violations must be either fixed or registered in the ledger as `open` before finishing the task. An unexplained violation is a blocker for "done", not a note.

### Rule 15 — Verification Cadence
> Machine gates (Rule 14) run per task. Two slower verification loops run on top of them:

| Trigger | Verification |
|---|---|
| Large refactor / multi-file change lands | **Delta review the same day**, scoped to the change diff — do not wait for the next full audit. (Rationale: a one-day, eight-document refactor performed without same-day verification injected three CRITICAL defects that surfaced only in the next full audit.) |
| Milestone / periodic checkpoint | **Independent context-free audit** (fresh reviewers who see only the files, not the session). The audit consumes the ledger's `open` items first (tagging RECURRING) and ends by updating the ledger — per Rule 11, a report without ledger updates is not a completed audit. |

The metric of a healthy cadence is not "findings per audit" but **recurrence rate ≈ 0** (nothing found twice) and **injection-to-detection gap ≈ same day** (new defects caught by the delta loop, not the milestone loop).

### Workflow Summary (v2 — verification is a structural node, not a virtue)
```
Receive request
  → [Rule 8] Log prompt, issue P-xxx
  → Consult ledger: related open F-/D- items? (recurrence starts here)
  → Perform analysis/investigation
  → [Rule 3] Summarize with judgment rationale (citations per Rule 13)
  → [Rule 6/9] Save analysis to review_docs/, design artifacts to rd/
  → [Rule 1] Present to user → Await approval
  → Create output
  → ★ VERIFY: [Rule 12] negative-control test → [Rule 14] tests + graph_checks pass
  → ★ LEDGER: [Rule 11] state transitions (open→fixed→verified), register new items
  → [Rule 7] Save outputs to output_docs/
  → [Rule 4] Append to logs/files.jsonl (req_id, basis_ids, verify, commit)
  → [Rule 8] Append to logs/prompts.jsonl
  → [Rule 4/8] python tools/worklog/validate_worklog.py logs/files.jsonl logs/prompts.jsonl
```
★ = the two nodes whose absence caused audit findings to recur across sessions.

Above the per-request loop, Rule 15's two slower loops apply: same-day delta review after large changes, context-free audit at milestones.
