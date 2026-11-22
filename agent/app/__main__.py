import os

import uvicorn

from .config import Settings

if __name__ == "__main__":
    Settings.from_environment()  # Загрузить .env до чтения порта.
    uvicorn.run(
        "app.main:app",
        host=os.getenv("AGENT_HOST", "0.0.0.0"),
        port=int(os.getenv("AGENT_PORT", "8000")),
        workers=1,  # UUID и фоновые задачи хранятся в памяти одного процесса.
    )
