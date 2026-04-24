// GENERATED FILE - do not edit directly. Source: static_src/
import { api, confirmModal, flash } from "./utils.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
import { describeUpdateTarget, getUpdateTarget, includesWebUpdateTarget, normalizeUpdateTarget, updateRestartNotice, updateTargetOptionsFromResponse, } from "./updateTargets.js?v=d636841caa7dd973f2c785ff2cd6199585023d519a2eb5a61d2f799a9872679f";
export async function loadUpdateTargetOptions(selectId) {
    const select = selectId
        ? document.getElementById(selectId)
        : null;
    if (!select)
        return;
    const isInitialized = select.dataset.updateTargetsInitialized === "1";
    const previousValue = select.value;
    let payload = null;
    let loadFailed = false;
    try {
        payload = (await api("/system/update/targets", {
            method: "GET",
        }));
    }
    catch (_err) {
        loadFailed = true;
    }
    const { options, defaultTarget } = updateTargetOptionsFromResponse(loadFailed ? null : payload);
    select.replaceChildren();
    if (loadFailed) {
        const errorOption = document.createElement("option");
        errorOption.value = "";
        errorOption.textContent = "Failed to load update targets \u2014 refresh to retry";
        select.appendChild(errorOption);
        select.disabled = true;
        return;
    }
    if (!options.length) {
        const emptyOption = document.createElement("option");
        emptyOption.value = "";
        emptyOption.textContent = "No update targets available";
        select.appendChild(emptyOption);
        select.disabled = true;
        return;
    }
    select.disabled = false;
    const previous = normalizeUpdateTarget(previousValue || "all");
    const hasPrevious = options.some((item) => item.value === previous);
    const hasDefault = options.some((item) => item.value === defaultTarget);
    options.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.value;
        option.textContent = item.label;
        select.appendChild(option);
    });
    if (isInitialized) {
        select.value = hasPrevious
            ? previous
            : hasDefault
                ? defaultTarget
                : options[0].value;
    }
    else {
        select.value = hasDefault ? defaultTarget : options[0].value;
        select.dataset.updateTargetsInitialized = "1";
    }
}
export async function handleSystemUpdate(btnId, targetSelectId) {
    const btn = document.getElementById(btnId);
    if (!btn)
        return;
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Checking...";
    const updateTarget = getUpdateTarget(targetSelectId);
    const targetLabel = describeUpdateTarget(updateTarget);
    let check;
    try {
        check = (await api("/system/update/check"));
    }
    catch (err) {
        flash(`Unable to check for updates: ${err.message || "unknown error"}`, "error");
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    if (!check?.update_available) {
        flash(check?.message || "No update available.", "info");
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    const restartNotice = updateRestartNotice(updateTarget);
    const confirmed = await confirmModal(`${check?.message || "Update available."} Update Codex Autorunner (${targetLabel})? ${restartNotice}`);
    if (!confirmed) {
        btn.disabled = false;
        btn.textContent = originalText;
        return;
    }
    btn.textContent = "Updating...";
    try {
        let res = (await api("/system/update", {
            method: "POST",
            body: { target: updateTarget },
        }));
        if (res.requires_confirmation) {
            const forceConfirmed = await confirmModal(res.message || "Active sessions are still running. Update anyway?", { confirmText: "Update anyway", cancelText: "Cancel", danger: true });
            if (!forceConfirmed) {
                btn.disabled = false;
                btn.textContent = originalText;
                return;
            }
            res = (await api("/system/update", {
                method: "POST",
                body: { target: updateTarget, force: true },
            }));
        }
        flash(res.message || `Update started (${targetLabel}).`, "success");
        if (!includesWebUpdateTarget(updateTarget)) {
            btn.disabled = false;
            btn.textContent = originalText;
            return;
        }
        document.body.style.pointerEvents = "none";
        setTimeout(() => {
            const url = new URL(window.location.href);
            url.searchParams.set("v", String(Date.now()));
            window.location.replace(url.toString());
        }, 8000);
    }
    catch (err) {
        flash(err.message || "Update failed", "error");
        btn.disabled = false;
        btn.textContent = originalText;
    }
}
