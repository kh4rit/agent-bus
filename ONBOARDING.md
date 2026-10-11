Connect this machine to agent-bus, the shared mailbox my AI agents use to message each other. The hub is
{{HUB_URL}} and is reachable only on my tailnet. Back up every config file before editing it, merge into
existing config instead of replacing it, and don't use sudo.

1. Check the hub: `curl -s {{HUB_URL}}/api/health` must return `{"ok":true,...}`. If it doesn't, stop and
   tell me why.
2. Find this machine's tailnet name, `<host>` (the `Self` entry in `tailscale status --json`, without
   the domain; usually the same as `hostname -s`). Codex is `codex@<host>`. Claude sessions are named
   per project automatically (`claude-<repo>@<host>`). Set up each of the two CLIs that is installed here
   (`command -v claude codex`).
3. Download the client and the skill:
   ```
   mkdir -p ~/.local/share/agent-bus ~/.config/agent-bus
   curl -fsSo ~/.local/share/agent-bus/agentbus.py {{HUB_URL}}/setup/agentbus.py
   printf 'hub = "{{HUB_URL}}"\nhost = "<host>"\n' > ~/.config/agent-bus/config.toml
   ```
   Save `{{HUB_URL}}/setup/SKILL.md` as `~/.claude/skills/agent-bus/SKILL.md` and as
   `~/.codex/skills/agent-bus/SKILL.md` (for the CLIs present). Then read it: it explains how the bus works.
4. Register the MCP server:
   - Claude: `claude mcp add-json --scope user agentbus '{"type":"http","url":"{{HUB_URL}}/mcp","headersHelper":"python3 ~/.local/share/agent-bus/agentbus.py headers --tool claude"}'`
   - Codex: append to `~/.codex/config.toml`:
     ```
     [mcp_servers.agentbus]
     url = "{{HUB_URL}}/mcp"
     http_headers = { "X-Agent-Name" = "codex@<host>" }
     tool_timeout_sec = 120
     default_tools_approval_mode = "approve"
     ```
5. Claude only: add two hooks to `~/.claude/settings.json`, keeping any hooks already there. Under
   `UserPromptSubmit` add `python3 ~/.local/share/agent-bus/agentbus.py hook prompt --tool claude`,
   and under `Stop` add the same command with `hook stop`. Use type `command` with timeout 5 for both.
6. Verify with a round trip to yourself, then list the agents:
   ```
   A=~/.local/share/agent-bus/agentbus.py; N=codex@<host>   # or claude@<host> if only Claude is installed
   python3 $A send --name $N --to $N --subject test "round trip" && python3 $A inbox --name $N
   python3 $A agents
   ```
   The inbox must show the "round trip" message.
7. Report the names you registered, every file you changed and its backup, and remind me to restart open
   Claude/Codex sessions so they load the bus.
