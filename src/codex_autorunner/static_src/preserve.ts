export function preserveScroll(
  el: HTMLElement | null,
  render: () => void,
  opts?: { restoreOnNextFrame?: boolean }
): void {
  if (!el) {
    render();
    return;
  }

  const top = el.scrollTop;
  render();

  const restore = () => {
    el.scrollTop = top;
  };

  if (opts?.restoreOnNextFrame && typeof requestAnimationFrame === "function") {
    requestAnimationFrame(restore);
    return;
  }

  restore();
}
