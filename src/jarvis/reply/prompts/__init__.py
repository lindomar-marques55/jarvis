"""
Prompt system for model-size-aware response generation.

This module provides model-size-specific prompt variations to improve
tool usage accuracy across different LLM sizes.
"""

from .model_variants import ModelSize, detect_model_size, get_system_prompts, supports_native_tool_calling
from .system import PromptComponents, ASR_NOTE, INFERENCE_GUIDANCE, VOICE_STYLE

__all__ = [
    "ModelSize",
    "detect_model_size",
    "supports_native_tool_calling",
    "get_system_prompts",
    "PromptComponents",
    "ASR_NOTE",
    "INFERENCE_GUIDANCE",
    "VOICE_STYLE",
]
