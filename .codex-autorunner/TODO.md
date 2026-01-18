# TODO

- [x] Add review defaults in repo config (DEFAULT_REPO_CONFIG), include `review` in `REPO_DEFAULT_KEYS`, and add housekeeping for `.codex-autorunner/review/runs`
- [x] Implement `core/review.py` ReviewService with lock/state/thread pattern, run dir + scratchpad creation, OpenCode prompt execution, final report read, optional scratchpad bundle, and robust status transitions
- [x] Add reusable OpenCode prompt runner helper in `src/codex_autorunner/agents/opencode/run_prompt.py` and adopt it in ReviewService (and optionally DocChatService) to avoid duplication
- [x] Add review API schemas in `src/codex_autorunner/web/schemas.py` (ReviewStartRequest/ReviewStatusResponse/optional ReviewControlResponse)
- [x] Add `routes/review.py` with status/start/stop/reset/artifact endpoints and safety checks for artifacts
- [x] Wire review routes into `src/codex_autorunner/routes/__init__.py`
- [x] Add Review dashboard card in `src/codex_autorunner/static/index.html` with controls, status, and artifact links
- [x] Add `src/codex_autorunner/static/review.js` and wire it in `static/app.js` with defaults (opencode + `zai-coding-plan/glm-4.7`) and status polling
- [x] Update `src/codex_autorunner/static/styles.css` for Review card layout and artifacts
- [x] Refactor `static/agentControls.ts` for namespaced storage/defaults and update `static/utils.ts` status pill handling (failed/stopping)
- [x] Add tests for ReviewService state/filesystem behavior in `tests/test_review_service.py`
- [x] Update API contract tests in `tests/test_api_contract.py` to include review endpoints
- [x] (Optional, skipped) Add lightweight UI module test for `review.js` similar to doc chat UI tests - skipped due to test setup complexity
- [x] Run `pytest` - npm run build not applicable (project uses .js files directly)
