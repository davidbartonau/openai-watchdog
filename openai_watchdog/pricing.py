"""Hardcoded OpenAI model pricing.

All token prices are in USD per 1 million tokens.
Image prices are per image (or per second for video).
Tool prices are per unit as noted.

Source: https://openai.com/api/pricing/ (as of 2026-02)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TokenPricing:
    """Per-1M-token pricing for a text/chat model."""
    input: float
    output: float
    cached_input: Optional[float] = None


@dataclass(frozen=True)
class ImageTokenPricing:
    """Per-1M-token pricing for image tokens."""
    input: float
    output: float
    cached_input: Optional[float] = None


@dataclass(frozen=True)
class AudioTokenPricing:
    """Per-1M-token pricing for audio tokens."""
    input: float
    output: float
    cached_input: Optional[float] = None


# ---------------------------------------------------------------------------
# Text / Chat completion models  (USD per 1M tokens)
# ---------------------------------------------------------------------------
TEXT_PRICING: dict[str, TokenPricing] = {
    # GPT-5.x family
    "gpt-5.2":                     TokenPricing(1.75, 14.00, 0.175),
    "gpt-5.1":                     TokenPricing(1.25, 10.00, 0.125),
    "gpt-5":                       TokenPricing(1.25, 10.00, 0.125),
    "gpt-5-mini":                  TokenPricing(0.25, 2.00, 0.025),
    "gpt-5-nano":                  TokenPricing(0.05, 0.40, 0.005),
    "gpt-5.2-chat-latest":         TokenPricing(1.75, 14.00, 0.175),
    "gpt-5.1-chat-latest":         TokenPricing(1.25, 10.00, 0.125),
    "gpt-5-chat-latest":           TokenPricing(1.25, 10.00, 0.125),
    "gpt-5.2-codex":               TokenPricing(1.75, 14.00, 0.175),
    "gpt-5.1-codex-max":           TokenPricing(1.25, 10.00, 0.125),
    "gpt-5.1-codex":               TokenPricing(1.25, 10.00, 0.125),
    "gpt-5-codex":                 TokenPricing(1.25, 10.00, 0.125),
    "gpt-5.1-codex-mini":          TokenPricing(0.25, 2.00, 0.025),
    "codex-mini-latest":           TokenPricing(1.50, 6.00, 0.375),
    "gpt-5.2-pro":                 TokenPricing(21.00, 168.00),
    "gpt-5-pro":                   TokenPricing(15.00, 120.00),
    # GPT-4.1 family
    "gpt-4.1":                     TokenPricing(2.00, 8.00, 0.50),
    "gpt-4.1-mini":                TokenPricing(0.40, 1.60, 0.10),
    "gpt-4.1-nano":                TokenPricing(0.10, 0.40, 0.025),
    # GPT-4o family
    "gpt-4o":                      TokenPricing(2.50, 10.00, 1.25),
    "gpt-4o-2024-05-13":           TokenPricing(5.00, 15.00),
    "gpt-4o-mini":                 TokenPricing(0.15, 0.60, 0.075),
    # Realtime (text tokens)
    "gpt-realtime":                TokenPricing(4.00, 16.00, 0.40),
    "gpt-realtime-mini":           TokenPricing(0.60, 2.40, 0.06),
    "gpt-4o-realtime-preview":     TokenPricing(5.00, 20.00, 2.50),
    "gpt-4o-mini-realtime-preview": TokenPricing(0.60, 2.40, 0.30),
    # Audio-capable (text tokens)
    "gpt-audio":                   TokenPricing(2.50, 10.00),
    "gpt-audio-mini":              TokenPricing(0.60, 2.40),
    "gpt-4o-audio-preview":        TokenPricing(2.50, 10.00),
    "gpt-4o-mini-audio-preview":   TokenPricing(0.15, 0.60),
    # o-series reasoning
    "o1":                          TokenPricing(15.00, 60.00, 7.50),
    "o1-pro":                      TokenPricing(150.00, 600.00),
    "o3-pro":                      TokenPricing(20.00, 80.00),
    "o3":                          TokenPricing(2.00, 8.00, 0.50),
    "o3-deep-research":            TokenPricing(10.00, 40.00, 2.50),
    "o4-mini":                     TokenPricing(1.10, 4.40, 0.275),
    "o4-mini-deep-research":       TokenPricing(2.00, 8.00, 0.50),
    "o3-mini":                     TokenPricing(1.10, 4.40, 0.55),
    "o1-mini":                     TokenPricing(1.10, 4.40, 0.55),
    # Search models (text tokens)
    "gpt-5-search-api":            TokenPricing(1.25, 10.00, 0.125),
    "gpt-4o-mini-search-preview":  TokenPricing(0.15, 0.60),
    "gpt-4o-search-preview":       TokenPricing(2.50, 10.00),
    # Other
    "computer-use-preview":        TokenPricing(3.00, 12.00),
    # Image generation models (text token component)
    "gpt-image-1.5":               TokenPricing(5.00, 10.00, 1.25),
    "chatgpt-image-latest":        TokenPricing(5.00, 10.00, 1.25),
    "gpt-image-1":                 TokenPricing(5.00, 0.0, 1.25),
    "gpt-image-1-mini":            TokenPricing(2.00, 0.0, 0.20),
}

# ---------------------------------------------------------------------------
# Image token pricing  (USD per 1M tokens)
# ---------------------------------------------------------------------------
IMAGE_TOKEN_PRICING: dict[str, ImageTokenPricing] = {
    "gpt-image-1.5":        ImageTokenPricing(8.00, 32.00, 2.00),
    "chatgpt-image-latest": ImageTokenPricing(8.00, 32.00, 2.00),
    "gpt-image-1":          ImageTokenPricing(10.00, 40.00, 2.50),
    "gpt-image-1-mini":     ImageTokenPricing(2.50, 8.00, 0.25),
    "gpt-realtime":         ImageTokenPricing(5.00, 0.0, 0.50),
    "gpt-realtime-mini":    ImageTokenPricing(0.80, 0.0, 0.08),
}

# ---------------------------------------------------------------------------
# Audio token pricing  (USD per 1M tokens)
# ---------------------------------------------------------------------------
AUDIO_TOKEN_PRICING: dict[str, AudioTokenPricing] = {
    "gpt-realtime":                AudioTokenPricing(32.00, 64.00, 0.40),
    "gpt-realtime-mini":           AudioTokenPricing(10.00, 20.00, 0.30),
    "gpt-4o-realtime-preview":     AudioTokenPricing(40.00, 80.00, 2.50),
    "gpt-4o-mini-realtime-preview": AudioTokenPricing(10.00, 20.00, 0.30),
    "gpt-audio":                   AudioTokenPricing(32.00, 64.00),
    "gpt-audio-mini":              AudioTokenPricing(10.00, 20.00),
    "gpt-4o-audio-preview":        AudioTokenPricing(40.00, 80.00),
    "gpt-4o-mini-audio-preview":   AudioTokenPricing(10.00, 20.00),
}

# ---------------------------------------------------------------------------
# Embedding models  (USD per 1M tokens — input only)
# ---------------------------------------------------------------------------
EMBEDDING_PRICING: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}

# ---------------------------------------------------------------------------
# Video pricing  (USD per second)
# ---------------------------------------------------------------------------
VIDEO_PER_SECOND: dict[str, dict[str, float]] = {
    "sora-2":     {"720p": 0.10},
    "sora-2-pro": {"720p": 0.30, "1080p": 0.50},
}

# ---------------------------------------------------------------------------
# Built-in tool pricing
# ---------------------------------------------------------------------------
CODE_INTERPRETER_PER_CONTAINER: dict[str, float] = {
    "1GB":  0.03,
    "4GB":  0.12,
    "16GB": 0.48,
    "64GB": 1.92,
}

FILE_SEARCH_STORAGE_PER_GB_DAY: float = 0.10
FILE_SEARCH_CALL_PER_1K: float = 2.50

WEB_SEARCH_PER_1K_CALLS: float = 10.00
WEB_SEARCH_PREVIEW_NON_REASONING_PER_1K: float = 25.00


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def estimate_text_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> Optional[float]:
    """Return estimated USD cost for a text/completion call, or None if model unknown."""
    pricing = _resolve_text_pricing(model)
    if pricing is None:
        return None
    uncached_input = input_tokens - cached_input_tokens
    cost = (uncached_input * pricing.input / 1_000_000)
    if cached_input_tokens and pricing.cached_input is not None:
        cost += (cached_input_tokens * pricing.cached_input / 1_000_000)
    elif cached_input_tokens:
        cost += (cached_input_tokens * pricing.input / 1_000_000)
    cost += (output_tokens * pricing.output / 1_000_000)
    return cost


def estimate_embedding_cost(model: str, input_tokens: int) -> Optional[float]:
    """Return estimated USD cost for an embedding call."""
    rate = _resolve_embedding_pricing(model)
    if rate is None:
        return None
    return input_tokens * rate / 1_000_000


def _resolve_text_pricing(model: str) -> Optional[TokenPricing]:
    """Look up pricing, trying exact match then prefix match."""
    if model in TEXT_PRICING:
        return TEXT_PRICING[model]
    # Try stripping date suffix, e.g. "gpt-4o-mini-2024-07-18" -> "gpt-4o-mini"
    for key in sorted(TEXT_PRICING, key=len, reverse=True):
        if model.startswith(key):
            return TEXT_PRICING[key]
    return None


def _resolve_embedding_pricing(model: str) -> Optional[float]:
    if model in EMBEDDING_PRICING:
        return EMBEDDING_PRICING[model]
    for key in sorted(EMBEDDING_PRICING, key=len, reverse=True):
        if model.startswith(key):
            return EMBEDDING_PRICING[key]
    return None
