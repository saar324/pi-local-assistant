# Pi Local Assistant

A small, fully local personal assistant and OpenAI-style chat API for a 1 GB Raspberry Pi 2. It uses a quantized text model through `llama.cpp`, keeps full conversations in a private SQLite database, and summarizes older turns when the 2,048-token working context fills up.

```
Mac / phone --SSH tunnel--> Pi API :8765 --> llama.cpp :8080 --> local GGUF model
                                |
                                +--> private SQLite history on the Pi
```

After the initial software and model downloads, inference and conversation storage work without internet access. The model server and API bind to `127.0.0.1`; use an SSH tunnel to reach them from another device. The API requires a private bearer key.

## What this can do

- Chat in the Pi terminal or call `POST /v1/chat/completions` from an API client.
- Use durable server-side sessions with `POST /api/sessions/{id}/chat` or an optional `session_id` field on chat completions.
- Automatically compact old turns into a short summary while retaining the full transcript in SQLite.
- Limit inference to one request at a time so concurrent calls cannot multiply model memory use.

The active context is finite, and generated summaries can lose details. Full transcripts persist until you delete them or run out of storage. The 0.8B model is useful for experimentation, but it is slow and much less capable than a desktop model. This version handles text and non-streaming requests only; it has no autonomous tools.

**Hermes Agent is not part of the Pi 2 runtime.** Hermes [requires at least 64K context](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart) and its [supported platforms](https://hermes-agent.nousresearch.com/docs/getting-started/platform-support) do not include 32-bit ARMv7. This project supplies a much smaller harness that fits the Pi. Hermes with a larger local model remains an option on a newer 64-bit machine.

## Setup on the Pi

Requirements: Raspberry Pi OS with Python 3.11+, 1 GB RAM, a microSD card with at least 4 GB free, and internet access for the initial download. Swap is helpful during compilation; runtime aims to stay within physical RAM.

```bash
git clone https://github.com/saar324/pi-local-assistant.git
cd pi-local-assistant
bash scripts/setup_pi.sh
```

`setup_pi.sh` builds a pinned `llama.cpp` revision for ARMv7 and downloads a pinned, checksum-verified [Qwen3.5-0.8B Q4_K_M GGUF](https://huggingface.co/bartowski/Qwen_Qwen3.5-0.8B-GGUF). Exact model details are in [MODEL.md](MODEL.md). The model, build tree, and conversation history stay outside this repository. The model is Apache 2.0 licensed; this repository does not redistribute its weights.

Start the model in one terminal:

```bash
bash scripts/run_model.sh
```

Then chat directly in a second terminal:

```bash
python3 pi_assistant.py chat
```

The CLI prints a session ID. List saved conversations with `python3 pi_assistant.py sessions`, then resume one with `python3 pi_assistant.py chat --session-id ID`.

Once the model and API both work, `bash scripts/install_services.sh` installs private systemd services that start on boot. The generated unit files and API key live on the Pi, outside this repository. The model service requests an 820 MiB RAM cap and 300 MiB swap cap by default; override these when installing with `PI_MODEL_MEMORY_MAX`, `PI_MODEL_MEMORY_HIGH`, and `PI_MODEL_SWAP_MAX` if your Pi needs different limits. These limits require the Linux memory cgroup controller. If `cat /sys/fs/cgroup/cgroup.controllers` does not show `memory`, append `cgroup_enable=memory cgroup_memory=1` to the single line in `/boot/firmware/cmdline.txt` (or `/boot/cmdline.txt` on older images), then reboot. Check the services with `systemctl status pi-local-model pi-local-api`.

## Run the API

Create a private key on the Pi, outside this repository:

```bash
mkdir -p "$HOME/.local/share/pi-local-assistant"
chmod 700 "$HOME/.local/share/pi-local-assistant"
umask 077
python3 -c 'import secrets; print(secrets.token_hex(32))' > "$HOME/.local/share/pi-local-assistant/api.key"
```

With the model server running, start the API:

```bash
PI_ASSISTANT_API_KEY="$(cat "$HOME/.local/share/pi-local-assistant/api.key")" \
  python3 pi_assistant.py serve
```

From another computer, open an SSH tunnel to the Pi:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@raspberrypi.local
```

Your client can then call `http://127.0.0.1:8765`. Keep the bearer key in private client settings. The following examples run on the Pi, where the key file already exists:

```bash
API_KEY="$(cat "$HOME/.local/share/pi-local-assistant/api.key")"

curl -sS http://127.0.0.1:8765/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"pi-local-assistant","messages":[{"role":"user","content":"Hello"}],"max_tokens":80}'

curl -sS http://127.0.0.1:8765/api/sessions \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"title":"my chat"}'
```

The second call returns a `session_id`. Use it for continuing conversation with automatic compaction:

```bash
curl -sS http://127.0.0.1:8765/api/sessions/SESSION_ID/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"Continue our conversation"}'
```

For a standard OpenAI client, `POST /v1/chat/completions` accepts its normal text `messages` array. That route is stateless unless you add the optional `session_id` field. `GET /v1/models`, `GET /api/sessions`, and `GET /api/sessions/{id}/messages` are also available. Session lists accept `limit` and `offset`; message history accepts `limit` and `before_id` (the oldest returned message ID). Both endpoints return at most 100 rows per call to protect the Pi's RAM. Requests with `stream: true` return a clear error in this version.

An OpenAI SDK client can use `base_url="http://127.0.0.1:8765/v1"`, `model="pi-local-assistant"`, and your private API key. The SDK runs on the calling device, not on the Pi.

## Private data and safety

- The database defaults to `~/.local/share/pi-local-assistant/sessions.sqlite3` with mode `0600`. It stores full conversations and is never committed.
- The API key is generated on the Pi and remains in `~/.local/share/pi-local-assistant/api.key` with private file permissions.
- Model weights, local build artifacts, transcripts, API keys, and real configuration are excluded from Git.
- Both services default to loopback. An SSH tunnel provides encrypted remote access without opening a LAN or internet port.
- The model has a 2,048-token context, generates at most 256 tokens per turn, and processes one request at a time. The code uses no cloud API calls or telemetry.

For long-term use, back up the private SQLite file separately. The repository itself is safe to share; the database and key are not.

## Development

No Python packages are required:

```bash
python3 -m unittest discover -s tests -v
```

The tests use a fake model and do not download weights.
