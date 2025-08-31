# app/config.py
from __future__ import annotations

import os
from dotenv import load_dotenv
from pydantic import BaseModel

# Ladda .env lokalt om den finns (Render använder sina egna env vars)
load_dotenv()

class Settings(BaseModel):
    # --- Enda hemligheten som hämtas från miljön ---
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")

    # --- Enkla, tydliga kod-defaults (inte från .env) ---
    realtime_url: str = "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview-2024-12-17"
    transcribe_model: str = "gpt-4o-mini-transcribe"
    input_language: str = "sv"
    commit_interval_ms: int = 150
    add_beta_header: bool = True

    # Behåller str-format (kommaseparerad) för minimal risk i resten av koden
    cors_origins: str = (
        "*.lovable.app,"
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "http://localhost:5173"
    )

    host: str = "0.0.0.0"
    port: int = 8000

settings = Settings()
