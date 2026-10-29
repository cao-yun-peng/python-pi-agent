"""First-party model provider adapters."""

from .deepseek import DeepSeekConfig, DeepSeekProvider, ReasoningEffort, ThinkingMode
from .glm import (
    GLMConfig,
    GLMProvider,
    GLMReasoningEffort,
    GLMThinkingMode,
)
from .kimi import (
    KimiConfig,
    KimiProvider,
    KimiReasoningEffort,
    KimiThinkingKeep,
    KimiThinkingMode,
)
from .timicc_responses import (
    TimiccReasoningEffort,
    TimiccReasoningSummary,
    TimiccResponsesConfig,
    TimiccResponsesProvider,
)

__all__ = [
    "DeepSeekConfig",
    "DeepSeekProvider",
    "GLMConfig",
    "GLMProvider",
    "GLMReasoningEffort",
    "GLMThinkingMode",
    "KimiConfig",
    "KimiProvider",
    "KimiReasoningEffort",
    "KimiThinkingKeep",
    "KimiThinkingMode",
    "ReasoningEffort",
    "ThinkingMode",
    "TimiccReasoningEffort",
    "TimiccReasoningSummary",
    "TimiccResponsesConfig",
    "TimiccResponsesProvider",
]
