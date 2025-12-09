import { flash, statusPill } from "./utils.js";
import { subscribe } from "./bus.js";
import { loadState, startRun, stopRun, resumeRun, killRun, startStatePolling } from "./state.js";

function renderState(state) {
  if (!state) return;
  statusPill(document.getElementById("runner-status"), state.status);
  document.getElementById("last-run-id").textContent = state.last_run_id ?? "–";
  document.getElementById("last-exit-code").textContent = state.last_exit_code ?? "–";
  document.getElementById("last-start").textContent = state.last_run_started_at ?? "–";
  document.getElementById("last-finish").textContent = state.last_run_finished_at ?? "–";
  document.getElementById("todo-count").textContent = state.outstanding_count ?? "–";
  document.getElementById("done-count").textContent = state.done_count ?? "–";
  document.getElementById("runner-pid").textContent = `Runner pid: ${state.runner_pid ?? "–"}`;
}

function bindAction(buttonId, action) {
  const btn = document.getElementById(buttonId);
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await action();
    } catch (err) {
      flash(err.message);
    } finally {
      btn.disabled = false;
    }
  });
}

export function initDashboard() {
  subscribe("state:update", renderState);
  bindAction("start-run", () => startRun(false));
  bindAction("start-once", () => startRun(true));
  bindAction("stop-run", stopRun);
  bindAction("resume-run", resumeRun);
  bindAction("kill-run", killRun);
  bindAction("refresh-state", loadState);

  startStatePolling();
}
