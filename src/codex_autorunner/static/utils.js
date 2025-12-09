const toast = document.getElementById("toast");
const decoder = new TextDecoder();

export function flash(message) {
  toast.textContent = message;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2200);
}

export function statusPill(el, status) {
  el.textContent = status || "idle";
  el.classList.remove("pill-idle", "pill-running", "pill-error");
  if (status === "running") {
    el.classList.add("pill-running");
  } else if (status === "error") {
    el.classList.add("pill-error");
  } else {
    el.classList.add("pill-idle");
  }
}

export async function api(path, options = {}) {
  const headers = options.headers ? { ...options.headers } : {};
  const opts = { ...options, headers };
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return res.json();
  }
  return res.text();
}

export function streamEvents(
  path,
  { method = "GET", body = null, onMessage, onError, onFinish } = {}
) {
  const controller = new AbortController();
  let fetchBody = body;
  const headers = {};
  if (fetchBody && typeof fetchBody === "object" && !(fetchBody instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    fetchBody = JSON.stringify(fetchBody);
  }
  fetch(path, { method, body: fetchBody, headers, signal: controller.signal })
    .then(async (res) => {
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Request failed (${res.status})`);
      }
      if (!res.body) {
        throw new Error("Streaming not supported in this browser");
      }
      const reader = res.body.getReader();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop();
        for (const chunk of chunks) {
          if (!chunk.trim()) continue;
          const lines = chunk.split("\n");
          let event = "message";
          const dataLines = [];
          for (const line of lines) {
            if (line.startsWith("event:")) {
              event = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trimStart());
            }
          }
          const data = dataLines.join("\n");
          if (onMessage) onMessage(data, event || "message");
        }
      }
      if (!controller.signal.aborted && onFinish) {
        onFinish();
      }
    })
    .catch((err) => {
      if (controller.signal.aborted) {
        if (onFinish) onFinish();
        return;
      }
      if (onError) onError(err);
      if (onFinish) onFinish();
    });

  return () => controller.abort();
}

export function createPoller(fn, intervalMs, { immediate = true } = {}) {
  let timer = null;
  const tick = async () => {
    try {
      await fn();
    } finally {
      timer = setTimeout(tick, intervalMs);
    }
  };
  if (immediate) {
    tick();
  } else {
    timer = setTimeout(tick, intervalMs);
  }
  return () => {
    if (timer) clearTimeout(timer);
  };
}
