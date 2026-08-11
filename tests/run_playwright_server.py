from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parents[1]
app = FastAPI()
app.mount("/ui", StaticFiles(directory=ROOT / "src" / "eirven_ai" / "web", html=True), name="ui")


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
