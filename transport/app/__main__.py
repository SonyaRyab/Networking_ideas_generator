"""Запуск: python -m app."""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    uvicorn.run(
        "app.main:app",
        host=os.getenv("TRANSPORT_HOST", "0.0.0.0"),
        port=int(os.getenv("TRANSPORT_PORT", "8080")),
    )
