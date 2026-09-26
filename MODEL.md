# Model used on the Pi 2

| Item | Value |
| --- | --- |
| Base model | [Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) |
| GGUF source | [bartowski/Qwen_Qwen3.5-0.8B-GGUF](https://huggingface.co/bartowski/Qwen_Qwen3.5-0.8B-GGUF) |
| Quantization | `Q4_K_M` |
| File | `Qwen3.5-0.8B-Q4_K_M.gguf` |
| Size | 579,615,840 bytes (553 MiB) |
| SHA-256 | `fb044e93939a70469c905781334f5de1e6c8b608ced6cbc8c9249bd4127d9526` |
| License | Apache 2.0 |
| Inference runtime | [`llama.cpp`](https://github.com/ggml-org/llama.cpp) at commit `81bc6b83f827df746eb129235488d325c49cae52` |

The setup script downloads this exact model and checks its SHA-256. The repository contains no model weights. Runtime inference uses only local files and loopback HTTP.

The Pi 2 configuration starts with a 2,048-token context, one inference slot, two CPU threads, and a maximum of 256 output tokens. These are conservative resource settings for 1 GB RAM. The API handles text only; this setup does not include a vision projector or speech model.

This is a small experimental assistant. Its summaries and answers can be inaccurate, especially after repeated compaction. Full conversation text remains in the private SQLite database for inspection and backup.
