#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
pi_user=$(id -un)
pi_home=$HOME
llama_dir=${LLAMA_CPP_DIR:-"$pi_home/llama.cpp"}
model_dir=${MODEL_DIR:-"$pi_home/models"}
model_path=${MODEL_PATH:-"$model_dir/Qwen3.5-0.8B-Q4_K_M.gguf"}
model_limit=${PI_MODEL_MEMORY_MAX:-820M}
model_high=${PI_MODEL_MEMORY_HIGH:-780M}
model_swap_limit=${PI_MODEL_SWAP_MAX:-300M}
key_dir="$pi_home/.local/share/pi-local-assistant"
key_file="$key_dir/api.key"

if [[ ! -x "$llama_dir/build/bin/llama-server" ]]; then
  echo "Build the model server with scripts/setup_pi.sh first." >&2
  exit 1
fi
if [[ ! -f "$model_path" ]]; then
  echo "Download the model with scripts/setup_pi.sh first." >&2
  exit 1
fi
if [[ ! -r /sys/fs/cgroup/cgroup.controllers ]] || ! grep -qw memory /sys/fs/cgroup/cgroup.controllers; then
  echo "Enable the Linux memory cgroup controller before installing capped services." >&2
  exit 1
fi

mkdir -p "$key_dir"
chmod 700 "$key_dir"
if [[ ! -f "$key_file" ]]; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$key_file"
fi
chmod 600 "$key_file"

cat <<UNIT | sudo tee /etc/systemd/system/pi-local-model.service >/dev/null
[Unit]
Description=Pi Local Assistant model server
After=local-fs.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User=$pi_user
Environment="HOME=$pi_home"
Environment="LLAMA_CPP_DIR=$llama_dir"
Environment="MODEL_PATH=$model_path"
WorkingDirectory=$project_dir
ExecStart=/usr/bin/env bash "$project_dir/scripts/run_model.sh"
Restart=on-failure
RestartSec=15
MemoryHigh=$model_high
MemoryMax=$model_limit
MemorySwapMax=$model_swap_limit
LimitCORE=0
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

cat <<UNIT | sudo tee /etc/systemd/system/pi-local-api.service >/dev/null
[Unit]
Description=Pi Local Assistant private chat API
After=pi-local-model.service
Requires=pi-local-model.service
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User=$pi_user
Environment="HOME=$pi_home"
WorkingDirectory=$project_dir
LoadCredential=api_key:$key_file
ExecStart=/usr/bin/python3 "$project_dir/pi_assistant.py" serve
Restart=on-failure
RestartSec=15
MemoryMax=100M
MemorySwapMax=50M
LimitCORE=0
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

sudo chmod 644 /etc/systemd/system/pi-local-model.service /etc/systemd/system/pi-local-api.service
sudo systemctl daemon-reload
sudo systemctl enable pi-local-model.service pi-local-api.service
sudo systemctl restart pi-local-model.service pi-local-api.service
echo "Services installed. Check: systemctl status pi-local-model pi-local-api"
