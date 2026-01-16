const js = require("@eslint/js");
const globals = require("globals");

module.exports = [
  {
    ignores: ["src/codex_autorunner/static/vendor/**"],
  },
  js.configs.recommended,
  {
    files: ["src/codex_autorunner/static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "module",
      globals: globals.browser,
    },
    rules: {
      "no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          caughtErrors: "none",
        },
      ],
    },
  },
];
