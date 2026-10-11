#!/usr/bin/env bash
# Install the agent-bus hub as a systemd service and publish it on the tailnet.
# Run on the hub machine from anywhere inside the project:  ./deploy/install-hub.sh
# Optional: AGENT_BUS_TOKEN=... ./deploy/install-hub.sh   to also require a shared bearer token.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${AGENT_BUS_PORT:-8765}"
RUN_USER="$(id -un)"

# `ssh host 'cmd'` runs a non-interactive shell that skips the profile, so add uv's usual install dirs.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"

command -v uv >/dev/null || {
  echo "uv is not installed on $(hostname). Install it, then rerun this script:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
}
command -v tailscale >/dev/null || { echo "tailscale is required on this machine"; exit 1; }

echo "==> Installing dependencies (uv: $(command -v uv))"
(cd "$DIR" && uv sync --frozen --no-dev)

DNS="$(tailscale status --json | "$DIR/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo "==> Tailnet name: $DNS"

if [[ -n "${AGENT_BUS_TOKEN:-}" ]]; then
  echo "==> Writing /etc/agent-bus.env (bearer token)"
  sudo install -m 600 /dev/null /etc/agent-bus.env
  echo "AGENT_BUS_TOKEN=$AGENT_BUS_TOKEN" | sudo tee /etc/agent-bus.env >/dev/null
fi

echo "==> Writing /etc/systemd/system/agent-bus.service"
sudo tee /etc/systemd/system/agent-bus.service >/dev/null <<EOF
[Unit]
Description=agent-bus hub (MCP mailbox for AI agents)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$RUN_USER
ExecStart=$DIR/.venv/bin/agent-bus
Environment=AGENT_BUS_HOST=127.0.0.1
Environment=AGENT_BUS_PORT=$PORT
Environment=AGENT_BUS_DB=/var/lib/agent-bus/bus.db
Environment=AGENT_BUS_ALLOWED_HOSTS=$DNS
Environment=AGENT_BUS_PUBLIC_URL=https://$DNS
EnvironmentFile=-/etc/agent-bus.env
StateDirectory=agent-bus
Restart=always
RestartSec=2
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now agent-bus
sudo systemctl restart agent-bus

echo "==> Publishing on the tailnet (tailnet-only, not Funnel)"
sudo tailscale serve --bg --https=443 "http://127.0.0.1:$PORT"

for _ in $(seq 20); do curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 0.5; done
curl -sf "https://$DNS/api/health" && echo
echo
echo "Hub ready:  https://$DNS/mcp"
echo "Onboard a machine: paste this to an agent there ->  Fetch https://$DNS/setup/ONBOARDING.md and follow it."
echo "Logs:       journalctl -u agent-bus -f"
