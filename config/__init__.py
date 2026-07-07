"""
config/__init__.py — Configuration package initializer.

Exposes the policy dictionary for clean imports:
    from config import policy
"""

from config.policy import policy

__all__ = ["policy"]
