"""Compatibility imports for the shared configuration operation."""
from agilecode.adapters.config_lock import atomic_json
from agilecode.application.services.configuration import Configuration

__all__ = ["Configuration", "atomic_json"]
