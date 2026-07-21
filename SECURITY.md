# Security

This is a summary of Foundry's security model. The full model — including
component-by-component reasoning — lives in
[`ARCHITECTURE.md` §8](ARCHITECTURE.md#8-security-model), and the detailed
threat model for the Docker execution boundary specifically lives in
[`services/executor/SECURITY.md`](services/executor/SECURITY.md).

## Model summary

- **No host execution.** Agents never get a shell on the host. The only
  host-side artifacts are the Compose stack itself and the bind-mounted
  `projects/` directories.
- **The Docker socket is never exposed to agent-reachable code.**
  `docker-socket-proxy` sits between the real socket and the Executor —
  the only service allowed to talk to it — with `BUILD`, `NETWORKS`,
  `VOLUMES`, `SWARM`, and `SYSTEM` denied. Mount decisions are made by
  Executor code, never by an LLM tool call.
- **Workspace containers are least-privilege**: non-root, `cap_drop=ALL`,
  `no-new-privileges`, an isolated network with no `host` networking,
  explicit CPU/memory limits, and torn down after every task.
- **Secrets** (Telegram bot token, Claude API key, GitHub token) are
  populated once via `./scripts/setup.sh`, stored under `secrets/` (mode
  `600`, gitignored), never logged, and injected only at the tool layer
  that actually needs them — never into agent-visible prompt state.
- **Single-operator authentication.** The Telegram Gateway only responds
  to one allow-listed Telegram user ID, set during setup.
- **Hard-to-undo actions are gated.** Merging into `main` always pauses
  for human approval via Telegram, with no bypass flag. The Claude API
  weekly spend threshold is the only other hard gate; everything else
  (feature-branch commits, file edits within a project's own mount,
  dependency installs inside workspace containers) runs autonomously.

## Reporting a vulnerability

This project doesn't yet have a formal disclosure process or a bug
bounty — it's a single-operator, self-hosted system. If you find a
security issue, please reach out via [GitHub](https://github.com/snipa14)
rather than filing a public issue, so it can be addressed before details
are public.
