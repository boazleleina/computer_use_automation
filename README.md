# computer-use-automation

Turns legacy UI flows into callable, replayable capabilities, so an agent can
use bank software that offers no API.

**Discovery** runs an LLM loop once against a live UI and writes down the
procedure it found. **Replay** executes that written-down procedure with no
model in the loop, thousands of times a day.

The point of the split is not cost or speed. It is that you cannot audit a
decision that has not been made yet. The artifact moves the decision from
runtime to review time, where a human approves it once and a compliance officer
can read it later.

## Status

Phase 0: scaffolding and contracts. Port signatures exist; nothing executes yet.

## Layout

```
src/cua/domain/      business rules, pure Python, no I/O
src/cua/ports/       the six interfaces the domain declares
src/cua/adapters/    technology: browser, model SDK, filesystem
src/cua/app/         use cases; sequence, not decisions
src/cua/composition.py   the only module that imports adapters
src/cua/cli.py       argument parsing and exit codes
```

## Develop

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # then set TARGET_APP_PASSWORD

.venv/bin/pytest
.venv/bin/mypy src target_app
.venv/bin/ruff check .
```

No credential is committed. `.env.example` documents the variables; the fixture
app refuses to sign anyone on until `TARGET_APP_PASSWORD` is set. That password
is never given to the automation — a human types it during handoff.

## Legacy target app

```bash
.venv/bin/python -m target_app.app     # http://127.0.0.1:5000
```

## Use

Not yet implemented. Both subcommands parse and exit 3.

```bash
cua discover --goal "look up a member balance" --name lookup_member_balance
cua replay --name lookup_member_balance --version 1 --input member_id=12345
```
