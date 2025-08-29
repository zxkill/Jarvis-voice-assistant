"""Зрение ассистента."""

from .presence import PresenceDetector
from .face_tracker import FaceTracker
from .idle_scanner import IdleScanner

__all__ = ["PresenceDetector", "FaceTracker", "IdleScanner"]
