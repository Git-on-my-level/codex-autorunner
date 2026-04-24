// GENERATED FILE - do not edit directly. Source: static_src/
import { clearChatHistory } from "./docChatStorage.js?v=ac0c75a9b48302989280b9278c713a406824bfb9e317de690d6949a4bd54d2e3";
const DISMISS_KEY = "car-walkthrough-dismissed";
const PMA_ONBOARDING_PRESET_KEY = "car-pma-pending-prompt";
/** Must match ``pma.ts`` ``pmaConfig.storage`` + ``setTarget("pma")`` (``docChatStorage`` key = ``car.pma.pma``). */
const PMA_LOCAL_CHAT = { keyPrefix: "car.pma.", maxMessages: 100, version: 1 };
/** Query param: ``?carOnboarding=1`` clears onboarding dismiss, pending PMA prompt, **PMA local chat history**, then is stripped. */
export const CAR_ONBOARDING_URL_PARAM = "carOnboarding";
export const ONBOARDING_ASSISTANT_INTRO = "I’m the PM Agent for CAR. I can help you set up CAR on this machine, connect " +
    "chat integrations like Discord or Telegram, add your first repo, and walk you " +
    "through the first workflow step by step.\n\n" +
    "The message below is prefilled so you can send it immediately and I’ll guide " +
    "the setup from there.";
export const ONBOARDING_PROMPT = "Walk me through setting up CAR on this machine, including potential chat app " +
    "integrations like Discord or Telegram.";
/**
 * Apply ``?carOnboarding=1`` before PMA initializes (call from the start of ``initHubShell``) so
 * locally persisted PMA messages do not reappear on a "clean slate" run.
 */
export function consumeOnboardingUrlReset() {
    try {
        const url = new URL(window.location.href);
        if (url.searchParams.get(CAR_ONBOARDING_URL_PARAM) !== "1") {
            return;
        }
        try {
            localStorage.removeItem(DISMISS_KEY);
        }
        catch {
            // ignore
        }
        try {
            clearChatHistory(PMA_LOCAL_CHAT, "pma");
        }
        catch {
            // ignore
        }
        try {
            sessionStorage.removeItem(PMA_ONBOARDING_PRESET_KEY);
        }
        catch {
            // ignore
        }
        url.searchParams.delete(CAR_ONBOARDING_URL_PARAM);
        if (typeof history !== "undefined" && history.replaceState) {
            history.replaceState(null, "", url.toString());
        }
    }
    catch {
        // ignore
    }
}
function isOnboardingSeen() {
    try {
        return localStorage.getItem(DISMISS_KEY) === "1";
    }
    catch {
        return false;
    }
}
function markOnboardingSeen() {
    try {
        localStorage.setItem(DISMISS_KEY, "1");
    }
    catch {
        // ignore
    }
}
/**
 * Write the onboarding preset into sessionStorage so PMA can seed the intro chat
 * message and composer prefill on first open. No-op if onboarding was previously
 * seen. Returns true if a preset was scheduled.
 */
export function scheduleOnboardingPromptIfFirstRun() {
    if (isOnboardingSeen())
        return false;
    try {
        const preset = {
            assistantIntro: ONBOARDING_ASSISTANT_INTRO,
            prompt: ONBOARDING_PROMPT,
        };
        sessionStorage.setItem(PMA_ONBOARDING_PRESET_KEY, JSON.stringify(preset));
    }
    catch {
        return false;
    }
    markOnboardingSeen();
    return true;
}
