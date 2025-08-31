from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional

import websockets

logger = logging.getLogger(__name__)

JsonDict = dict[str, object]

class OpenAIRealtimeClient:
    """Minimal WebSocket-klient mot OpenAI/Azure Realtime.

    Den här klienten skickar PCM16-ljud som base64 i events av typen
    `input_audio_buffer.append` och kan manuellt commit:a bufferten med
    `input_audio_buffer.commit` för att trigga transkribering.
    """
    def __init__(
        self,
        url: str,
        api_key: str,
        transcribe_model: str,
        language: str = "sv",
        add_beta_header: bool = True,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.transcribe_model = transcribe_model
        self.language = language
        self.add_beta_header = add_beta_header
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()

    async def connect(self) -> None:
        # Headers: Azure vs OpenAI
        headers = []
        if ".openai.azure.com" in self.url:
            headers.append(("api-key", self.api_key))
        else:
            headers.append(("Authorization", f"Bearer {self.api_key}"))
            if self.add_beta_header:
                headers.append(("OpenAI-Beta", "realtime=v1"))

        # WS connect
        self.ws = await websockets.connect(
            self.url,
            extra_headers=headers,
            max_size=32 * 1024 * 1024,
        )
        self._connected.set()

        # Konfigurera sessionen (pcm16 + transcribe-modell + språk)
        session_update = {
            "type": "session.update",
            "session": {
                "modalities": ["text"],  # räcker för STT
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1",  # samma som repo 2 för kompatibilitet
                    "language": self.language,
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                    "create_response": False,  # vi vill bara STT
                    "interrupt_response": True
                },
            },
        }
        await self.ws.send(json.dumps(session_update))

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        """Skicka en ljudchunk (PCM16) som base64 till input_audio_buffer.append"""
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        b64 = base64.b64encode(pcm_bytes).decode("ascii")
        msg = {"type": "input_audio_buffer.append", "audio": b64}
        await self.ws.send(json.dumps(msg))

    async def commit(self) -> None:
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        await self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def recv_loop(self, on_event: Callable[[JsonDict], Awaitable[None]]) -> None:
        if not self.ws:
            raise RuntimeError("WebSocket not connected")
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                    await on_event(data)
                except Exception as e:
                    logger.warning("Fel vid hantering av Realtime event: %s", e)
                    continue
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosedError as e:
            logger.info("Realtime WebSocket stängd: %s", e)
        except Exception as e:
            logger.exception("Realtime recv_loop fel: %s", e)
