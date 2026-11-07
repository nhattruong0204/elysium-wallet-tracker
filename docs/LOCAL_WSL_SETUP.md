# Isolated WSL development and tests

Validated on Ubuntu 24.04 / WSL2, Python 3.12, PostgreSQL 16, November 7, 2026.
Project minimum is Python 3.10; CI uses 3.12. Node is not required by the Python
Playwright tests. Docker and authenticated `gh` were already available. No production
credentials, databases, bot processes or deployment automation are needed.

```bash
uv venv --python python3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt \
  pytest playwright ruff mypy types-requests types-PyYAML pip-audit
```

Copy `.env.example` to `.env` (mode 0600, ignored by Git), replace its database password
with locally generated random hex, and leave **TG_BOT_TOKEN and TG_CHAT_ID empty**.
Add local-only URLs; use your generated password in place of LOCAL_PASSWORD:

```dotenv
DATABASE_URL=postgresql+psycopg://tracker:LOCAL_PASSWORD@127.0.0.1:55482/tracker_dev
TEST_DATABASE_URL=postgresql+psycopg://tracker:LOCAL_PASSWORD@127.0.0.1:55482/tracker_test
```

With the local environment exported, start only the disposable database container:

```bash
set -a
source .env
set +a
docker run -d --name elysium-issue82-local-pg --label elysium.scope=issue82-local \
  -p 127.0.0.1:55482:5432 -e POSTGRES_USER=tracker -e POSTGRES_DB=tracker_dev \
  -e POSTGRES_PASSWORD postgres:16-alpine
docker exec elysium-issue82-local-pg pg_isready -U tracker
# Run the next commands once readiness succeeds.
docker exec elysium-issue82-local-pg createdb -U tracker tracker_test
docker exec elysium-issue82-local-pg psql -U tracker -d tracker_test \
  -c 'ALTER DATABASE tracker_test SET jit=off'
cp config.example.yaml config.yaml
cp watchlist.example.txt watchlist.txt
chmod 600 config.yaml watchlist.txt
```

Set `leaderboard.enabled`, `market.enabled`, `ui.enabled` and `collector.enabled` false
in local config, plus `intel.paper_enabled`, `intel.dd_enabled`, `intel.launch_watch`,
`intel.new_launchpad_alerts`, `intel.ascend_index` false, and
`trading: {paper_enabled: false}`. Scanner startup is controlled by the CLI; no
background application process is started by these setup commands. Do not start the scanner, UI,
collector, notifier, production compose stack or any bot. The CLI migration command
below connects only to `tracker_dev`; tests destroy only `tracker_test`.
The database container has no persistent volume; deleting it deletes this local data.

```bash
.venv/bin/python -m agora_tracker --config config.yaml --migrate
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python -m pytest -q tests/test_trade_rpc_bench.py tests/test_tracker.py
.venv/bin/ruff check .
.venv/bin/mypy agora_tracker
.venv/bin/pip-audit -r requirements.txt
.venv/bin/python -m pytest -q -rs
```

Never run two integration suites against this database at once. The installed PostgreSQL
16 `pg_dump` and `pg_restore` cover backup/restore tests. Browser tests must actually run,
not silently skip for missing binaries/libraries.

## Without sudo for browser libraries

The normal `--with-deps` installer needs sudo. In this WSL session Chromium downloaded
successfully but lacked NSPR/NSS/ALSA. The following user-local extraction worked without
changing system packages (check `ldd` for other missing libraries on a different host):

```bash
.venv/bin/python -m playwright install chromium
mkdir -p .local/browser-debs .local/browser-libs
(cd .local/browser-debs && apt-get download libnspr4 libnss3 libasound2t64 && \
  for f in *.deb; do dpkg-deb -x "$f" ../browser-libs; done)
export LD_LIBRARY_PATH="$PWD/.local/browser-libs/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
.venv/bin/python -m pytest -q -rs
```

Exclude `/.local/` in `.git/info/exclude`; never commit downloaded packages or local env.
Do not print `.env`, shell traces, authentication tokens or database URLs with passwords.
The production compose file is deliberately unused for this setup. Feature-branch pushes
run PR CI; never change `deploy/OPS_REQUEST`, push to `ops`, merge or deploy as setup.
