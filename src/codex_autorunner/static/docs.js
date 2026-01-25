// GENERATED FILE - do not edit directly. Source: static_src/
import { initDocs } from "./docsInit.js";
import { applyDocUpdateFromChat } from "./docsDocUpdates.js";
import { applyPatch, discardPatch, reloadPatch } from "./docChatActions.js";
import { getChatState } from "./docsState.js";
import { handleStreamEvent, performDocChatRequest, applyChatResult } from "./docChatStream.js";
import { renderChat } from "./docChatRender.js";
import { setDoc } from "./docsCrud.js";
export { initDocs };
export const __docChatTest = {
    applyChatResult,
    applyDocUpdateFromChat,
    applyPatch,
    reloadPatch,
    discardPatch,
    getChatState,
    handleStreamEvent,
    performDocChatRequest,
    renderChat,
    setDoc,
};
