"""Single-worker startup for local development and Render."""

import os

import uvicorn
from dotenv import load_dotenv
from pathlib import Path


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    port = int(os.getenv("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, workers=1)


if __name__ == "__main__":
    main()
