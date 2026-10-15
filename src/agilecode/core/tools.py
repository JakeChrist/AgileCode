"""Compatibility import for the single runtime tool implementation."""
import sys
from agilecode.runtime import tools as _canonical

sys.modules[__name__] = _canonical
