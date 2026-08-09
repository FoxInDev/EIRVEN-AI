# Third-party notices — EIRVEN AI 1.2.2

EIRVEN source code is distributed under the MIT License. Runtime models and dependencies remain governed by their upstream licenses.

- Local Ollama models — downloaded by the installer from their upstream distribution; not embedded in this source archive.
- GigaAM v3 / ONNX-ASR — downloaded/loaded separately for Russian speech recognition.
- Silero TTS — fully local low-latency Russian fallback speech synthesis model downloaded separately by the installer; upstream model/code terms apply.
- Qwen3-TTS / qwen-tts — optional experimental expressive speech synthesis path; not downloaded or selected by default.
- piper-onnx / ONNX Runtime — local ONNX TTS runtime, integrity validation and emergency fallback.
- Russian Piper voice `irina` — downloaded separately from the upstream Piper voice repository as the local fallback for the public Baya voice; exact model/dataset terms remain those published upstream.
- OpenCV — local image/desktop vision utility used by supported visual workflows.
- pypdf, python-docx, openpyxl, python-pptx — local parsing of attached documents.
- MSS — full desktop screen capture dependency.
- DDGS — keyless public web-search client; individual search provider terms may also apply.
- Playwright, FastAPI, Uvicorn, HTTPX, Pillow, psutil, PyAutoGUI, pywinauto and other dependencies — their respective upstream licenses.

Jarvis source supplied by the user was used only as an architecture/visual reference. Its implementation is not copied or distributed inside EIRVEN.

Before bundling third-party model files directly into a commercial binary, re-check exact model and voice licenses for the exact versions being redistributed. The default EIRVEN installer downloads these assets from upstream instead of embedding them into this source archive.


## Chatterbox TTS

Optional local multilingual speech synthesis via `chatterbox-tts` (Resemble AI, MIT). r7 installs it only on supported CUDA systems; no hosted API is required.

## Edge TTS

Optional natural Russian neural speech through the Microsoft Edge Read Aloud service using the open-source `edge-tts` client. No API key is required; this path is network-backed and falls back to local TTS when unavailable.
