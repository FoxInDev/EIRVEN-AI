# Third-party notices — EIRVEN AI 1.7.3

EIRVEN source code is distributed under the MIT License. Runtime models and dependencies remain governed by their upstream licenses.

- Local Ollama models — downloaded by the installer from their upstream distribution; not embedded in this source archive.
- Claude Code CLI — installed separately from Anthropic's official distribution and used as a local orchestration client pointed at Ollama; the proprietary Claude model weights are not bundled or represented as local.
- GigaAM v3 / ONNX-ASR — downloaded/loaded separately for Russian speech recognition.
- Silero Models V5.5 RU — fully local Russian TTS downloaded separately by the installer; the public EIRVEN voice uses its `baya` speaker at 48 kHz.
- Qwen3-TTS / qwen-tts — optional experimental expressive speech synthesis path; not downloaded or selected by default.
- OpenCV — local image/desktop vision utility used by supported visual workflows.
- FFmpeg / imageio-ffmpeg — локальный движок монтажа, перекодирования и проверки видео; лицензия конкретной FFmpeg-сборки зависит от включённых кодеков.
- pypdf, python-docx, openpyxl, python-pptx — local parsing of attached documents.
- MSS — full desktop screen capture dependency.
- DDGS — keyless public web-search client; individual search provider terms may also apply.
- Playwright, FastAPI, Uvicorn, HTTPX, Pillow, psutil, PyAutoGUI, pywinauto and other dependencies — their respective upstream licenses.
- QR Code Generator for JavaScript 1.4.4 by Kazuhiko Arase — bundled under the MIT License to create the offline mobile-download QR in the local settings page.
- ComfyUI v0.29.0 — downloaded on demand into `data/photo_engine`; upstream source is GPL-3.0 and is not embedded in the Windows ZIP.
- Stable Diffusion XL Base 1.0 (`sd_xl_base_1.0.safetensors`) — downloaded on demand from Stability AI at a pinned revision; expected SHA-256 `31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b`; model terms are CreativeML Open RAIL++-M / OpenRAIL++ as published upstream.
- Animagine XL 4.0 Opt (`animagine-xl-4.0-opt.safetensors`) — downloaded on demand from Cagliostro Labs at a pinned revision; expected SHA-256 `6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac`; model license is OpenRAIL++ as published upstream.

Jarvis source supplied by the user was used only as an architecture/visual reference. Its implementation is not copied or distributed inside EIRVEN.

Before bundling third-party model files directly into a commercial binary, re-check exact model and voice licenses for the exact versions being redistributed. The default EIRVEN installer downloads these assets from upstream instead of embedding them into this source archive.


## Chatterbox TTS

Optional developer-only multilingual speech synthesis via `chatterbox-tts` (Resemble AI, MIT). It is not downloaded or selected by the supported public installation.

## Edge TTS

Legacy compatibility code can recognize an `edge-tts` installation, but the supported public installation neither downloads nor selects this network-backed path.
