from __future__ import annotations

import dataclasses
from typing import Iterable, Optional, Protocol


@dataclasses.dataclass
class SpeechSessionMetadata:
    """Context passed to providers to keep sessions auditable without leaking audio."""

    session_id: str
    provider: str
    latency_mode: str
    language: Optional[str] = None
    client: Optional[str] = None  # e.g., "web", "tui"
    user_agent: Optional[str] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None


@dataclasses.dataclass
class AudioChunk:
    """
    Representation of an audio chunk pushed into the provider.

    Only lightweight metadata is stored to avoid persisting raw audio outside memory.
    """

    data: bytes
    sample_rate: int
    start_ms: int
    end_ms: int
    sequence: int


@dataclasses.dataclass
class TranscriptionEvent:
    text: str
    is_final: bool
    latency_ms: Optional[int] = None
    error: Optional[str] = None


class TranscriptionStream(Protocol):
    """Streaming handle for a single push-to-talk session."""

    def send_chunk(self, chunk: AudioChunk) -> Iterable[TranscriptionEvent]: ...

    def flush_final(self) -> Iterable[TranscriptionEvent]:
        """Send end-of-input and return any remaining events."""
        ...

    def abort(self, reason: Optional[str] = None) -> None:
        """Abort the stream; providers should clean up remote resources."""
        ...


class SpeechProvider(Protocol):
    """Provider abstraction so TUI and web can share the same transcription backend."""

    name: str
    supports_streaming: bool

    def start_stream(self, session: SpeechSessionMetadata) -> TranscriptionStream:
        """Begin a streaming session for a given request."""
        ...
