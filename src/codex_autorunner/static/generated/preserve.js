// GENERATED FILE - do not edit directly. Source: static_src/
export function preserveScroll(el, render, opts) {
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
