# Changelog

All notable changes to Arkon are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.4.0] — 2026-05-18

A scope-aware refresh of the Wiki UX. Every wiki page already lived in a
scope (`global`, `department`, `project`), but the UI treated same-slug
pages from different scopes as duplicates and the index page only ever
showed the global catalog. This release threads scope context through
the URL, the page tree, the page detail view, and the ingestion pipeline.

### Added

- **Scope switcher on `/wiki`**: a dropdown listing the scopes the
  current user can access. Selecting a scope updates the URL
  (`/wiki?scope_type=&scope_id=`), refetches the matching `_index`
  catalog and pages grid, and is shareable / reloadable.
- **Scope-grouped page tree**: the sidebar in `/wiki` and the detail
  viewer now groups pages by scope (`GLOBAL`, plus each department),
  then by page type (`Entities`, `Concepts`, `Topics`, `Sources`).
  Clicking a scope header opens that scope's wiki landing; the chevron
  toggles expansion independently. The active scope and the bucket
  containing the active page auto-expand on navigation.
- **Scope-preserving navigation**: backlinks, outlinks, and inline
  `[[wikilinks]]` carry the current page's scope params, so jumping
  between related pages keeps the user inside the same scope context.
  The detail back button returns to `/wiki?scope_type=&scope_id=` for
  department-scoped pages (projects continue to return to
  `/workspaces`).
- **`GET /api/wiki/my-scopes`**: lists global plus each department and
  project the requester can read. Used by the scope switcher.
- **`scope_name` in wiki responses**: `/api/wiki/pages` joins the
  `Project` and `Department` tables so each summary carries a
  human-readable scope name (e.g. `"Phòng Nhân sự"`) alongside the raw
  ID, removing the need for separate lookups on the client.
- **Thematic-section concept pages during ingestion**: the MRP
  extraction and planning prompts now recognise documents that describe
  a primary entity through several distinct themes (e.g. *Product
  Positioning*, *Target Customer Profile*, *Content Pillars*) and emit
  a separate `concept` page per theme instead of dumping the content
  into the entity page. The entity page links out to them with
  `[[concept/...]]`.

### Changed

- `/api/wiki/pages` and `/api/wiki/index` accept optional
  `scope_type` + `scope_id` query parameters. When omitted the original
  behaviour is preserved (RBAC-filtered list, global index).
- `ScopeBadge` accepts `scopeId: string | null` to match the relaxed
  `WikiPageSummary` type.
- Sidebar collapse/expand chevrons swapped for `left_panel_close` /
  `left_panel_open` icons that don't look like a Back button.
- The `/wiki` page tree no longer surfaces project-scoped pages —
  workspaces remain reachable from `/workspaces` and from the scope
  switcher, keeping the wiki sidebar focused on enterprise-wide
  knowledge.
- The wiki graph no longer draws the dashed convex-hull boundaries
  around department and project clusters; nodes and edges stand on
  their own.
- The `Wiki` button previously added to department cards was removed
  once the scope switcher and clickable tree scope headers landed —
  redundant entry points were creating clutter.

### Fixed

- Department-scoped detail pages no longer fetch
  `/api/projects/<id>/wiki` (which 404s on department IDs); the tree
  uses the new general scope-aware `/api/wiki/pages` endpoint instead.
- Clicking a backlink, outlink, or inline `[[wikilink]]` from a scoped
  page used to drop scope context and load the flat "old" tree.
- Navigating to a global wiki page used to render with the legacy flat
  tree while `/wiki` showed the new grouped tree.
- `DELETE /api/wiki/pages/<slug>` returned 404 for workspace pages
  because the endpoint looked them up with the default global scope.
  Even after that lookup was fixed, the cascade helper re-fetched the
  row with the same default and silently no-op'd the actual delete,
  returning `{"ok": true}` while the row remained in the database.
  `delete_page_cascade` now takes the resolved `WikiPage` object so
  no second lookup is performed.
- The summary block under the page title piped `page.summary` straight
  into ReactMarkdown without the `[[wikilink]]` preprocessing step
  used by the main content renderer, so users saw raw `[[Arkon]]` and
  bare `**...**` markers in the header of every page. Wikilinks now
  resolve through the same preprocessor and inherit the active scope.

### Backend

- Frontend `WikiPageSummary` and Pydantic `WikiPageSummary` both gain
  `scope_name: Optional[str]`; new shared `WikiScope` type for the
  switcher payload.
- `_build_wiki_scope_filter` is reused by both `/wiki/pages` and the
  new `/wiki/my-scopes` endpoint.
- `regenerate_index` is called with the deleted page's actual scope so
  the right `_index` is rebuilt after a non-global delete.

---

## [0.3.1] — 2026-05-14

### Added

- **Wiki Graph — Department Clustering**: Wiki pages scoped to a department now visually group into department clusters on the `/wiki/graph` canvas.
  - Convex hull drawn per department (below project hulls) with a distinct color per department.
  - Force simulation biases nodes toward their department's X-zone (70% scope pull, 30% component spread) so related pages naturally converge.
  - Legend lists each department with icon `business` and page count.
  - Tooltip shows department name for dept-scoped pages.

### Fixed

- Graph endpoint now joins the `Department` table so `scope_name` is populated for department-scoped pages (previously only `Project` was joined, leaving dept nodes without a name label).

---

## [0.3.0] — 2026-05-13

### Added

- **Department-level Wiki Isolation**: Wiki pages compiled from department-scoped sources are now restricted to members of that department.
  - `ScopeType.DEPARTMENT` added to the enum; pipeline `_resolve_wiki_scopes()` resolves project > department(s) > global, fanning out multi-department sources into one page per department scope (LLM runs once, content is duplicated to each scope).
  - `wiki_service._scope_filter_with_dept()` provides a single-query OR filter (global + user's department).
  - `get_wiki_page` returns HTTP 403 for cross-department access.
  - Source PATCH: changing department on a `ready` source triggers wiki detach, old-scope index regeneration, and MRP re-queue automatically.
  - Frontend: edit-source dialog warns before department reassignment triggers re-analysis.

- **MRP Pipeline — Plan Regeneration with Reviewer Feedback**: Admin can now reject a pending plan with a note, triggering LLM-based regeneration that incorporates the feedback.
  - `POST /sources/{id}/plan/regenerate` runs in the background via `regenerate_plan_task`.
  - Plan Review Dialog surfaces a *Regenerate* button that requires a reviewer note.
  - `_resolve_maybe_items` uses LLM to decide UPDATE vs CREATE (previously always downgraded MAYBE to CREATE).

- **Catalog-driven LLM & Vision Selection**: Replaces free-form `llm_provider + llm_model_id` config with curated catalogs (`LLMModelSpec`, `VisionModelSpec`) that expose context window size, tool support, vision capability, and per-token cost.
  - `/api/settings/{llm,vision}/{catalog,switch}` endpoints mirror the embedding catalog pattern.
  - Settings UI renders a `ModelCatalogCard` per capability with metadata (context window, costs, tool/vision badges).
  - `writer._get_source_context_budget` reads `context_window_tokens` from the spec — the stale hard-coded table is removed.

- **Gemini Model Updates**: Catalog updated with newer Gemini variants.
  - `gemini-3.1-flash-lite`: 1M context, tools + vision + thinking, cheapest Google 1M option ($0.25 in / $1.50 out per 1M tokens). New recommended default for high-volume extraction and captioning.
  - `gemini-3-flash-preview` and two additional preview models added.
  - Admins on `gemini-3.1-flash` must reselect in Settings (model removed from catalog).

### Fixed

- **MRP Pipeline Hardening** (critical):
  - Draft results (`PageWriteResult`) now persisted in `plan_json._page_drafts`; VERIFY/COMMIT phases resume without re-running REFINE.
  - `caption_images_task` is now serialized before `ingest_map_reduce_task`, baking captions into `source.full_text` before MAP runs — fixes the race condition that produced empty image markers in compiled wiki pages.
  - KB reconciliation searches every destination scope and retains the best semantic match, preventing duplicate pages when the same concept exists across scopes.

- **MRP Pipeline Hardening** (high):
  - `assemble_evidence` uses word-boundary regex (`\bterm\b`) instead of substring matching, so short entity names (e.g. "AI") no longer match unrelated subjects ("MAIL").
  - `/sources/{id}/plan/regenerate` runs async via arq; UI polls `GET /plan` instead of holding an open HTTP connection.
  - JSON fence stripping unified via `parse_json_loose`; removes several incorrect `str.strip("```json")` variants in mapper and wiki_analyzer.

- **MRP Pipeline Hardening** (medium):
  - Approve/reject/regenerate endpoints use `SELECT FOR UPDATE` and reject mismatched status to prevent race conditions.

---

## [0.2.x] — prior releases

See git log.
