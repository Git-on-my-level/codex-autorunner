# TODO

- [x] Inventory JavaScript files by package/runtime boundary and decide migration order.
- [x] Review current TypeScript config and update it for JS->TS migration (module resolution, path aliases, build outputs).
- [x] Add `tsc --noEmit` to pre-commit hooks.
- [x] Add `tsc --noEmit` to CI pipeline.
 - [x] Migrate Phase 1: Core utilities (bus.js, constants.js, tabs.js, cache.js, todoPreview.js, loader.js)
 - [x] Migrate Phase 2: Small feature modules (docs.js, terminal.js, docsDrafts.js, docsThreadRegistry.js, docsVoice.js, docsClipboard.js, docsDocUpdates.js, docsSnapshot.js)
 - [x] Migrate Phase 3: Core state & utilities (state.js, env.js, app.js, snapshot.js, bootstrap.js, docsState.js)
 - [x] Migrate Phase 4: Feature modules (docsParse.js, github.js, docsUi.js, docsElements.js, autoRefresh.js, docChatRender.js, docsSpecIngest.js)
 - [x] Migrate Phase 5: Larger feature modules (docsCrud.js, docChatActions.js, docChatEvents.js, mobileCompact.js, docsInit.js, settings.js, agentControls.js, docChatStream.js)
 - [x] Migrate Phase 6: Complex/core infrastructure (runs.js, voice.js, utils.js, logs.js, dashboard.js, hub.js, terminalManager.js)
 - [x] Remove leftover JS sources and update build/lint configs to target TS only.
 - [x] Validate end-to-end: build, lint, tests, and typecheck passing.
- [ ] Open a PR and ensure the PR passes CI with the new type checks
