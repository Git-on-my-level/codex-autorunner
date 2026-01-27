// GENERATED FILE - do not edit directly. Source: static_src/
"use strict";
(() => {
    const loadScripts = () => {
        const suffix = window.__assetSuffix || "";
        const addScript = (src, { type } = {}) => new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = `${src}${suffix}`;
            if (type)
                script.type = type;
            script.async = false;
            script.onload = () => resolve();
            script.onerror = reject;
            document.body.appendChild(script);
        });
        addScript("static/vendor/xterm.js")
            .then(() => addScript("static/vendor/xterm-addon-fit.js"))
            .then(() => addScript("static/app.js", { type: "module" }))
            .catch((err) => {
            console.error("Failed to load assets", err);
        });
    };
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", loadScripts, { once: true });
    }
    else {
        loadScripts();
    }
})();
