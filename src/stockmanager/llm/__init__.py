"""LLM backend adapter layer: the Reasoner and the News Extractor talk to any model through ``LLMBackend``."""

from stockmanager.llm.protocol import ChatResult, LLMAuthError, LLMBackend, LLMError, LLMTimeout

__all__ = ["ChatResult", "LLMAuthError", "LLMBackend", "LLMError", "LLMTimeout"]
