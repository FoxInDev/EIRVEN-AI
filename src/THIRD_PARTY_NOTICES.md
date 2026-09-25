# Third-party notices

EIRVEN integrates open-source/runtime components under their respective licenses. Desktop model weights are not bundled in the Windows installer: Ollama downloads DeepSeek-Coder-V2 and Qwen3-VL during setup, and their upstream terms remain in force.

The Android package uses llama.cpp (MIT), PyTorch Android Lite (BSD-style), JTransforms (BSD-style), WebLLM (Apache-2.0), and an official Silero v5 Russian Baya model (Apache-2.0). The corresponding license texts are included in the Android project and APK assets. Qwen3-4B-Instruct-2507 is licensed under Apache-2.0; the Q4_K_M GGUF conversion is downloaded on first use from the pinned Bartowski revision and remains subject to the upstream terms. Exact provenance and checksum are recorded in `QWEN_MODEL_NOTICE.txt` inside the APK.

The desktop web interface bundles Google Noto Color Emoji v2.051 for consistent
cross-platform color emoji. The unmodified font uses the SIL Open Font License 1.1;
its license is included as `web/NotoColorEmoji-OFL.txt`. The bundled file's
SHA-256 is `72A635CB3D2F3524C51620CDDE406B217204E8A6A06C6A096FF8ED4B5FD6E27B`.
It is an open Noto design and is not Apple Color Emoji or an extraction of Apple assets.
The pinned upstream URL is only a secondary recovery source; normal desktop rendering
uses the bundled file and works offline.
