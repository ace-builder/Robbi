"""Compatibility import for the shared configuration store."""
import sys
from .configuration import store
sys.modules[__name__] = store
