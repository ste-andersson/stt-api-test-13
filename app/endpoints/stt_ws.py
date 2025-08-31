import asyncio
import logging
import os
import time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from starlette.websockets import WebSocketState

from ..config import settings
from ..debug_store import store
from ..stt.realtime_client import OpenAIRealtimeClient

log = logging.getLogger("stt")

router = APIRouter()

@router.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()
    
    # A är default: JSON, B som fallback: ren text
    mode = (ws.query_params.get("mode") or os.getenv("WS_DEFAULT_MODE", "json")).lower()
    send_json = (mode == "json")
    
    session_id = store.new_session()
    
    # Skicka "ready" meddelande för kompatibilitet med frontend
    if send_json:
        await ws.send_json({
            "type": "ready",
            "audio_in": {"encoding": "pcm16", "sample_rate_hz": 16000, "channels": 1},
            "audio_out": {"mimetype": "audio/mpeg"},
        })
        await ws.send_json({"type": "session.started", "session_id": session_id})

    # Setup klient mot OpenAI/Azure Realtime
    rt = OpenAIRealtimeClient(
        url=settings.realtime_url,
        api_key=settings.openai_api_key,
        transcribe_model=settings.transcribe_model,
        language=settings.input_language,
        add_beta_header=settings.add_beta_header,
    )
    
    try:
        await rt.connect()
    except Exception as e:
        if send_json and ws.client_state == WebSocketState.CONNECTED:
            await ws.send_json({"type": "error", "reason": "realtime_connect_failed", "detail": str(e)})
        return
    else:
        if send_json and ws.client_state == WebSocketState.CONNECTED:
            await ws.send_json({"type": "info", "msg": "realtime_connected"})

    buffers = store.get_or_create(session_id)

    # Hålla senaste text för enkel diff
    last_text = ""
    
    # Flagga för att veta om vi har skickat ljud
    has_audio = False
    last_audio_time = 0  # Timestamp för senaste ljud

    # Task: läs events från Realtime och skicka deltas till frontend
    async def on_rt_event(evt: dict):
        nonlocal last_text
        t = evt.get("type")

        # (A) logga alltid eventtyp för felsökning (/debug/rt-events om ni har det)
        try:
            buffers.rt_events.append(str(t))
        except Exception:
            pass

        # (B) bubbla upp error/info till klienten (syns i browser-konsol)
        if t == "error":
            detail = evt.get("error", evt)
            if send_json and ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json({"type": "error", "reason": "realtime_error", "detail": detail})
            return
        if t == "session.updated":
            if send_json and ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json({"type": "info", "msg": "realtime_connected_and_configured"})
            return

        # (C) försök extrahera transcript från flera varianter
        transcript = None

        # 1) Klassisk Realtime-transkript (som repo 2 använder) - whisper-1 + server VAD
        if t == "conversation.item.input_audio_transcription.completed":
            transcript = (
                evt.get("transcript")
                or evt.get("item", {}).get("content", [{}])[0].get("transcript")
            )

        # 2) Alternativ nomenklatur: response.audio_transcript.delta/completed
        if not transcript and t in ("response.audio_transcript.delta", "response.audio_transcript.completed"):
            transcript = evt.get("transcript") or evt.get("text") or evt.get("delta")

        # 3) Sista fallback: response.output_text.delta (text-delning)
        if not transcript and t == "response.output_text.delta":
            delta_txt = evt.get("delta")
            if isinstance(delta_txt, str):
                transcript = (last_text or "") + delta_txt

        if not isinstance(transcript, str) or not transcript:
            return

        # (D) beräkna delta och skicka till frontend
        delta = transcript[len(last_text):] if transcript.startswith(last_text) else transcript

        if delta and ws.client_state == WebSocketState.CONNECTED:
            buffers.openai_text.append(transcript)
            
            # Bestäm om detta är final eller partial transcript
            is_final = t in (
                "conversation.item.input_audio_transcription.completed",
                "response.audio_transcript.completed",
            )
            
            if send_json:
                await ws.send_json({
                    "type": "stt.final" if is_final else "stt.partial",
                    "text": transcript
                })
            else:
                await ws.send_text(delta)  # fallback: ren text
            buffers.frontend_text.append(delta)
            last_text = transcript

    rt_recv_task = asyncio.create_task(rt.recv_loop(on_rt_event))

    # Periodisk commit för att få löpande partials
    async def commit_loop():
        try:
            while True:
                await asyncio.sleep(max(0.001, settings.commit_interval_ms / 1000))
                # Bara committa om vi har skickat ljud
                if has_audio:
                    # Kontrollera om det har gått för lång tid sedan senaste ljudet
                    import time
                    if time.time() - last_audio_time > 2.0:  # 2 sekunder timeout
                        has_audio = False
                        log.debug("Timeout - återställer has_audio flaggan")
                        continue
                    
                    try:
                        await rt.commit()
                    except Exception as e:
                        # Hantera "buffer too small" fel mer elegant
                        if "buffer too small" in str(e) or "input_audio_buffer_commit_empty" in str(e):
                            log.debug("Buffer för liten, väntar på mer ljud: %s", e)
                            # Om buffern är helt tom, återställ has_audio flaggan
                            if "0.00ms of audio" in str(e):
                                has_audio = False
                            continue  # Fortsätt loopen istället för att bryta
                        else:
                            log.warning("Commit fel: %s", e)
                            break
        except asyncio.CancelledError:
            pass

    commit_task = asyncio.create_task(commit_loop())

    try:
        while ws.client_state == WebSocketState.CONNECTED:
            try:
                msg = await ws.receive()

                if "bytes" in msg and msg["bytes"] is not None:
                    chunk = msg["bytes"]
                    buffers.frontend_chunks.append(len(chunk))
                    try:
                        await rt.send_audio_chunk(chunk)
                        buffers.openai_chunks.append(len(chunk))
                        has_audio = True  # Markera att vi har skickat ljud
                        import time
                        last_audio_time = time.time()  # Uppdatera timestamp
                    except Exception as e:
                        log.error("Fel när chunk skickades till Realtime: %s", e)
                        break
                elif "text" in msg and msg["text"] is not None:
                    # Tillåt ping/ctrl meddelanden som sträng
                    if msg["text"] == "ping":
                        await ws.send_text("pong")
                    else:
                        # ignoreras
                        pass
                else:
                    # okänt format
                    pass

            except WebSocketDisconnect:
                log.info("WebSocket stängd: %s", session_id)
                break
            except RuntimeError as e:
                log.info("WS disconnect during receive(): %s", e)
                break
            except Exception as e:
                log.error("WebSocket fel: %s", e)
                break
    finally:
        commit_task.cancel()
        rt_recv_task.cancel()
        try:
            await rt.close()
        except Exception:
            pass
        try:
            await asyncio.gather(commit_task, rt_recv_task, return_exceptions=True)
        except Exception:
            pass
        # Stäng WebSocket bara om den inte redan är stängd
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except Exception:
                pass


# Alias route för /ws som använder samma logik som /ws/transcribe
@router.websocket("/ws")
async def ws_alias(ws: WebSocket):
    # Återanvänd exakt samma logik som i ws_transcribe
    await ws_transcribe(ws)
