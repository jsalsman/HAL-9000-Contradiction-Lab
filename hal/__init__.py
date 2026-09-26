"""HAL 9000 Contradiction Lab: a blinded, resumable, cumulative behavior test."""

from hal.protocol import PROTOCOL_VERSION

# Exported so callers can tag data without importing transport modules.
__all__ = ["PROTOCOL_VERSION"]
