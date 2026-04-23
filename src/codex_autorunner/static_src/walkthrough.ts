const DISMISS_KEY = "car-walkthrough-dismissed";

const PROMPTS: Record<string, string> = {
  discord:
    "Walk me through setting up Discord notifications for CAR using the existing CAR Discord setup guide.",
  telegram:
    "Walk me through setting up Telegram notifications for CAR using the existing CAR Telegram setup guide.",
  "add-repo":
    "Help me add my first repository to CAR. Walk me through the setup steps.",
  "run-ticket":
    "Help me create and run my first CAR ticket. Start with a simple example.",
};

const TOTAL_STEPS = 3;
let currentStep = 1;

function getStrip(): HTMLElement | null {
  return document.getElementById("walkthrough-strip");
}

function isDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === "1";
  } catch {
    return false;
  }
}

function markDismissed(): void {
  try {
    localStorage.setItem(DISMISS_KEY, "1");
  } catch {
    // ignore
  }
}

function dismiss(): void {
  const strip = getStrip();
  if (strip) strip.classList.add("hidden");
  markDismissed();
}

function showStep(step: number): void {
  for (let i = 1; i <= TOTAL_STEPS; i++) {
    const el = document.getElementById(`walkthrough-step-${i}`);
    if (!el) continue;
    el.classList.toggle("hidden", i !== step);
  }
}

function advance(): void {
  if (currentStep >= TOTAL_STEPS) {
    dismiss();
    return;
  }
  currentStep += 1;
  showStep(currentStep);
}

function firePrompt(key: string): void {
  const prompt = PROMPTS[key];
  if (!prompt) return;
  try {
    sessionStorage.setItem("car-pma-pending-prompt", prompt);
  } catch {
    // ignore
  }
  const pmaBtn = document.querySelector<HTMLButtonElement>(
    '[data-hub-mode="pma"]:not([disabled])'
  );
  if (pmaBtn) {
    pmaBtn.click();
  }
  document.dispatchEvent(
    new CustomEvent("pma:inject-prompt", { detail: { prompt } })
  );
}

export function initWalkthrough(): void {
  if (isDismissed()) return;
  const strip = getStrip();
  if (!strip) return;
  strip.classList.remove("hidden");
  currentStep = 1;
  showStep(currentStep);

  strip.addEventListener("click", (evt) => {
    const target = evt.target as HTMLElement | null;
    if (!target) return;
    const closeBtn = target.closest("#walkthrough-close");
    if (closeBtn) {
      dismiss();
      return;
    }
    const skipBtn = target.closest<HTMLButtonElement>("[data-wt-skip]");
    if (skipBtn) {
      advance();
      return;
    }
    const chip = target.closest<HTMLButtonElement>("[data-wt-prompt]");
    if (chip) {
      const key = chip.dataset.wtPrompt || "";
      firePrompt(key);
      if (currentStep >= TOTAL_STEPS) {
        setTimeout(() => dismiss(), 500);
      } else {
        advance();
      }
    }
  });
}
