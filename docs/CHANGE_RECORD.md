# SlideAI Change Record (Engineering)

> Purpose: Keep module/system changes traceable, reviewable, and easy to merge.

## Mandatory Rule (Must Follow)
1. Any future module-level or system-level change **must be recorded in this file** before merge.
2. Each PR/merge should include at least one new entry in the `Change Entries` section.
3. Do not mix unrelated changes in one entry.
4. If a change is reverted, add a new `REVERT` entry (do not silently delete history).

## Scope
- Record functional changes, API behavior changes, config changes, infra/runtime changes, schema changes, and risk fixes.
- Skip trivial formatting-only changes unless they affect behavior.

## Entry Format (Required)
Copy this template for every change:

```md
### [CR-YYYYMMDD-XXX] <Title>
- Date: YYYY-MM-DD
- Author: <name>
- Type: feature | fix | refactor | config | infra | docs | revert
- Scope: frontend | backend | api | db | deploy | cross-module
- Related Issue/PR: <id or N/A>

#### Why
- <problem/background>

#### What Changed
- Files:
  - <path>
  - <path>
- Behavior:
  - <before>
  - <after>

#### API/Contract Impact
- Endpoint/Interface: <none or details>
- Backward Compatibility: compatible | breaking

#### Risks
- <known risks>

#### Verification
- <how verified>

#### Rollback
- <how to rollback>
```

## Merge Checklist (Quick)
- [ ] Change is isolated to intended module scope.
- [ ] Entry added to `docs/CHANGE_RECORD.md`.
- [ ] API/contract impact documented.
- [ ] Risk + rollback documented.
- [ ] Local verification result documented.

## Agent Handoff Note
When asking an agent to implement or merge changes, include:
1. Target scope (which modules can be modified)
2. Non-target scope (what must not be touched)
3. Required output: update `docs/CHANGE_RECORD.md` with a new entry
4. Acceptance criteria + test evidence

---

## Change Entries

### [CR-20260906-001] Canonical project workflow, recovery and shared page images
- Date: 2026-09-06
- Author: Codex (implementation and verification)
- Type: fix
- Scope: cross-module
- Related Issue/PR: N/A

#### Why
- Legacy endpoints and UI fallbacks bypassed persistent projects; previews repeated work and interrupted jobs lacked reliable input snapshots.
- ASS rendering could fail with an undefined helper; adjacent menus could overlap and change text appearance on hover.

#### What Changed
- Files: backend/app/api/video.py, video_runs.py; services/artifact_store.py, page_images.py, utility/api.py; frontend/src/components and composables; backend/scripts/video_run_cli.py; scripts/smoke_test.sh; README.md.
- Behavior: require run/page/variant IDs for rendering, reuse saved assets, remove unused account/admin UI and text-only LLM helpers, generate scripts independently from upload, retain model-free startup.
- Save job inputs for opt-in interrupted recovery. Share one 1080p-bounded JPEG and one thumbnail per page; prioritize initial preview then preload other pages.
- Restore ASS helper import, validate uploads, bound FFmpeg execution, update main-environment dependencies, and make dropdowns mutually exclusive with stable hover text.
- Preserve the existing reverse-proxy router base adjustment. Keep local audit notes and runtime output out of source handoff bundles and Git.

#### API/Contract Impact
- Endpoint/Interface: remove legacy thumbnail, standalone alignment/render and uploaded-media merge endpoints; CLI and smoke script use persistent run assets.
- Backward Compatibility: breaking for old clients without run_id; current WebUI, CLI and backend must be deployed together.

#### Risks
- Full GPU/LLM generation and a fresh Docker deployment were not rerun for this release. External model runtimes require separate dependency and compatibility checks.
- Older interrupted jobs without input snapshots require a new job. Image migration updates cached images in place; it retains source PDFs and generated media.

#### Verification
- Backend: 59 tests and 11 subtests passed, including real FFmpeg render/persistence in burn, sidecar and none modes.
- Frontend: 17 Node tests, one Chromium menu/mount test, and production build passed.
- Basic live smoke test and shell syntax checks passed. Full model smoke test was updated but not executed.

#### Rollback
- Original branches remain unchanged; switch to the preceding commit (7c8145f) to restore source. Back up data/video_runs separately before experimenting with older code; Git does not version user artifacts.


### [CR-20260417-001] Frontend Dev API Default Port Alignment (8000 -> 8001)
- Date: 2026-04-17
- Author: Codex Agent
- Type: fix
- Scope: frontend, api
- Related Issue/PR: N/A

#### Why
- In local dev, backend service runs on port `8001`, but frontend debug/dev path was still defaulting to `8000`, causing API 404 in register/login/debug checks.

#### What Changed
- Files:
  - `frontend/src/config/api.js`
  - `frontend/vite.config.js`
- Behavior:
  - Before: frontend dev default API URL/proxy target used `http://localhost:8000`
  - After: frontend dev default API URL/proxy target uses `http://localhost:8001`

#### API/Contract Impact
- Endpoint/Interface: none (same endpoints)
- Backward Compatibility: compatible

#### Risks
- Teammates who still run backend on `8000` need to set `VITE_API_URL` explicitly.

#### Verification
- Checked frontend `/api/me` through WebUI dev server returns `401 Not authenticated` (expected when no token), confirming API is routed to live backend rather than wrong port 404.

#### Rollback
- Revert the two files above to `8000` defaults, or set `VITE_API_URL` per developer environment.

### [CR-20260731-001] Batch Rendering Uses Stage-Based GPU Scheduling
- Date: 2026-07-31
- Author: Codex Agent
- Type: refactor
- Scope: frontend
- Related Issue/PR: N/A

#### Why
- The render-all workflow previously alternated TTS, forced alignment, and video rendering for every page.
- TTS and alignment intentionally release each other's GPU worker, so page-wise alternation caused both models to reload repeatedly in a multi-page project.

#### What Changed
- Files:
  - `frontend/src/composables/useRenderQueue.js`
- Behavior:
  - Before: each page ran `TTS -> alignment -> video`, then the next page repeated the same sequence.
  - After: the batch runs `all TTS -> all alignment -> all video rendering`.
  - Single-page rendering is unchanged.
  - `none`, `sidecar`, and `burn` subtitle modes retain their original output behavior.
  - Prepared browser-side audio/alignment objects are released after each page render and cleared on completion or failure.

#### API/Contract Impact
- Endpoint/Interface: none
- Backward Compatibility: compatible

#### Risks
- A large batch temporarily retains the generated audio blobs until the alignment/render stages finish.
- A later backend-orchestrated job API should replace these browser-side intermediate blobs for very large projects.

#### Verification
- `npm run build`: passed with Vite 5.4.21.
- `scripts/smoke_test.sh`: passed frontend, backend, history API, LLM status, and CORS checks.
- Mocked three-page workflow:
  - `none`: all TTS requests completed before all video requests.
  - `sidecar`: all TTS requests completed before all alignment requests, followed by all video requests.
  - `burn`: all TTS requests completed before all alignment requests, followed by all video requests.

#### Rollback
- Restore `frontend/src/composables/useRenderQueue.js` from `/home/g114056175/my_vscode/SlideAI.20260731.bak`.

### [CR-20260731-002] Use Video-Run PDF as the Canonical Upload
- Date: 2026-07-31
- Author: Codex Agent
- Type: refactor
- Scope: backend
- Related Issue/PR: N/A

#### Why
- Every upload kept one UUID PDF under `backend/app/tmp_pdf` and another copy under the persistent video run.
- Re-uploading the same source accumulated duplicate PDFs without a retention policy.

#### What Changed
- Files:
  - `backend/app/api/video.py`
  - `backend/app/services/artifact_store.py`
- Behavior:
  - New uploads are written directly to `data/video_runs/<run_id>/input/`.
  - The run manifest PDF is the single canonical project PDF.
  - The legacy `pdf_id` thumbnail endpoint now resolves the matching run manifest and uses its cached page image or canonical PDF.
  - New uploads no longer write to `backend/app/tmp_pdf`.
  - The existing 11 temporary PDF copies and the obsolete `tmp_pdf` directory were deleted.
  - Obsolete cleanup/exclusion references were removed from the smoke test and handoff-bundle scripts.

#### API/Contract Impact
- Endpoint/Interface: `/api/video-abstract/thumbnail` keeps the same query contract.
- Backward Compatibility: compatible for projects that have a persistent video-run manifest.

#### Risks
- A legacy database-only project with no matching `data/video_runs` manifest and no persistent thumbnail can no longer use the removed temporary PDF fallback.
- Current Lab projects all use video-run manifests and the run-specific thumbnail API.

#### Verification
- Python compilation passed for `video.py` and `artifact_store.py`.
- Existing `pdf_id` values resolved to their persistent manifests.
- Three existing projects returned valid 1280px JPEG thumbnails through the legacy endpoint.
- A real three-page PDF upload created only the canonical run PDF, returned a valid thumbnail, and did not recreate `tmp_pdf`.
- The temporary test run was removed.
- `scripts/smoke_test.sh` passed after backend restart.

#### Rollback
- Restore the two backend files and any required legacy PDFs from `/home/g114056175/my_vscode/SlideAI.20260731.bak`.

### [CR-20260731-003] Safer Bilingual Subtitle Splitting and Numeric Alignment Repair
- Date: 2026-07-31
- Author: Codex Agent
- Type: fix
- Scope: backend, tests
- Related Issue/PR: N/A

#### Why
- English titles and reference abbreviations could create accidental one-word subtitles such as `Dr`.
- Times such as `10:30` could be split at the colon, and mixed tokens such as `Qwen3 ForcedAligner` could lose their space.
- Short-line rebalancing could duplicate already-moved tokens.
- Numeric/unit repair did not detect split aligner units (`1.23` + `GB`), could mistake a natural pause for an anomaly, used Chinese spoken forms for English, and accepted a second pass without a quality gate.

#### What Changed
- Files:
  - `backend/app/services/alignment/sentence_splitter.py`
  - `backend/app/services/alignment/repair.py`
  - `backend/app/services/subtitle_alignment.py`
  - `backend/tests/test_sentence_splitter.py`
  - `backend/tests/test_alignment_repair.py`
- Behavior:
  - Protects English titles, initials, reference abbreviations, clocks, compact colons, URLs, ports, versions, decimals, numeric units, code spans, and closing quotes.
  - Restores English spacing without separating compact numeric units.
  - Prevents duplicate token movement during orphan-line repair.
  - Prefers complete English prepositional/subordinate phrases and merges soft-fit short lines.
  - Detects combined and split numeric/unit alignment anomalies using duration, following gaps, and local timing rates.
  - Rewrites numeric/unit text with language-aware Chinese or English spoken forms.
  - Corrects Chinese large-number zero handling.
  - Rejects a second alignment pass when text similarity, span count, monotonicity, duration, or audio bounds are invalid.

#### API/Contract Impact
- Endpoint/Interface: none
- Backward Compatibility: compatible; existing request/response fields and subtitle modes are unchanged.

#### Risks
- Abbreviation and line-break preferences remain rule based; uncommon domain abbreviations may need to be added to the regression corpus.
- Numeric spoken forms depend on how the selected TTS reads a unit. Failed or low-quality second passes retain the original timeline.

#### Verification
- Python compilation passed for the splitter, repair module, alignment service, and tests.
- 14 deterministic regression tests passed.
- 384 generated mixed Chinese/English combinations preserved all spoken text, with no accidental abbreviation/time fragments.
- Existing project corpus: 3 script files, 31 pages, 550 subtitle segments; zero text-preservation failures, zero segments under 4 display units, and zero segments over the 36.5 soft maximum.
- Splitter benchmark on the existing 31 pages: about 8.9 ms total / 0.29 ms per page after warm-up.
- Live VoxCPM2 + Qwen3 ForcedAligner test passed for Chinese and English technical scripts.
- The Chinese live test detected a collapsed `1.23GB` word timeline and expanded it from roughly 1 ms to roughly 720 ms while preserving the same page-level segment boundaries.
- `scripts/smoke_test.sh` passed after the native service restart.

#### Rollback
- Restore the three backend service files from `/home/g114056175/my_vscode/SlideAI.20260731.bak` and remove the two added regression-test files.
