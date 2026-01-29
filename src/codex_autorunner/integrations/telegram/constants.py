from __future__ import annotations

DEFAULT_PAGE_SIZE = 10
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_CALLBACK_DATA_LIMIT = 64
THREAD_LIST_PAGE_LIMIT = 100
THREAD_LIST_MAX_PAGES = 5
DEFAULT_MODEL_LIST_LIMIT = 25
DEFAULT_MCP_LIST_LIMIT = 50
DEFAULT_SKILLS_LIST_LIMIT = 50
MAX_TOPIC_THREAD_HISTORY = 50
RESUME_BUTTON_PREVIEW_LIMIT = 60
RESUME_PREVIEW_USER_LIMIT = 1000
RESUME_PREVIEW_ASSISTANT_LIMIT = 1000
RESUME_PREVIEW_SCAN_LINES = 200
RESUME_MISSING_IDS_LOG_LIMIT = 10
RESUME_REFRESH_LIMIT = 10
TOKEN_USAGE_CACHE_LIMIT = 256
TOKEN_USAGE_TURN_CACHE_LIMIT = 512
DEFAULT_INTERRUPT_TIMEOUT_SECONDS = 30.0


DEFAULT_AGENT_TURN_TIMEOUT_SECONDS = {
    "codex": 28800.0,
    "opencode": 28800.0,
}
DEFAULT_AGENT = "codex"
APP_SERVER_START_BACKOFF_INITIAL_SECONDS = 1.0
APP_SERVER_START_BACKOFF_MAX_SECONDS = 30.0
CACHE_CLEANUP_INTERVAL_SECONDS = 300.0
COALESCE_BUFFER_TTL_SECONDS = 60.0
MEDIA_BATCH_BUFFER_TTL_SECONDS = 60.0
MODEL_PENDING_TTL_SECONDS = 1800.0
PENDING_APPROVAL_TTL_SECONDS = 600.0
PENDING_QUESTION_TTL_SECONDS = 600.0
REASONING_BUFFER_TTL_SECONDS = 900.0
SELECTION_STATE_TTL_SECONDS = 1800.0
TURN_PREVIEW_TTL_SECONDS = 900.0
PROGRESS_STREAM_TTL_SECONDS = 900.0
OVERSIZE_WARNING_TTL_SECONDS = 3600.0
UPDATE_ID_PERSIST_INTERVAL_SECONDS = 60.0
OUTBOX_RETRY_INTERVAL_SECONDS = 10.0
OUTBOX_IMMEDIATE_RETRY_DELAYS = (0.5, 2.0, 5.0)
OUTBOX_MAX_ATTEMPTS = 8
VOICE_RETRY_INTERVAL_SECONDS = 5.0
VOICE_RETRY_INITIAL_SECONDS = 2.0
VOICE_RETRY_MAX_SECONDS = 300.0
VOICE_RETRY_JITTER_RATIO = 0.2
VOICE_MAX_ATTEMPTS = 20
VOICE_RETRY_AFTER_BUFFER_SECONDS = 1.0
WHISPER_TRANSCRIPT_DISCLAIMER = (
    "Note: transcribed from user voice. If confusing or possibly inaccurate and you "
    "cannot infer the intention please clarify before proceeding."
)
DEFAULT_UPDATE_REPO_URL = "https://github.com/Git-on-my-level/codex-autorunner.git"
DEFAULT_UPDATE_REPO_REF = "main"
RESUME_PICKER_PROMPT = (
    "Select a thread to resume (buttons below or reply with number/id)."
)
BIND_PICKER_PROMPT = "Select a repo to bind (buttons below or reply with number/id)."
AGENT_PICKER_PROMPT = "Select an agent (buttons below)."
MODEL_PICKER_PROMPT = "Select a model (buttons below)."
EFFORT_PICKER_PROMPT = "Select a reasoning effort for {model}."
UPDATE_PICKER_PROMPT = "Select update target (buttons below)."
REVIEW_COMMIT_PICKER_PROMPT = (
    "Select a commit to review (buttons below or reply with number)."
)
REVIEW_COMMIT_BUTTON_LABEL_LIMIT = 80
UPDATE_TARGET_OPTIONS = (
    ("both", "Both (web + Telegram)"),
    ("web", "Web only"),
    ("telegram", "Telegram only"),
)
TRACE_MESSAGE_TOKENS = (
    "failed",
    "error",
    "denied",
    "unknown",
    "not bound",
    "not found",
    "invalid",
    "unsupported",
    "disabled",
    "missing",
    "mismatch",
    "different workspace",
    "no previous",
    "no resumable",
    "no workspace-tagged",
    "not applicable",
    "selection expired",
    "timed out",
    "timeout",
    "aborted",
    "canceled",
    "cancelled",
)
PLACEHOLDER_TEXT = "Working..."
QUEUED_PLACEHOLDER_TEXT = "Queued (waiting for available worker...)"
STREAM_PREVIEW_PREFIX = ""
THINKING_PREVIEW_MAX_LEN = 80
THINKING_PREVIEW_MIN_EDIT_INTERVAL_SECONDS = 1.0
TURN_PROGRESS_MAX_LEN = 160
TURN_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.0
TURN_PROGRESS_TTL_SECONDS = 900.0
PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 5.0
COMPACT_MAX_ACTIONS = 10
COMPACT_MAX_TEXT_LENGTH = 80
STATUS_ICONS = {
    "done": "✓",
    "fail": "✗",
    "warn": "⚠",
    "running": "▸",
    "update": "↻",
    "thinking": "🧠",
}
COMMAND_DISABLED_TEMPLATE = "'/{name}' is disabled while a task is in progress."
MAX_MENTION_BYTES = 200_000
VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
VALID_AGENT_VALUES = {"codex", "opencode"}
DEFAULT_AGENT_MODELS = {
    "codex": "gpt-5.2-codex",
    "opencode": "zai-coding-plan/glm-4.7",
}
LEGACY_DEFAULT_AGENT_MODELS = DEFAULT_AGENT_MODELS
CONTEXT_BASELINE_TOKENS = 12000
APPROVAL_POLICY_VALUES = {"untrusted", "on-failure", "on-request", "never"}
APPROVAL_PRESETS = {
    "read-only": ("on-request", "readOnly"),
    "auto": ("on-request", "workspaceWrite"),
    "full-access": ("never", "dangerFullAccess"),
}
SHELL_OUTPUT_TRUNCATION_SUFFIX = "\n...(truncated)"
SHELL_MESSAGE_BUFFER_CHARS = 200
COMPACT_SUMMARY_PROMPT = (
    "Summarize the conversation so far into a concise context block I can paste into "
    "a new thread. Include goals, constraints, decisions, and current state."
)
INIT_PROMPT = "\n".join(
    [
        "Generate a file named AGENTS.md that serves as a contributor guide for this repository.",
        "Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.",
        "Follow the outline below, but adapt as needed - add sections if relevant, and omit those that do not apply to this project.",
        "",
        "Document Requirements",
        "",
        '- Title the document "Repository Guidelines".',
        "- Use Markdown headings (#, ##, etc.) for structure.",
        "- Keep the document concise. 200-400 words is optimal.",
        "- Keep explanations short, direct, and specific to this repository.",
        "- Provide examples where helpful (commands, directory paths, naming patterns).",
        "- Maintain a professional, instructional tone.",
        "",
        "Recommended Sections",
        "",
        "Project Structure & Module Organization",
        "",
        "- Outline the project structure, including where the source code, tests, and assets are located.",
        "",
        "Build, Test, and Development Commands",
        "",
        "- List key commands for building, testing, and running locally (e.g., npm test, make build).",
        "- Briefly explain what each command does.",
        "",
        "Coding Style & Naming Conventions",
        "",
        "- Specify indentation rules, language-specific style preferences, and naming patterns.",
        "- Include any formatting or linting tools used.",
        "",
        "Testing Guidelines",
        "",
        "- Identify testing frameworks and coverage requirements.",
        "- State test naming conventions and how to run tests.",
        "",
        "Commit & Pull Request Guidelines",
        "",
        "- Summarize commit message conventions found in the project's Git history.",
        "- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).",
        "",
        "(Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions.",
    ]
)


TurnKey = tuple[str, str]
