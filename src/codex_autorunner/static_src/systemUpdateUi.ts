import { api, confirmModal, flash } from "./utils.js";
import {
  describeUpdateTarget,
  getUpdateTarget,
  includesWebUpdateTarget,
  normalizeUpdateTarget,
  type UpdateTargetsResponse,
  updateRestartNotice,
  updateTargetOptionsFromResponse,
} from "./updateTargets.js";

interface UpdateCheckResponse {
  update_available?: boolean;
  message?: string;
}

interface UpdateResponse {
  message?: string;
  requires_confirmation?: boolean;
}

export async function loadUpdateTargetOptions(
  selectId: string | null
): Promise<void> {
  const select = selectId
    ? (document.getElementById(selectId) as HTMLSelectElement | null)
    : null;
  if (!select) return;
  const isInitialized = select.dataset.updateTargetsInitialized === "1";
  let payload: UpdateTargetsResponse | null;
  try {
    payload = (await api("/system/update/targets", {
      method: "GET",
    })) as UpdateTargetsResponse;
  } catch (_err) {
    return;
  }
  const { options, defaultTarget } = updateTargetOptionsFromResponse(payload);
  if (!options.length) return;

  const previous = normalizeUpdateTarget(select.value || "all");
  const hasPrevious = options.some((item) => item.value === previous);
  const fallback = options.some((item) => item.value === defaultTarget)
    ? defaultTarget
    : options[0].value;

  select.replaceChildren();
  options.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    select.appendChild(option);
  });
  if (isInitialized) {
    select.value = hasPrevious ? previous : fallback;
  } else {
    select.value = fallback;
    select.dataset.updateTargetsInitialized = "1";
  }
}

export async function handleSystemUpdate(
  btnId: string,
  targetSelectId: string | null
): Promise<void> {
  const btn = document.getElementById(btnId) as HTMLButtonElement | null;
  if (!btn) return;

  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Checking...";
  const updateTarget = getUpdateTarget(targetSelectId);
  const targetLabel = describeUpdateTarget(updateTarget);

  let check: UpdateCheckResponse | undefined;
  try {
    check = (await api("/system/update/check")) as UpdateCheckResponse;
  } catch (err) {
    check = {
      update_available: true,
      message:
        (err as Error).message || "Unable to check for updates.",
    };
  }

  if (!check?.update_available) {
    flash(check?.message || "No update available.", "info");
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  const restartNotice = updateRestartNotice(updateTarget);
  const confirmed = await confirmModal(
    `${check?.message || "Update available."} Update Codex Autorunner (${targetLabel})? ${restartNotice}`
  );
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
    })) as UpdateResponse;
    if (res.requires_confirmation) {
      const forceConfirmed = await confirmModal(
        res.message || "Active sessions are still running. Update anyway?",
        { confirmText: "Update anyway", cancelText: "Cancel", danger: true }
      );
      if (!forceConfirmed) {
        btn.disabled = false;
        btn.textContent = originalText;
        return;
      }
      res = (await api("/system/update", {
        method: "POST",
        body: { target: updateTarget, force: true },
      })) as UpdateResponse;
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
  } catch (err) {
    flash((err as Error).message || "Update failed", "error");
    btn.disabled = false;
    btn.textContent = originalText;
  }
}
