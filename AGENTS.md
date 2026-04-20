# AGENTS.md

This repository contains `dn42ctl`, a Python CLI for generating/maintaining DN42-related configuration (Bird/Babel/WireGuard) with state stored in SQLite.

## Quick start (dev)

- Python: **3.11+**
- Recommended runner/env: **uv**
- System dependency: `wg` (wireguard-tools) is required for `bgp peer` / `ibgp peer` / `scan`

Commands (from repo root):

```bash
uv venv
uv pip install -e .
uv run dn42ctl --help
```

Notes:

- Many commands write to `/etc` and `/var/lib` by default (Linux), so they often need `sudo`.
- For development without root, pass `--config-path` / `--db-path` to writable locations.

## Where things live

- CLI entrypoint (Typer): [`src/dn42ctl/cli.py`](src/dn42ctl/cli.py) (script entry: `dn42ctl = dn42ctl.cli:app` in [`pyproject.toml`](pyproject.toml))
- Service layer (reusable business logic): [`src/dn42ctl/services/`](src/dn42ctl/services/)
- Config I/O (TOML): [`src/dn42ctl/config.py`](src/dn42ctl/config.py)
- Default system paths: [`src/dn42ctl/paths.py`](src/dn42ctl/paths.py)
- DB + migrations (SQLite): [`src/dn42ctl/db.py`](src/dn42ctl/db.py), [`src/dn42ctl/migrations.py`](src/dn42ctl/migrations.py)
- Rendering + templates (Jinja2): [`src/dn42ctl/render.py`](src/dn42ctl/render.py), [`src/dn42ctl/templates/`](src/dn42ctl/templates/)
- WireGuard helper (invokes `wg`): [`src/dn42ctl/wg.py`](src/dn42ctl/wg.py)

## Project invariants & pitfalls

- **Routing safety constraint**: `AllowedIPs` must be written, but the tool must **not** auto-modify system routing tables.
  - Details and rationale are documented in the spec: [`docs/spec.md`](docs/spec.md).
- Templates are rendered with **Jinja2 `StrictUndefined`**; missing context variables should be treated as bugs.
- SQLite can store WireGuard private keys; keep permissions restrictive (the code attempts `chmod 0600`).
- The tool targets **Linux** paths and backends (`systemd-networkd` and `NetworkManager`). Avoid introducing Windows-specific assumptions.
- If you use Pylance/pyright strict checking, avoid importing underscore-prefixed (private) helpers across modules (can trigger `reportPrivateUsage`).

## How to extend safely

- Add/change a CLI command: update `src/dn42ctl/cli.py` + implement logic in `src/dn42ctl/services/` (keep CLI thin).
- Change persistent state: add a migration in `src/dn42ctl/migrations.py` (idempotent, versioned).
- Change config outputs: update the corresponding renderer in `src/dn42ctl/render.py` and template(s) together.

## Validation (quick)

```bash
uv run dn42ctl --help
uv run python -m compileall -q src
```

## Documentation (link, don’t duplicate)

- Spec / constraints: [`docs/spec.md`](docs/spec.md)
- Architecture:
  - DB: [`docs/architecture/database.md`](docs/architecture/database.md)
  - Network backends: [`docs/architecture/network_backends.md`](docs/architecture/network_backends.md)
- Command docs: [`docs/commands/`](docs/commands/)
- End-user walkthrough & defaults: [`README.md`](README.md)
