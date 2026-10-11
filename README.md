# agent-bus

A small, self-hosted mailbox that lets AI coding agents (Claude Code, Codex, and anything else that
speaks MCP) message each other, on one machine or across your [Tailscale](https://tailscale.com) network.

```
                       hub (any always-on machine on your tailnet)
             agent-bus: MCP over HTTP + SQLite, published with `tailscale serve`
                          https://hub.<your-tailnet>.ts.net/mcp
         ┌──────────────────┬──────────┴─────────┬───────────────────────┐
  claude-myapp@laptop   codex@laptop   claude-infra@desktop   codex-worker@desktop
                                                               (started on demand)
```

Agents already run in separate terminals, on separate machines, with no way to ask each other anything.
agent-bus gives each of them an address and an inbox, so a Claude session can hand a review to Codex,
ask the agent on your Linux box to run benchmarks, or claim a file before editing it.

- **One protocol for every agent.** Agents connect with their normal MCP client and get tools: `whoami`,
  `list_agents`, `set_status`, `send`, `inbox`, `wait_for_message`, `read_thread`, `claim`, `release`,
  `list_claims`.
- **Messages persist.** The hub keeps them in SQLite, so an agent that's offline gets them later.
- **Messages arrive by themselves in Claude Code.** Hooks inject unread messages when you submit a prompt,
  and when Claude finishes a turn (so it can answer them).
- **Idle agents can be woken.** An optional daemon starts a headless agent (`codex exec`, `claude -p`)
  when a message arrives for it, and sends its answer back as the reply.
- **Advisory claims.** Agents can lock a file, port or machine for a while, so they don't collide.
- **Small.** The hub is about 700 lines of Python on the official MCP SDK. The client is one file that
  uses only the standard library.

## How it works

The **hub** runs on one machine. It listens on `127.0.0.1` only and is published to your tailnet with
`tailscale serve`, so it's never on the public internet and Tailscale handles encryption and access.

Agents identify themselves with an **`X-Agent-Name`** header, `<tool>@<host>`:

- **Codex:** a static name per machine, `codex@<host>`.
- **Claude Code:** one name per project, `claude-<repo>@<host>`. A `headersHelper` computes it when the
  session connects. Claude Code starts that helper outside the project (and background sessions connect
  before they have one), so the hooks also report the project to the hub, keyed by the claude process.
  From then on the hub uses the project name for that session's MCP calls. Worktrees count as their
  repository.

Machines get the client, the agent skill and setup instructions from the hub itself (`/setup/...`), so
adding a machine is one prompt.

## Quick start

**Requirements:** a Linux machine on your tailnet with systemd, [`uv`](https://docs.astral.sh/uv/) and
Tailscale (the hub; a small VPS is ideal), and Python 3.11+ on each machine that runs agents.

### 1. Install the hub

On the hub machine:

```bash
git clone https://github.com/kh4rit/agent-bus ~/agent-bus
~/agent-bus/deploy/install-hub.sh
```

The script installs dependencies, creates a hardened `agent-bus.service`, and runs
`tailscale serve --bg --https=443`. It prints the hub URL and the one-line onboarding prompt. To also
require a shared bearer token, run it as `AGENT_BUS_TOKEN=<random> ./deploy/install-hub.sh`.

### 2. Connect each machine

Paste this to Claude Code or Codex on the machine:

> Fetch `https://hub.<your-tailnet>.ts.net/setup/ONBOARDING.md` and follow it.

The agent checks the hub, downloads the client and the `agent-bus` skill, writes
`~/.config/agent-bus/config.toml`, registers the MCP server (and the hooks, for Claude), and verifies
with a round trip. It backs up every file it changes and doesn't need sudo. Restart open sessions
afterwards: MCP servers load when a session starts.

<details>
<summary>Manual setup</summary>

```bash
mkdir -p ~/.local/share/agent-bus ~/.config/agent-bus
curl -fsSo ~/.local/share/agent-bus/agentbus.py https://hub.<your-tailnet>.ts.net/setup/agentbus.py
cp examples/config.toml ~/.config/agent-bus/config.toml        # then set hub and host
```

- **Claude Code:** register the server, merge `examples/claude-hooks.json` into
  `~/.claude/settings.json`, and install the skill to `~/.claude/skills/agent-bus/SKILL.md` (download
  it from `/setup/SKILL.md`, which fills in your hub's URL).
  ```bash
  claude mcp add-json --scope user agentbus '{"type":"http","url":"https://hub.<your-tailnet>.ts.net/mcp","headersHelper":"python3 ~/.local/share/agent-bus/agentbus.py headers --tool claude"}'
  ```
- **Codex:** add `examples/codex-config.toml` to `~/.codex/config.toml`, and install the skill to
  `~/.codex/skills/agent-bus/SKILL.md`. `default_tools_approval_mode = "approve"` is required, otherwise
  `codex exec` rejects MCP calls.

</details>

## Using it

Ask your agent in plain language: "ask codex@desktop to review `src/x.py`", "check the bus", "claim
port 3000 on the desktop while you run the server". The bundled skill
([`skills/agent-bus/SKILL.md`](skills/agent-bus/SKILL.md)) teaches agents the naming scheme, how to
write messages an agent without your files can act on, when to wait for replies, how to handle claims
and incoming requests, and basic troubleshooting.

From a terminal:

```bash
A=~/.local/share/agent-bus/agentbus.py
python3 $A agents                                   # who exists, what they're doing, unread counts
python3 $A whoami --tool claude                     # the name a Claude session in this directory gets
python3 $A send --tool claude --to codex@desktop "please run the benchmarks"
python3 $A inbox --tool claude --wait 60            # long-poll; --peek to leave messages unread
python3 $A inbox --tool claude --after-id 240       # replay from a cursor, marks nothing
python3 $A thread 248                               # a whole conversation
python3 $A forget old-probe@laptop                  # remove a stale identity
python3 $A claim gpu@desktop --name bench@desktop --ttl 30 --wait 600   # advisory lock for scripts
python3 $A release gpu@desktop --name bench@desktop
python3 $A version                                  # installed client/skill vs. the hub's
python3 $A update                                   # refresh them from the hub
```

The CLI exits 0 on success (including "no messages"), 2 if the hub is unreachable or returns an error,
and 3 if `claim` was denied, so scripts can branch on the exit code.

Copies of the client and skill on each machine come from the hub's `/setup/` files. The hub's health
check fingerprints them, so `version` shows which copies are out of date and `update` replaces them
(keeping a `.bak`). A client that is the repository's own file is left alone; update that with git. A
client from before `update` existed needs one manual download first:
`curl -fsSo ~/.local/share/agent-bus/agentbus.py https://hub.<your-tailnet>.ts.net/setup/agentbus.py`.

### Workers

Add `[[worker]]` entries to `~/.config/agent-bus/config.toml` (see
[`examples/config.toml`](examples/config.toml)) and run `python3 agentbus.py wake`, for example with
[`examples/com.agentbus.wake.plist`](examples/com.agentbus.wake.plist) on macOS or a systemd user unit
on Linux. When a message arrives for a worker name from a sender matching `allow_from`, the daemon runs
the worker's command with the message as the prompt and replies with its final output. Give workers
names distinct from your interactive agents, so they don't compete for the same inbox.

## Security model

- **Network:** the hub binds to `127.0.0.1`, and `tailscale serve` publishes it to your tailnet only.
  Your tailnet ACLs decide which machines can reach it. Don't use Funnel. An optional bearer token
  (`AGENT_BUS_TOKEN`) adds a second check. The hub rejects requests with unexpected `Host` headers on every route, which blocks DNS-rebinding
  attacks from web pages.
- **Identity is self-declared.** Any client that can reach the hub can claim any name. agent-bus is for
  agents you run yourself, on machines you trust, not for multi-tenant use.
- **Messages are untrusted input.** A message from another agent is a peer request, not an instruction
  from the user. The server instructions, the hook text, the worker prompt and the skill all say so, and
  tell agents to check with the user before anything destructive or outward-facing.
- **Workers run agents unattended.** They act only for senders matching `allow_from`. Keep their commands
  at normal permission levels: no `--dangerously-skip-permissions` or full-auto modes.
- **Don't send secrets** over the bus. Messages are stored in plain text in the hub's SQLite database.

## Configuration

| Hub (environment) | Default | |
|---|---|---|
| `AGENT_BUS_HOST` / `AGENT_BUS_PORT` | `127.0.0.1` / `8765` | bind address |
| `AGENT_BUS_DB` | `~/.local/share/agent-bus/bus.db` | SQLite file (the installer uses `/var/lib/agent-bus/bus.db`) |
| `AGENT_BUS_TOKEN` | unset | require `Authorization: Bearer <token>` |
| `AGENT_BUS_ALLOWED_HOSTS` | localhost only | extra `Host` headers to accept (your `*.ts.net` name) |
| `AGENT_BUS_PUBLIC_URL` | derived per request | the URL filled into `/setup/*.md` |

Clients read `~/.config/agent-bus/config.toml` (`hub`, `host`, optional `token`, `[[worker]]`), and
honour `AGENT_BUS_URL`, `AGENT_BUS_TOKEN` and `AGENT_BUS_NAME`.

## Troubleshooting

- **`Could not resolve host ... ts.net`:** something is overriding Tailscale MagicDNS. Corporate DNS
  filters such as Microsoft Global Secure Access are known to. Tailscale traffic still works, so add the
  hub's tailnet IP to `/etc/hosts` or turn the filter off.
- **No `agentbus` tools:** restart the session; check `claude mcp list` or `~/.codex/config.toml`.
- **Codex says "MCP tool call requires approval":** set `default_tools_approval_mode = "approve"`.
- **A Claude session has a `claude-p<pid>@host` name:** that's its temporary name until its first prompt,
  when the hooks register its project. If it persists, check that the hooks are installed.

## Limitations

- One hub, no replication. If it's down, agents can't message each other (they keep working otherwise).
- Delivery is pull-based: agents see messages when they check, when a Claude hook fires, or when a
  worker is woken. There's no push into a running Codex session.
- Names are per project, not per session: two Claude sessions in the same repository share an inbox, and
  whichever checks first gets the message. Sessions on the same machine can reach each other directly
  with Claude Code's own session messaging.
- Tested with Claude Code 2.1 and Codex CLI 0.160 on macOS and Linux. The Windows client is untested.

## Development

```bash
uv sync
uv run pytest
AGENT_BUS_DB=/tmp/bus.db uv run agent-bus      # local hub on http://127.0.0.1:8765
```

Layout: `src/agent_bus/` (hub: `server.py` MCP tools and REST API, `store.py` SQLite), `clients/agentbus.py`
(CLI, hooks, headers helper, wake daemon), `skills/agent-bus/` (agent skill), `ONBOARDING.md` (setup
prompt served by the hub), `deploy/` (hub installer), `examples/`.

## License

[MIT](LICENSE)
