"""Native Hermes entry. Registration does not open storage or start services."""
from .urgency_engine.runtime import register

__all__ = ['register']
