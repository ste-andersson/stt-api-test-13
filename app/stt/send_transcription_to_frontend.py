async def send_transcription_to_frontend(ws, result: dict, send_json: bool, buffers):
    """
    Skicka transkriptionstext till frontend.
    Flyttad från stt_ws.py för att separera concerns.
    
    Args:
        ws: WebSocket-anslutning till frontend
        result: Resultat från process_realtime_event
        send_json: Om JSON-format ska användas
        buffers: Debug store buffers för logging
    """
    if result["type"] == "transcript" and result["delta"]:
        # Logga för debug
        buffers.openai_text.append(result["text"])
        
        if send_json:
            await ws.send_json({
                "type": "stt.final" if result["is_final"] else "stt.partial",
                "text": result["text"]
            })
        else:
            await ws.send_text(result["delta"])  # fallback: ren text
            
        # Logga delta för debug
        buffers.frontend_text.append(result["delta"])
        
        return result["text"]  # Returnera text för att uppdatera last_text
    return None
