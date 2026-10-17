"""Known model IDs per provider — October 2026 snapshot.

Reserved for the future /llmtest Telegram command: given a caught
gateway, it would test the gateway against real model identifiers per
provider (a weak gateway that answers a probe will 404 on unknown
models and 200/429 on ones it actually serves, which identifies both
the model list it exposes and which provider it is mimicking).

IDs are OpenAI-compatible ``model`` values unless noted. ``base_url`` is
the provider's canonical OpenAI-compatible endpoint, for reference /
direct testing. Refresh this list quarterly — providers rotate fast.

Confidence:
  HIGH   — confirmed on official docs / model pages at snapshot time
  MEDIUM — widely reported, not double-checked against official docs
"""

PROVIDERS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api": "openai",
        "flagship": "gpt-6-astra",
        "fast": "gpt-6-luna",
        "models": [
            "gpt-6-astra",      # flagship
            "gpt-6-sol",
            "gpt-6.1-sol",
            "gpt-6-luna",       # fast
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5-pro",
            "gpt-5.5",
            "gpt-5.4-pro",
            "gpt-5.4-mini",
            "gpt-5.4",
            "gpt-5.2-pro",
            "gpt-5.2",
            "gpt-5-pro",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-5",
            "gpt-4o",           # deprecated but still serving
            "gpt-4o-mini",      # deprecated but still serving
        ],
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api": "openai",
        "flagship": "gemini-4-argon",
        "fast": "gemini-3.8-flash",
        "models": [
            "gemini-4-argon",           # flagship
            "gemini-3.1-pro-preview",
            "gemini-3-pro-preview",
            "gemini-3.8-flash",         # fast, stable
            "gemini-3.6-flash",
            "gemini-3.1-flash-lite",
            "gemini-3-flash-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
    },
    "anthropic": {
        # native Messages API — NOT a plain OpenAI-compatible endpoint;
        # a future /llmtest should POST /v1/messages with the anthropic
        # schema (x-api-key header, anthropic-version) rather than
        # /chat/completions
        "base_url": "https://api.anthropic.com/v1",
        "api": "anthropic",
        "flagship": "claude-opus-5-5",
        "fast": "claude-haiku-5-5",
        "models": [
            "claude-opus-5-5",           # flagship (5.x gen drops the date)
            "claude-sonnet-5-5",
            "claude-haiku-5-5",          # fast
            "claude-opus-4-5-20251101",  # 4.5 gen keeps dated IDs
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
        ],
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api": "openai",
        "flagship": "deepseek-v4-pro",
        "fast": "deepseek-flash",
        "models": [
            "deepseek-v4-pro",      # flagship
            "deepseek-flash",       # fast
            "deepseek-chat",        # legacy alias
            "deepseek-reasoner",    # legacy
        ],
    },
    "moonshot": {
        # CN mirror: https://api.moonshot.cn/v1
        "base_url": "https://api.moonshot.ai/v1",
        "api": "openai",
        "flagship": "kimi-k3",
        "fast": "kimi-k2.7-code-highspeed",
        "models": [
            "kimi-k3",                       # flagship
            "kimi-k2.7-code-highspeed",      # fast
            "kimi-k2.7-code",
            "kimi-k2.6",
            "moonshot-v1-128k",              # retired but sometimes still alive
            "moonshot-v1-32k",
            "moonshot-v1-8k",
        ],
    },
    "glm": {
        # Zhipu. CN: https://open.bigmodel.cn/api/paas/v4
        # IDs are lowercase, like most providers (MiniMax's mixed case is
        # the exception)
        "base_url": "https://api.z.ai/api/paas/v4",
        "api": "openai",
        "flagship": "glm-5.3",
        "fast": "glm-5.3-flash",
        "models": [
            "glm-5.3",              # flagship
            "glm-5.3-flash",        # fast
            "glm-5.2",
            "glm-5.1",
            "glm-5-turbo",
            "glm-5",
            "glm-4.7-flash",
            "glm-4.7",
            "glm-4.6",
        ],
    },
    "minimax": {
        # CN: https://api.minimaxi.com/v1
        # IDs are mixed case (MiniMax-M3), unlike most providers
        "base_url": "https://api.minimax.io/v1",
        "api": "openai",
        "flagship": "MiniMax-M3",
        "fast": "MiniMax-M3.1-Flash-Preview",
        "models": [
            "MiniMax-M3",                  # flagship
            "MiniMax-M3.1-Flash-Preview",  # fast (preview)
            "MiniMax-M2.7-highspeed",
            "MiniMax-M2.7",
            "MiniMax-M2.5",
            "MiniMax-M2.1",
            "MiniMax-M2",
            "MiniMax-M1",
            "MiniMax-Text-01",
        ],
    },
    "qwen": {
        # Aliyun DashScope. CN: https://dashscope.aliyuncs.com/compatible-mode/v1
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "api": "openai",
        "flagship": "qwen3.8-max",
        "fast": "qwen3.8-flash",
        "models": [
            "qwen3.8-max",      # flagship
            "qwen3.7-max",
            "qwen3.8-flash",    # fast
            "qwen3.7-plus",
            "qwen3.6-plus",
            "qwen3.6-flash",
            "qwen-flash",
            "qwen-max",         # alias
            "qwen-plus",        # alias
            "qwen-turbo",       # alias
        ],
    },
}

SNAPSHOT = "2026-10"


def providers():
    """All provider keys, flagship-first in stable order."""
    return list(PROVIDERS)


def candidate_models(provider):
    """Ordered model IDs for one provider: flagship, fast, then the rest
    in listed order, deduped. Unknown provider -> [] ."""
    info = PROVIDERS.get(provider)
    if not info:
        return []
    out = []
    for key in (info.get("flagship"), info.get("fast")):
        if key and key not in out:
            out.append(key)
    for m in info.get("models", []):
        if m not in out:
            out.append(m)
    return out
