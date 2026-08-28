# Kyiv Monitor — Air Alerts and News in English

Kyiv Monitor turns fast-moving Ukrainian-language Telegram reporting into timely,
focused English updates for people following Kyiv and Ukraine. A continuously running
Python worker watches a curated set of sources, separates urgent air-alert information
from general news, filters promotional and non-operational content, and publishes to
two purpose-built Telegram destinations:

- **Kyiv Air Alert** — a quiet, alert-only channel that becomes active when Kyiv is
  under an air alert and publishes concise real-time updates and the all-clear;
- **Kyiv Hourly News 🇺🇦** — a separate news group with categorized hourly summaries
  and a morning recap after the overnight pause.

Public output is produced in English. The service is informational and is not a
replacement for official Ukrainian civil-defence instructions, sirens, or local
emergency-alert applications.

## Telegram channels

Each source has one defined role; trigger messages, real-time alert updates, and
general news are never mixed blindly into the same pipeline.

| Role | Telegram source | Use |
| --- | --- | --- |
| Kyiv local news | `@kievinfo_kyiv` | City services, infrastructure and events affecting daily life in Kyiv |
| Ukraine news | `@shv_ukr` | Political, economic, diplomatic and national developments |
| Military monitoring | `@AMK_Mapping` | Relevant military and strategic developments |
| Additional news | `@insiderukr` | Additional Ukrainian current-affairs reporting for the hourly analysis |
| Alert-state trigger | `@kyiv_airraid_alert` | Explicit Kyiv alert/all-clear state only; never used as news content |
| Live alert feed | `@kyivnebomonitoring` | Actionable real-time updates translated and published only while ALERT is active |

The output destinations are configured through `TARGET_CHAT_ID` for **Kyiv Air Alert**
and `SUMMARY_CHAT_ID` for **Kyiv Hourly News 🇺🇦**. Their Telegram IDs and private
operational chats are kept in the deployment environment, never in the repository.

## Operating modes

### NORMAL

Messages from the four NORMAL sources are persisted in SQLite and evaluated across
the Kyiv City, Ukrainian National Developments, Military Developments and Air Defence
Monitoring categories. Selected updates are summarized hourly. Between 01:00 and
07:00 Europe/Kyiv, hourly publishing is paused and the accumulated material is used
for the morning recap.

### ALERT

The explicit Kyiv state from `@kyiv_airraid_alert` controls the mode. While active,
actionable new messages and edits from `@kyivnebomonitoring` enter the low-latency
translation pipeline. NORMAL summaries are suspended, alert transitions are
serialized, public delivery must be confirmed before the state is committed, and
stale translations are discarded after the alert ends.

### TEST

`TEST_MODE=true` disables real-source handlers and confines alert and summary output
to `TEST_CHAT_ID`; operational failures may still be reported to `OPS_CHAT_ID`. By
default it does not open `TELEGRAM_SESSION`, preventing staging from invalidating the
production authorization key. Interactive test commands require a separate
`TEST_TELEGRAM_SESSION`; never reuse the production session. Available commands are
`/test_start`, `/test_message`, `/test_burst N`, `/test_end`, and `/test_summary`.

## Architecture

```text
NORMAL sources ── Telethon ── SQLite queue ── Anthropic ── Kyiv Hourly News
                                   │
@kyiv_airraid_alert ── state ──────┤
                                   │
@kyivnebomonitoring ── filter ── translation ── Kyiv Air Alert
```

- `monitor.py`: orchestration, pipelines and integrations.
- `alert_rules.py`: pure trigger classification with no network or global state.
- `text_processing.py`: deterministic cleaning, filtering and translation helpers.
- `state_store.py`: SQLite persistence, cursors, delivery claims and session locking.
- `tests/`: routing, persistence, restart and transition regression tests.
- `docs/`: detailed architecture, operations and historical test log.

The project is being separated incrementally rather than rewritten, keeping external
behaviour covered by tests at each step.

## Configuration

Copy `.env.example` and provide real values through the deployment environment.
Never commit credentials or a Telethon session string.

Required in production:

- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`;
- `ANTHROPIC_API_KEY`, `BOT_TOKEN`;
- `TARGET_CHAT_ID`, `SUMMARY_CHAT_ID`, `SUMMARY_CHAT_LINK`;
- `OWNER_CHAT_ID`; optionally a distinct `OPS_CHAT_ID`;
- optionally `ADMIN_USER_IDS`, a comma-separated allowlist for manual overrides. If
  omitted, only `OWNER_CHAT_ID` is authorized.

`TARGET_CHAT_ID` and `SUMMARY_CHAT_ID` must be different. `SUMMARY_CHAT_LINK` must
point to the news group, not the alert channel.

## Local verification

The tests stub external services and do not send Telegram or Anthropic requests.

```bash
python3 -m pip install -r requirements.txt
python3 -m py_compile monitor.py alert_rules.py state_store.py text_processing.py predeploy_check.py
python3 -m unittest discover -s tests -v
```

Lint locally with [ruff](https://docs.astral.sh/ruff/) (`pip install ruff==0.16.4`, config in
`pyproject.toml`):

```bash
ruff check .
```

`requirements.txt` lists only the direct dependencies (`telethon`, `httpx`) and stays
the file to edit when adding or upgrading a dependency. `requirements-lock.txt` pins
the full resolved tree, including transitive packages like `pyaes`, `rsa` and
`httpcore`. `.github/workflows/deploy-production.yml` sets it as Railway's build
install command (`RAILPACK_INSTALL_CMD`) before each deploy, and CI installs from it
too, so every environment runs the exact versions that were tested. Regenerate it
after changing `requirements.txt`:

```bash
python3 -m venv /tmp/lockenv && source /tmp/lockenv/bin/activate
pip install -r requirements.txt
pip list --format=freeze > requirements-lock.txt
deactivate
```

## Deployment safety

GitHub Actions runs syntax checks and the complete test suite for pull requests and
pushes to `main`. Configure GitHub branch protection so `main` requires the `test`
check and at least one review. Railway should deploy production only from protected
`main`; use a separate Railway service/environment with `TEST_MODE=true` for staging.

`railway.json` runs `predeploy_check.py` before the new worker starts. It rejects
missing or malformed production configuration—including an invalid Telethon
StringSession—without printing secret values. The deployment workflow currently stops
the active worker before invoking `railway up`, so a failed pre-deploy check requires
manual recovery or rollback; it does not guarantee uninterrupted service. Failed
workers are limited to three restart attempts instead of looping indefinitely.

SQLite and the Telethon lock default to paths under `/data`. They survive deployments
only when Railway has a persistent volume mounted there; without one, they are local
to the container. Independently, the deployment workflow stops the active worker and
waits 15 seconds before starting its replacement, preventing simultaneous use of
`TELEGRAM_SESSION` during the normal deployment path.

Do not test manual alert commands in the production channel. Verify the candidate
commit in staging, merge only after green checks, and retain the preceding Railway
deployment for rollback.

## Manual overrides

Only Telegram user IDs listed in `ADMIN_USER_IDS` may execute `/alert` or `/normal`;
when the variable is omitted, the allowlist contains only `OWNER_CHAT_ID`. Rejected
attempts are logged and reported to Ops. The canonical automatic state continues to
come from `@kyiv_airraid_alert`.

## Further documentation

- [`docs/PROJECT_ARCHITECTURE.md`](docs/PROJECT_ARCHITECTURE.md)
- [`docs/SELF_HEALING_LOOPS.md`](docs/SELF_HEALING_LOOPS.md)
- [`NEXT_STEPS_AND_IMPLEMENTATIONS.md`](NEXT_STEPS_AND_IMPLEMENTATIONS.md)
