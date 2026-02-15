DEFAULT_TICKET_MODEL = "zai-coding-plan/glm-5"

OPENCODE_USAGE_TOTAL_KEYS = ("totalTokens", "total_tokens", "total")
OPENCODE_USAGE_INPUT_KEYS = (
    "inputTokens",
    "input_tokens",
    "promptTokens",
    "prompt_tokens",
)
OPENCODE_USAGE_CACHED_KEYS = (
    "cachedInputTokens",
    "cached_input_tokens",
    "cachedTokens",
    "cached_tokens",
)
OPENCODE_USAGE_OUTPUT_KEYS = (
    "outputTokens",
    "output_tokens",
    "completionTokens",
    "completion_tokens",
)
OPENCODE_USAGE_REASONING_KEYS = (
    "reasoningTokens",
    "reasoning_tokens",
    "reasoningOutputTokens",
    "reasoning_output_tokens",
)
OPENCODE_CONTEXT_WINDOW_KEYS = (
    "modelContextWindow",
    "contextWindow",
    "context_window",
    "contextWindowSize",
    "context_window_size",
    "contextLength",
    "context_length",
    "maxTokens",
    "max_tokens",
)
OPENCODE_MODEL_CONTEXT_KEYS = ("context",) + OPENCODE_CONTEXT_WINDOW_KEYS

__all__ = [
    "DEFAULT_TICKET_MODEL",
    "OPENCODE_USAGE_TOTAL_KEYS",
    "OPENCODE_USAGE_INPUT_KEYS",
    "OPENCODE_USAGE_CACHED_KEYS",
    "OPENCODE_USAGE_OUTPUT_KEYS",
    "OPENCODE_USAGE_REASONING_KEYS",
    "OPENCODE_CONTEXT_WINDOW_KEYS",
    "OPENCODE_MODEL_CONTEXT_KEYS",
]
