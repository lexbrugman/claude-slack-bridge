# Install with Homebrew

An alternative to the `git clone` + `docker compose up -d` instructions in the
README. What it changes:

- **launchd (or systemd) supervises the daemon**, not Docker's own restart
  policy. `brew services start/stop/restart` is the control surface, and the
  container comes back after a reboot without Docker Desktop having to decide
  that for itself.
- **Config lives outside the source tree**, in `~/.config/claude-slack-bridge/`.
  An upgrade replaces the code and cannot touch your tokens or your channel
  mapping.

Docker is still what runs the daemon. This packages *how it is started*, not
what it is.

```
brew services  ──▶  claude-slack-bridge  ──▶  docker compose up --build
   (launchd)          (foreground wrapper)         (the daemon)
```

## Install

```bash
# any engine will do — OrbStack is the lightest
brew install --cask orbstack

brew install lexbrugman/tap/claude-slack-bridge
```

The formula deliberately does not declare a Docker dependency: OrbStack,
Colima and Docker Desktop all satisfy it, and the formula has no business
picking one for you.

## Configure

The config is not created for you — it holds two Slack tokens, and a file no
installer writes is a file no upgrade can overwrite.

```bash
mkdir -p ~/.config/claude-slack-bridge
cp "$(brew --prefix claude-slack-bridge)/libexec/config.env.default" \
   ~/.config/claude-slack-bridge/config.env
chmod 600 ~/.config/claude-slack-bridge/config.env
```

Then edit it. The template documents every setting; these must be right before
the first start:

| Setting | Why |
| --- | --- |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | From your Slack app — see [slack-setup.md](slack-setup.md). |
| `PROJECTS_DIR` | The **parent** directory of your projects. Mounted at `/projects` in the container, so a project at `~/Workspace/api` is `/projects/api` in the mapping. |
| `SECURITY_ALLOWED_USERS` | The template ships denying everyone. Read on. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Only for the Slack → Claude direction, and not in the template because a mounted `~/.claude` does not carry your login on macOS. Read on. |

### The access-control setting is not optional

The Slack → Claude direction runs `claude -p` against your files, on your
machine, as you. Anyone who can post in a channel the Slack app is installed in
can drive it. Upstream's default is permissive; the template here ships
`SECURITY_ENABLED=true` with `SECURITY_STRICT_MODE=true` and empty lists, which
denies **everyone including you** until you add your own Slack user ID (Slack
profile → ⋮ → Copy member ID). See [security.md](security.md).

If you only want the Claude → Slack direction, leave the lists empty. That is
the closed configuration, and `ask_on_slack` is unaffected by it.

Where the config is looked for, in order:

1. `~/.config/claude-slack-bridge/config.env` — yours
2. `$(brew --prefix)/etc/claude-slack-bridge/config.env` — machine-level, if you
   prefer one config for all users of the machine
3. the `.env` in the installed tree — only relevant when running from a checkout

### Authenticate the Claude CLI

Only the **Slack → Claude** direction needs this. `ask_on_slack` does not: that
path runs `session.py` in the container as a relay, and the Claude asking the
question is the one on your host, already logged in.

Without it, the bot answers *"Sorry, I encountered an error processing your
request."* and the log shows the CLI exiting `rc=1`. Run it by hand to see the
real reason:

```bash
docker exec claude-slack-bridge claude -p 'say ok'
# Not logged in · Please run /login
```

**On macOS the mounted `~/.claude` cannot carry your login.** Claude Code stores
its OAuth token as a login-Keychain item, not as a file in that directory, so
the mount brings your settings, history and `CLAUDE.md` across but not your
credentials. The container cannot reach the Keychain either: that is the macOS
Security framework talking to `securityd` over Mach IPC, and the container is a
Linux VM with neither. Docker shares files, not OS services. On a Linux host the
CLI writes `~/.claude/.credentials.json` instead, which the mount *does* carry —
which is why this works there and fails here.

Mint a long-lived token on the host, where the Keychain exists, and hand it to
the container:

```bash
claude setup-token                    # on the HOST, not in the container
```

Put it in your config and restart:

```
CLAUDE_CODE_OAUTH_TOKEN=<the token it prints>
```

```bash
brew services restart claude-slack-bridge
docker exec claude-slack-bridge claude -p 'say ok'    # should answer now
```

No compose or Dockerfile change is needed — `env_file` injects everything in
`config.env` into the container environment, so the variable arrives on its own.
It lives outside the installed tree, so it survives container recreation,
`brew upgrade` and reboots.

The cost is a long-lived token in a file rather than in the Keychain — revoke it
independently if it leaks; `chmod 600` the config, as above.

The one alternative: `docker exec -it claude-slack-bridge claude`, then `/login`.
That writes `.credentials.json` onto the mounted volume, so it persists too. It
is interactive, and it gives the container a second credential to refresh — but
it keeps the credential in a file rather than in the environment, which matters
below.

**`ANTHROPIC_API_KEY` is not an option here**, although it looks like the obvious
one. The daemon strips it — along with `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` —
from the environment before spawning the CLI, so that a prompt-injected run
cannot exfiltrate it (`_run_claude`, `src/claude_handler.py`). Set it and
Slack → Claude stays unauthenticated with nothing in the log to say why.

`CLAUDE_CODE_OAUTH_TOKEN` is deliberately *not* stripped, because it is the thing
that authenticates the subprocess. So unlike your Slack tokens, it is readable by
the Claude the bridge runs — which runs with `--dangerously-skip-permissions`.
The `/login` route keeps it out of the environment, though a run with those
permissions can still read the file. Treat any Slack channel you allowlist as
trusted with that token.

What does not work, so you don't spend an evening on it: mounting
`~/Library/Keychains/login.keychain-db` (an encrypted store that only `securityd`
can open), and copying the item out with
`security find-generic-password -s "Claude Code-credentials" -w` into
`.credentials.json` — the container would then refresh that copy while your host
CLI refreshes the Keychain one, and a refresh on either side can invalidate the
other.

## Start it

```bash
brew services start claude-slack-bridge
claude-slack-bridge logs
```

The first start builds the image and takes a few minutes. Later ones are a cache
hit and near-instant. Wait for the Socket Mode connection line in the log before
expecting Slack to answer.

If Docker is not running yet, the wrapper waits for it rather than exiting —
which is what you want at login, when launchd starts services before Docker
Desktop has finished coming up.

## The channel → project mapping

`~/.config/claude-slack-bridge/projects.json`, created empty (`{}`) on first
start. It is bind-mounted into the container, so an edit needs no rebuild:

```jsonc
{
  "#api-channel": "/projects/api",
  "#web-channel": { "path": "/projects/web", "plugin_dir": "/projects/web/.claude" }
}
```

Paths are **container** paths under `/projects`, not host paths. Apply an edit
live — the Slack connection stays up and in-flight runs are not killed:

```bash
claude-slack-bridge reload      # → "reloaded N channel(s)"
```

See [reloading-projects.md](reloading-projects.md).

## Per-project `.mcp.json`

Unchanged by this install method — the container name is the same, so the
snippet in the README works as written.

## Day to day

```bash
claude-slack-bridge logs        # follow the container log
claude-slack-bridge reload      # re-read projects.json, live
claude-slack-bridge config      # which config file is in effect
brew services restart claude-slack-bridge
```

### After an upgrade, restart it

Homebrew does not restart services on upgrade. The new code sits in the Cellar
while the old container keeps serving, and nothing says so:

```bash
brew upgrade claude-slack-bridge
brew services restart claude-slack-bridge
```

The restart rebuilds the image from the newly installed tree, which is why the
wrapper runs `up --build` rather than plain `up`.

### Following the dev branch

```bash
brew install --HEAD lexbrugman/tap/claude-slack-bridge
```

Installs the tip of `main` instead of a release. Nothing is compiled either way
— the daemon is built by `docker compose up --build` — so this is only a
different source. A plain `brew upgrade` does not move a `--HEAD` install
forward; `brew upgrade --fetch-HEAD` does.

## Notes

- **Do not use `sudo brew services`.** The container mounts your `~/.claude`; a
  system daemon has the wrong home directory for it.
- **The container's own restart policy is off** under `brew services`. The
  wrapper sets `RESTART_POLICY=no` so launchd is the only thing restarting the
  container — two supervisors racing over one container is how you get a daemon
  that flaps and a `brew services stop` that does not stop anything.
- **Logs** go to `$(brew --prefix)/var/log/claude-slack-bridge.log` (the
  wrapper's own output: waiting for Docker, compose's build and startup).
  `claude-slack-bridge logs` shows the daemon's log inside the container.
