"""Shared service exceptions."""


class TranscriptionCancelled(Exception):
    """
    Raised when a transcription is stopped because its job was cancelled.

    Distinct from a transcription *failure*: a cancellation must never trigger
    the faster-whisper fallback, which would quietly restart hours of work the
    user just asked to stop.
    """
