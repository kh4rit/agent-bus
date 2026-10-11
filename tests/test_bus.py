import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_bus.server import build_app
from agent_bus.store import BusError, Store

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "clients"))
import agentbus  # noqa: E402

TOKEN = "s3cret"


# -- store --------------------------------------------------------------------


def test_send_inbox_thread_and_patterns():
    s = Store(":memory:")
    for n in ("claude@mac", "codex@mac", "codex@box"):
        s.touch(n)
    r = s.send("claude@mac", ["codex@*"], "hi all")
    assert sorted(r["delivered_to"]) == ["codex@box", "codex@mac"]
    assert s.unread_count("codex@mac") == 1

    [m] = s.inbox("codex@mac")
    assert m["body"] == "hi all" and m["from"] == "claude@mac"
    assert s.inbox("codex@mac") == []  # marked read

    reply = s.send("codex@mac", ["claude@mac"], "hello back", reply_to=m["id"])
    assert reply["thread_id"] == r["thread_id"]
    assert [x["body"] for x in s.thread(r["thread_id"], "claude@mac")] == ["hi all", "hello back"]
    with pytest.raises(BusError):
        s.thread(r["thread_id"], "stranger@x")


def test_unknown_recipient_queues_and_bad_input_rejected():
    s = Store(":memory:")
    r = s.send("claude@mac", ["later@box"], "for when you wake up")
    assert "never connected" in r["note"]
    assert s.inbox("later@box")[0]["body"] == "for when you wake up"
    with pytest.raises(BusError):
        s.send("claude@mac", ["nobody-*"], "x")
    with pytest.raises(BusError):
        s.send("bad name!", ["a"], "x")
    with pytest.raises(BusError):
        s.send("claude@mac", ["a"], "   ")


def test_sender_filter_leaves_other_messages_unread():
    s = Store(":memory:")
    s.send("claude@mac", ["w@box"], "trusted")
    s.send("rando@box", ["w@box"], "untrusted")
    got = s.inbox("w@box", senders=["claude@*"])
    assert [m["body"] for m in got] == ["trusted"]
    assert s.unread_count("w@box") == 1


def test_claims():
    s = Store(":memory:")
    assert s.claim("a@x", "src/app.py")["granted"]
    denied = s.claim("b@x", "src/app.py")
    assert not denied["granted"] and denied["held_by"] == "a@x"
    assert s.claim("a@x", "src/app.py", note="refresh")["granted"]
    assert not s.release("b@x", "src/app.py")["released"]
    assert s.release("a@x", "src/app.py")["released"]
    assert s.claim("b@x", "src/app.py")["granted"]


# -- running hub ----------------------------------------------------------------


@pytest.fixture(scope="module")
def hub_url(tmp_path_factory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    store = Store(tmp_path_factory.mktemp("db") / "bus.db")
    config = uvicorn.Config(build_app(store, TOKEN, ["hub.example-tailnet.ts.net"]), host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx2.get(f"{url}/api/health").status_code == 200:
                break
        except httpx2.TransportError:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    t.join(5)


def mcp_session(url: str, agent: str | None, extra: dict | None = None):
    headers = {"Authorization": f"Bearer {TOKEN}", **(extra or {})}
    if agent:
        headers["X-Agent-Name"] = agent
    return httpx2.AsyncClient(headers=headers, timeout=30)


async def call(url: str, agent: str | None, tool: str, args: dict | None = None, extra: dict | None = None):
    async with mcp_session(url, agent, extra) as http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                res = await session.call_tool(tool, args or {})
    if res.is_error:
        raise RuntimeError(res.content[0].text)
    data = res.structured_content
    # list results are wrapped as {"result": [...]} in structured output
    return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data


def test_auth_required(hub_url):
    assert httpx2.get(f"{hub_url}/api/health").status_code == 200
    assert httpx2.get(f"{hub_url}/api/agents").status_code == 401
    ok = httpx2.get(f"{hub_url}/api/agents", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_setup_files_served(hub_url):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = httpx2.get(f"{hub_url}/setup/agentbus.py", headers=auth)
    assert r.status_code == 200 and "def cmd_wake" in r.text
    assert httpx2.get(f"{hub_url}/setup/SKILL.md", headers=auth).text.startswith("---\nname: agent-bus")
    assert httpx2.get(f"{hub_url}/setup/..%2Fserver.py", headers=auth).status_code == 404
    assert httpx2.get(f"{hub_url}/setup/agentbus.py").status_code == 401
    # onboarding docs get the hub's own URL in place of the placeholder
    onboarding = httpx2.get(f"{hub_url}/setup/ONBOARDING.md", headers={**auth, "Host": "hub.example-tailnet.ts.net"}).text
    assert "{{HUB_URL}}" not in onboarding and "https://hub.example-tailnet.ts.net/setup/agentbus.py" in onboarding
    local = httpx2.get(f"{hub_url}/setup/SKILL.md", headers=auth).text
    assert "{{HUB_URL}}" not in local and f"{hub_url}/mcp" in local


def test_foreign_host_header_rejected(hub_url):
    h = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json", "X-Agent-Name": "claude@mac"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    good = httpx2.post(f"{hub_url}/mcp", json=body, headers={**h, "Host": "hub.example-tailnet.ts.net"})
    evil = httpx2.post(f"{hub_url}/mcp", json=body, headers={**h, "Host": "evil.example.com"})
    assert good.status_code == 200
    assert evil.status_code in (403, 421)
    # the REST API is covered too, not just /mcp
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert httpx2.get(f"{hub_url}/api/agents", headers={**auth, "Host": "evil.example.com"}).status_code == 421
    assert httpx2.get(f"{hub_url}/api/agents", headers={**auth, "Host": "hub.example-tailnet.ts.net"}).status_code == 200


async def test_mcp_round_trip(hub_url):
    assert (await call(hub_url, "claude@mac", "whoami"))["agent"] == "claude@mac"
    await call(hub_url, "codex@mac", "set_status", {"description": "reviewing PRs"})
    agents = {a["name"]: a for a in await call(hub_url, "claude@mac", "list_agents")}
    assert agents["codex@mac"]["description"] == "reviewing PRs"

    sent = await call(hub_url, "claude@mac", "send", {"to": ["codex@mac"], "body": "review #12?", "subject": "PR"})
    [m] = await call(hub_url, "codex@mac", "inbox")
    assert m["body"] == "review #12?" and m["id"] == sent["id"]
    await call(hub_url, "codex@mac", "send", {"to": ["claude@mac"], "body": "LGTM", "reply_to": m["id"]})
    [r] = await call(hub_url, "claude@mac", "wait_for_message", {"timeout_s": 5})
    assert r["body"] == "LGTM" and r["thread_id"] == sent["thread_id"]
    assert await call(hub_url, "claude@mac", "wait_for_message", {"timeout_s": 1}) == []

    assert (await call(hub_url, "claude@mac", "claim", {"resource": "port:3000"}))["granted"]
    assert not (await call(hub_url, "codex@mac", "claim", {"resource": "port:3000"}))["granted"]


async def test_mcp_requires_identity(hub_url):
    with pytest.raises(RuntimeError, match="No agent identity"):
        await call(hub_url, None, "whoami")


def test_long_poll_wakes_on_send(hub_url):
    client = agentbus.Hub(hub_url, TOKEN)
    client.inbox("poller@mac")  # register
    threading.Timer(1.0, lambda: client.send("claude@mac", ["poller@mac"], "ping")).start()
    t0 = time.monotonic()
    msgs = client.inbox("poller@mac", wait=10)
    assert [m["body"] for m in msgs] == ["ping"]
    assert time.monotonic() - t0 < 5


def run_cli(hub_url, *argv, stdin=""):
    return subprocess.run(
        [sys.executable, str(ROOT / "clients/agentbus.py"), "--hub", hub_url, *argv],
        input=stdin, capture_output=True, text=True, timeout=30,
        env={"AGENT_BUS_TOKEN": TOKEN, "PATH": "/usr/bin:/bin", "AGENT_BUS_CONFIG": "/nonexistent"},
    )


def test_claude_hooks(hub_url):
    client = agentbus.Hub(hub_url, TOKEN)
    assert run_cli(hub_url, "hook", "prompt", "--name", "hooked@mac", stdin="{}").stdout == ""

    client.send("codex@mac", ["hooked@mac"], "build is green")
    out = json.loads(run_cli(hub_url, "hook", "prompt", "--name", "hooked@mac", stdin="{}").stdout)
    assert "build is green" in out["hookSpecificOutput"]["additionalContext"]

    client.send("codex@mac", ["hooked@mac"], "one more thing")
    out = json.loads(run_cli(hub_url, "hook", "stop", "--name", "hooked@mac", stdin='{"stop_hook_active": false}').stdout)
    assert out["decision"] == "block" and "one more thing" in out["reason"]

    client.send("codex@mac", ["hooked@mac"], "not now")
    assert run_cli(hub_url, "hook", "stop", "--name", "hooked@mac", stdin='{"stop_hook_active": true}').stdout == ""


def test_hook_is_silent_when_hub_down():
    r = subprocess.run([sys.executable, str(ROOT / "clients/agentbus.py"), "--hub", "http://127.0.0.1:1",
                        "hook", "prompt", "--name", "x@y"], input="{}", capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == ""


def test_cli_hub_errors_exit_2():
    r = subprocess.run([sys.executable, str(ROOT / "clients/agentbus.py"), "--hub", "http://127.0.0.1:1",
                        "inbox", "--name", "x@y"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and r.stdout == "" and "cannot reach hub" in r.stderr


def test_wake_worker_runs_and_replies(hub_url):
    client = agentbus.Hub(hub_url, TOKEN)
    stop = threading.Event()
    script = "import sys; print('handled: ' + sys.argv[1].strip().splitlines()[-1])"
    worker = agentbus.Worker(client, {
        "name": "worker@box",
        "command": [sys.executable, "-c", script, "{prompt}"],
        "allow_from": ["boss@*"],
        "cwd": "/tmp",
    }, stop)
    worker.start()
    try:
        client.send("intruder@x", ["worker@box"], "rm -rf /")
        sent = client.send("boss@mac", ["worker@box"], "count the files")
        deadline = time.monotonic() + 20
        replies = []
        while not replies and time.monotonic() < deadline:
            replies = client.inbox("boss@mac", wait=2)
        [reply] = replies
        assert reply["from"] == "worker@box" and reply["reply_to"] == sent["id"]
        assert "count the files" in reply["body"]
        # the message from a sender outside allow_from is left untouched
        assert client.inbox("worker@box", mark_read=False)[0]["body"] == "rm -rf /"
    finally:
        stop.set()


def test_wake_worker_reports_failure(hub_url):
    client = agentbus.Hub(hub_url, TOKEN)
    stop = threading.Event()
    worker = agentbus.Worker(client, {
        "name": "broken@box", "command": [sys.executable, "-c", "import sys; sys.exit('boom')"],
        "allow_from": ["*"], "cwd": "/tmp",
    }, stop)
    worker.start()
    try:
        client.send("boss2@mac", ["broken@box"], "do it")
        replies = []
        deadline = time.monotonic() + 20
        while not replies and time.monotonic() < deadline:
            replies = client.inbox("boss2@mac", wait=2)
        assert "failed (exit 1)" in replies[0]["body"] and "boom" in replies[0]["body"]
    finally:
        stop.set()


# -- per-project names ------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_derive_name_from_project(tmp_path, monkeypatch):
    repo = tmp_path / "My Repo"
    (repo / "src").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x", cwd=repo)
    _git("worktree", "add", "-q", "--detach", str(tmp_path / "wt-xyz"), cwd=repo)
    plain = tmp_path / "notes"
    plain.mkdir()
    cfg = {"host": "laptop"}

    assert agentbus.derive_name("claude", repo / "src", cfg) == "claude-my-repo@laptop"
    assert agentbus.derive_name("claude", tmp_path / "wt-xyz", cfg) == "claude-my-repo@laptop"  # worktree → main repo
    assert agentbus.derive_name("codex", plain, cfg) == "codex-notes@laptop"
    monkeypatch.setenv("HOME", str(plain))
    assert agentbus.derive_name("claude", plain, cfg) == "claude@laptop"  # home directory → no project


def run_in(cwd, *argv, stdin="", extra_env=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home", "AGENT_BUS_CONFIG": "/nonexistent", **(extra_env or {})}
    return subprocess.run([sys.executable, str(ROOT / "clients/agentbus.py"), *argv], cwd=cwd, input=stdin,
                          capture_output=True, text=True, timeout=30, env=env)


def test_headers_helper(tmp_path):
    proj = tmp_path / "myapp"
    proj.mkdir()
    out = json.loads(run_in(proj, "headers", "--tool", "claude", extra_env={"AGENT_BUS_TOKEN": "t"}).stdout)
    assert out["X-Agent-Name"].startswith("claude-myapp@")
    assert out["Authorization"] == "Bearer t"
    # explicit name wins
    out = json.loads(run_in(proj, "headers", "--tool", "claude", extra_env={"AGENT_BUS_NAME": "x@y"}).stdout)
    assert out["X-Agent-Name"] == "x@y"


def test_hook_uses_project_name(hub_url, tmp_path):
    proj = tmp_path / "projx"
    proj.mkdir()
    name = run_in(proj, "whoami", "--tool", "claude").stdout.strip()
    assert name.startswith("claude-projx@")
    agentbus.Hub(hub_url, TOKEN).send("codex@mac", [name], "for projx only")
    event = json.dumps({"cwd": str(proj)})
    r = run_in(tmp_path, "--hub", hub_url, "hook", "prompt", "--tool", "claude", stdin=event,
               extra_env={"AGENT_BUS_TOKEN": TOKEN})
    assert "for projx only" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


# -- cursor, peek long-poll, thread, forget -------------------------------------------


def test_after_id_cursor_replays_without_marking():
    s = Store(":memory:")
    ids = [s.send("a@x", ["c@x"], f"m{i}")["id"] for i in range(3)]
    s.inbox("c@x", limit=1)  # consume the first one
    replay = s.inbox("c@x", after_id=ids[0])
    assert [m["body"] for m in replay] == ["m1", "m2"]
    assert s.unread_count("c@x") == 2  # cursor reads mark nothing
    assert [m["body"] for m in s.inbox("c@x", after_id=0)] == ["m0", "m1", "m2"]  # read ones included


def test_peek_long_polls(hub_url):
    client = agentbus.Hub(hub_url, TOKEN)
    client.inbox("peeker@mac")
    t0 = time.monotonic()
    assert client.inbox("peeker@mac", wait=1.5, mark_read=False) == []
    assert time.monotonic() - t0 >= 1.4  # waited instead of returning at once
    threading.Timer(0.5, lambda: client.send("a@mac", ["peeker@mac"], "hi")).start()
    [m] = client.inbox("peeker@mac", wait=10, mark_read=False)
    assert m["body"] == "hi"
    assert client.inbox("peeker@mac", mark_read=False)[0]["id"] == m["id"]  # still unread


def test_cursor_long_polls(hub_url):
    client = agentbus.Hub(hub_url, TOKEN)
    first = client.send("a@mac", ["cursor@mac"], "one")["id"]
    threading.Timer(0.5, lambda: client.send("a@mac", ["cursor@mac"], "two")).start()
    msgs = client.inbox("cursor@mac", wait=10, after_id=first)
    assert [m["body"] for m in msgs] == ["two"]


def test_thread_and_forget_via_rest(hub_url):
    client = agentbus.Hub(hub_url, TOKEN)
    root = client.send("t1@mac", ["t2@mac"], "question")
    client.send("t2@mac", ["t1@mac"], "answer", reply_to=root["id"])
    assert [m["body"] for m in client.thread("t1@mac", root["thread_id"])] == ["question", "answer"]
    with pytest.raises(RuntimeError, match="not part of thread"):
        client.thread("outsider@mac", root["thread_id"])

    client.inbox("stale@mac")  # connects once, so it is in the directory
    client.send("t1@mac", ["stale@mac"], "never read")
    with pytest.raises(RuntimeError, match="unread"):
        client.forget("stale@mac")
    assert client.forget("stale@mac", force=True)["dropped_unread"] == 1
    assert "stale@mac" not in [a["name"] for a in client.agents()]


async def test_process_alias_from_hook(hub_url, tmp_path, monkeypatch):
    """MCP connects with a guessed name; once a hook in the same claude process reports the project, the
    MCP connection acts as the project agent."""
    stand_in = subprocess.Popen(["sleep", "60"])  # plays the claude process; fresh pid, so no earlier aliases
    pid = str(stand_in.pid)
    monkeypatch.setenv("CLAUDE_PID", pid)
    key = agentbus.process_key({})
    assert key and key.split(":")[1] == pid
    proc_hdr = {"X-Agent-Process": key}
    fallback = f"claude-p{pid}@mac"
    assert (await call(hub_url, fallback, "whoami", extra=proc_hdr))["agent"] == fallback

    proj = tmp_path / "aliasproj"
    proj.mkdir()
    r = run_in(tmp_path, "--hub", hub_url, "hook", "prompt", "--tool", "claude",
               stdin=json.dumps({"cwd": str(proj), "session_id": "abc"}),
               extra_env={"AGENT_BUS_TOKEN": TOKEN, "CLAUDE_PID": pid})
    assert r.returncode == 0, r.stderr
    project_name = run_in(proj, "whoami", "--tool", "claude").stdout.strip()
    assert project_name.startswith("claude-aliasproj@")

    assert (await call(hub_url, fallback, "whoami", extra=proc_hdr))["agent"] == project_name
    sent = await call(hub_url, fallback, "send", {"to": ["aliascheck@mac"], "body": "from the aliased session"},
                      extra=proc_hdr)
    [m] = agentbus.Hub(hub_url, TOKEN).inbox("aliascheck@mac")
    assert m["id"] == sent["id"]
    assert m["from"] == project_name  # replies reach the project inbox the hooks read
    # same pid with a different start time (pid reuse) does not inherit the alias
    host, _, _ = key.split(":")
    reused = {"X-Agent-Process": f"{host}:{pid}:1"}
    assert (await call(hub_url, "other@mac", "whoami", extra=reused))["agent"] == "other@mac"
    stand_in.kill()


def test_claude_pid_skips_wrapping_shell():
    """The headersHelper may run as `sh -c 'python3 agentbus.py headers'`; the key must name claude, not sh."""
    script = "import sys; sys.path.insert(0, %r); import agentbus; print(agentbus.claude_pid())" % str(ROOT / "clients")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PID"}
    direct = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env).stdout.strip()
    via_sh = subprocess.run(["sh", "-c", f"{sys.executable} -c {shlex.quote(script)}; :"], capture_output=True,
                            text=True, env=env).stdout.strip()
    assert direct == via_sh == str(os.getpid())


# -- claims from the CLI, version/update ----------------------------------------------


def cli(hub_url, *argv, home="/nonexistent-home", script=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "AGENT_BUS_CONFIG": "/nonexistent", "AGENT_BUS_TOKEN": TOKEN}
    return subprocess.run([sys.executable, str(script or ROOT / "clients/agentbus.py"), "--hub", hub_url, *argv],
                          capture_output=True, text=True, timeout=60, env=env)


def test_cli_claims(hub_url):
    res = "gpu@bench-box"
    a = cli(hub_url, "claim", res, "--name", "runner-a@box", "--ttl", "5", "--note", "benchmark")
    assert a.returncode == 0 and json.loads(a.stdout)["granted"]
    b = cli(hub_url, "claim", res, "--name", "runner-b@box")
    assert b.returncode == 3 and json.loads(b.stdout)["held_by"] == "runner-a@box"
    assert res in cli(hub_url, "claims").stdout

    # --wait: b gets it as soon as a releases
    threading.Timer(1.0, lambda: cli(hub_url, "release", res, "--name", "runner-a@box")).start()
    t0 = time.monotonic()
    b = cli(hub_url, "claim", res, "--name", "runner-b@box", "--wait", "20")
    assert b.returncode == 0 and json.loads(b.stdout)["granted"] and time.monotonic() - t0 < 15
    assert json.loads(cli(hub_url, "release", res, "--name", "runner-b@box").stdout)["released"]


def test_version_and_update(hub_url, tmp_path):
    home = tmp_path / "home"
    skill = home / ".claude/skills/agent-bus/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old skill\n")
    installed = tmp_path / "bin/agentbus.py"  # an installed copy, not the repo checkout
    installed.parent.mkdir()
    installed.write_text((ROOT / "clients/agentbus.py").read_text() + "\n# locally modified\n")

    rows = json.loads(cli(hub_url, "version", "--json", home=home, script=installed).stdout)
    assert {r["file"]: r["current"] for r in rows} == {"agentbus.py": False, "SKILL.md": False}

    out = cli(hub_url, "update", home=home, script=installed)
    assert out.returncode == 0 and out.stdout.count("update ") == 2, out.stdout + out.stderr
    assert "{{HUB_URL}}" not in skill.read_text() and f"{hub_url}/mcp" in skill.read_text()
    assert (skill.parent / "SKILL.md.bak").read_text() == "old skill\n"
    rows = json.loads(cli(hub_url, "version", "--json", home=home, script=installed).stdout)
    assert all(r["current"] for r in rows)

    # the repository's own client is never overwritten
    dev = cli(hub_url, "update", home=home)
    assert "development checkout" in dev.stdout
