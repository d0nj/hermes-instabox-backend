# hermes-instabox-backend

An [InstaBox](https://instabox.sh) terminal backend for [Hermes Agent](https://hermes-agent.nousresearch.com).
The terminal, file and code tools run inside a cloud Linux machine (Firecracker microVM, Debian 13)
that belongs to the agent — not on the host running Hermes.

InstaBox gives each agent a machine with 4 vCPU / 8 GiB, a 16 GiB root disk and a persistent 20 GiB
disk at `/data` (home is `/data/home`), reachable over an HTTPS API with `exec` and file endpoints.

## Requirements

- Hermes Agent **0.21.6+** (the terminal-environment plugin API).
- An InstaBox account and an API key (`ibk_...`): <https://dashboard.instabox.sh/dashboard/api-keys>.
- No Python dependencies: the API client is stdlib-only.

## Install

```bash
git clone https://github.com/d0nj/hermes-instabox-backend
ln -s "$PWD/hermes-instabox-backend/instabox" ~/.hermes/plugins/instabox   # or copy the folder
hermes plugins enable instabox
echo 'INSTABOX_API_KEY=ibk_...' >> ~/.hermes/.env
hermes config set terminal.backend instabox
hermes doctor          # shows the InstaBox rows
```

`hermes config set terminal.backend instabox` switches the whole agent over; per-run, `TERMINAL_ENV=instabox`
does the same for one process.

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `INSTABOX_API_KEY` | — | API key, in the environment or the profile's `.env` (never `config.yaml`). |
| `terminal.backend` | `local` | Set to `instabox` to select this backend. |
| `terminal.instabox_template` | `agent-browser` | Template to create from: `agent-browser` (with desktop) or `universal` (without). |
| `terminal.instabox_snapshot` | — | Start new machines from this snapshot (name or `sn_...` ID) instead of a template. |
| `terminal.instabox_machine` | — | Use this existing machine (name or `m_...` ID) instead of creating one. |
| `terminal.instabox_auto_suspend` | `1800` | Idle seconds before InstaBox pauses the machine; `0` disables it on paid plans. |
| `terminal.instabox_file_sync` | `false` | Mirror the host's `~/.hermes` (skills, cache, credential files) into the machine. |
| `terminal.instabox_api_url` | `https://api.instabox.sh` | API endpoint (also `INSTABOX_API_URL` for tests). |
| `terminal.container_persistent` | `true` | Reuse one machine per task and pause it when idle; `false` creates and deletes one. |

`terminal.container_cpu` / `_memory` / `_disk` and `image=` overrides are **ignored**: InstaBox sizes
are fixed per plan and there are no custom images.

## Starting from a snapshot

A snapshot is a machine's installed software plus everything under `/data` — home, repos, saved
sign-ins (`gh`, git, SSH, agents). Save one, then point the backend at it:

```bash
instabox snapshot save dev --name dev-base     # or "Save snapshot" in the dashboard
hermes config set terminal.instabox_snapshot dev-base
```

- Accepts the snapshot **name** or its `sn_...` ID; it is resolved before any machine is created.
- The snapshot must be **`ready`**. A running machine's save is `pending` until it uploads, and the
  backend says so instead of creating a machine from it.
- A snapshot **wins over** `terminal.instabox_template`: InstaBox takes `snapshotId` or
  `templateId`, never both.
- It applies to **newly created** machines. An existing `hermes-<task>` machine is reused as it is —
  delete it first (`instabox rm hermes-default`) or use a new task id to get one built from the
  snapshot.
- The snapshot decides the guest user; set `terminal.instabox_guest_user` only for a user that
  exists inside it.
- Saving happens in the InstaBox CLI or dashboard, not here; snapshots count against the plan cap
  (free 5, pro 100).

## Using an existing machine

`terminal.instabox_machine` points the backend at a machine you already have instead of creating
one — the practical mode on the free plan, where the single-machine cap is often already taken:

```bash
instabox ls                                              # names and states
hermes config set terminal.instabox_machine base-dev     # name or m_... ID
```

- No machine is created, and an existing `hermes-<task>` machine is not looked up.
- The machine is left as it was found: a suspended machine is resumed for the commands and
  suspended again on cleanup; a running one is left running.
- `terminal.instabox_snapshot` and `terminal.instabox_template` do not apply in this mode.
- A missing name fails with a message pointing at `instabox ls`.

> **Prefer the plugin settings for these keys.** `hermes config set` rewrites the `terminal:`
> section and drops `terminal.instabox_*` keys it does not recognize, so a later config change can
> silently remove `instabox_machine`. `plugins.entries.instabox.settings.*` survives that and is
> read by the same code (the `terminal.*` key just wins when both are present).

## Plans and limits

The backend is built around InstaBox's per-plan limits rather than assuming an unlimited account.

| | Free | Pro ($20/mo + usage) | Team ($500/mo + usage) |
| --- | --- | --- | --- |
| Machines | **1** | 500 (100 running) | unlimited (500 running) |
| Snapshots | 5 | 100 | — |
| Templates | `agent-browser` or a snapshot only | any | any |
| Sizes (vCPU/GiB) | 4/8 | 2/4, 4/8, 8/16 | 2/4 … 32/64 |
| Idle pause | fixed 30 min | 5–30 min, or off | 5–30 min, or off |

How that shows up in the backend:

- **Startup is measured.** The backend logs `InstaBox: <machine> ready in Xs (attach, ready, home,
  sync, session)` on every environment creation, so a slow first command is diagnosable.
- **Attach or create.** `terminal.instabox_machine` uses one machine you name;
  otherwise a task gets its own machine, reused by name. A persistent environment attaches to `hermes-<task>`
  (task ids come from Hermes: `default` for an interactive session, one per batch prompt or
  subagent). On the free plan, use the default `container_persistent: true` and avoid parallel
  batch runs — each distinct task id wants its own machine.
- **`429 quota_exceeded` is handled, not fatal.** A persistent environment adopts an existing
  machine labelled `hermes: terminal-backend` (running first, then paused) and keeps working, with a
  warning in the log. An ephemeral environment never adopts — it would delete a machine another
  session is using — and instead fails with the limit and the two ways out (`instabox rm <name>`, or
  `terminal.container_persistent: true`).
- **Free-plan template restriction** (`403 template_not_allowed`) is reported with the fix
  (`terminal.instabox_template agent-browser`) instead of a raw API error.
- **Idle pause defaults to 1800s**, which is exactly what the free plan enforces anyway and a valid
  Pro/Team value. Suspended machines keep their memory and files and are resumed on the next command.
- **Sizes are not requested**, so a create can never hit `spec_not_allowed`; the machine gets the
  plan's default size.
- **Snapshots are first-class** (`terminal.instabox_snapshot`) and count against the plan cap
  (free 5, pro 100). They are also the way a free workspace starts from anything other than
  `agent-browser`: a `snapshotId` is allowed where a non-`agent-browser` template is not.
- **Concurrency**: the API allows 32 in-flight requests per user across sandboxes; `exec` retries a
  retryable `429` once. Machines run one lifecycle operation at a time, and the backend waits out
  `operationId` instead of racing it.

## How it works

- `POST /v1/machines` returns an *operation*; the backend follows it with long-poll
  `GET /v1/operations/{id}?wait=25s` until it succeeds.
- A configured snapshot is resolved by name or ID and must be `ready`; the create then sends
  `snapshotId` and no `templateId`.
- Every command is one `POST /v1/machines/{id}/exec` with `bash -lc`, capped at the API's 600 s.
  stderr is merged with `exec 2>&1` at the source so ordering survives, and the session snapshot,
  cwd markers and file tools come from Hermes' `BaseEnvironment`.
- `stdin` up to 1 MiB rides in the request body; larger payloads are staged as a file over
  `PUT /v1/machines/{id}/files` and redirected.
- Interrupting a command sends a second exec that TERMs/KILLs the recorded shell and its children;
  the original request is abandoned either way.
- `~/.hermes` is mirrored with Hermes' `FileSyncManager` (`upload`, `rm`, tar download), so skills,
  cache files and credential files are present in the machine. The first command of a session
  uploads whatever changed on the host: a typical install is ~700 files / 6 MB, the API takes one
  file per request at ~0.6 s, so it is **off by default** — enable `instabox_file_sync` only when
  the machine needs host skills/credentials (one `.tar.gz` upload + one extract).
- Cleanup pauses a persistent machine (`suspend`, memory kept) or deletes an ephemeral one.

Not supported: `supports_streams` is off, so Bot Desktop, `computer_use` and the browser stay on the
host — the exec API is request/response, with no long-lived stdin/stdout channel.

## Performance

Measured against the live API from a European host:

| | |
| --- | --- |
| cold environment build (attach + resume + `$HOME` + session snapshot) | ~2.5 s |
| cold build with `file_sync` on (738 files) | ~6.6 s |
| command round trip once warm | ~0.8 s |
| one file `PUT` | 0.6 s |
| mirroring a typical `~/.hermes` (738 files / 6 MB) | **3.0 s** — one 1.8 MiB `.tar.gz` upload + one extract (was ~7.6 min at one request per file) |

That last row is why `instabox_file_sync` is **off by default**. Every environment build logs
`InstaBox: <machine> ready in Xs (attach, ready, home, sync, session)`, so a slow start is
diagnosable from `~/.hermes/logs/agent.log`.

## Tests

```bash
python -m pytest tests -q          # from the repo root, with Hermes importable
```

`tests/mock_api.py` is a local fake of the InstaBox API that really runs `bash -lc` inside a
throwaway machine root, so the suite covers exec, cwd persistence, stdin, timeouts, persistence,
ephemeral delete, quota adoption and file sync without an InstaBox account. Set `HERMES_AGENT_ROOT`
if Hermes is not installed in the active environment. `tests/serve_mock.py --port 8787` runs the
fake on a fixed port for a manual end-to-end check.

## License

MIT — see [LICENSE](LICENSE).
