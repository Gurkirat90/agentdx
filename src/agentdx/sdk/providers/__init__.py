"""Provider shims targeting the OpenAI-compatible surface, not a vendor SDK (PRD §8.5).

Groq ships as the default recording configuration, but the shim is written against
the OpenAI-compatible interface so a model deprecation cannot break the product.
Will contain: openai_compatible.py, groq.py, anthropic.py (P04).
"""
