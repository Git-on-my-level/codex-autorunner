// GENERATED FILE - do not edit directly. Source: static_src/
import { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { renderRepos as _renderRepos, renderAgentWorkspaces as _renderAgentWorkspaces } from "./hubRepoCards.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { applyHubPanelState, toggleHubPanel, initInteractionHarness, setHubChannelEntries as setHubChannelEntriesAction, getHubChannelEntries, getPinnedParentRepoIds } from "./hubActions.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
import { attachHandlersAndControls, bootstrapHubData } from "./hubActions.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
export { loadHubBootstrapCache, saveHubBootstrapCache } from "./hubCache.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
export { applyHubPanelState, toggleHubPanel, initInteractionHarness } from "./hubActions.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
export function initHub() {
    attachHandlersAndControls();
    bootstrapHubData();
}
function renderRepos(repos) {
    _renderRepos(repos, getHubChannelEntries(), getPinnedParentRepoIds());
}
function renderAgentWorkspaces(workspaces) {
    _renderAgentWorkspaces(workspaces, getHubChannelEntries());
}
export const __hubTest = {
    renderRepos,
    renderAgentWorkspaces,
    applyHubPanelState,
    toggleHubPanel,
    loadHubBootstrapCache,
    saveHubBootstrapCache,
    initInteractionHarness,
    setHubChannelEntries(entries) {
        setHubChannelEntriesAction(entries);
    },
};
