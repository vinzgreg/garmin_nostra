# garmin-nostra — project instructions for Claude

## What this project is
A Dockerised Python service that syncs Garmin Connect activities for multiple
users. For each new activity it stores metrics in SQLite, downloads GPX/FIT
files, renders a map PNG (OSM tiles), posts a Mastodon mention, and optionally
pushes a CalDAV event to Nextcloud. All user-facing text is in **German** with
metric units.

See `garmin-sync/README.md` for the full feature list and file map.

---

## Code quality rules — apply these when writing or modifying any code

### Docker / containers
- Containers must **always run as a non-root user**. Add `RUN useradd ...` and
  `USER appuser` to every Dockerfile. Never rely on cron (requires root);
  use a Python sleep loop or similar instead.
- Data directories are bind-mounted from the host. Document the required UID
  and provide a `chown` command in the README when changing the runtime user.

### Python — resource cleanup
- Always use `finally` (or a context manager) to shut down
  `ThreadPoolExecutor` instances. A bare `try/except` that returns or raises
  inside the `try` block will leak threads.

### Python — timeouts and threading
- Never use `signal.SIGALRM` for timeouts. It only works on the main thread
  and breaks if the call is ever moved to a worker. Use `ThreadPoolExecutor`
  with `future.result(timeout=...)` instead — the same pattern used for GPX
  and FIT downloads.
- `socket.setdefaulttimeout()` is a process-global safety ceiling, not a
  per-request timeout. Set it to at least `4×` the per-client timeout so it
  doesn't interfere with legitimate slow operations (large downloads).

### Python — SQLite
- Always enable WAL mode immediately after opening a connection:
  `conn.execute("PRAGMA journal_mode=WAL")`. This allows concurrent readers
  (e.g. manual `sqlite3` shell queries) without blocking writers.
- Wrap the PRAGMA in try/except so a read-only filesystem doesn't crash
  startup.

### Python — secrets and configuration
- Never require secrets to be hardcoded in config files. Support an `env:`
  prefix that resolves to an environment variable at load time, e.g.
  `mastodon_access_token = "env:MASTODON_TOKEN"`. Document this in the
  example config and in `docker-compose.yml`.

### Shell scripts
- Never interpolate shell variables into inline Python source strings. Pass
  values as `sys.argv` arguments instead:
  ```bash
  # Wrong — shell injection risk if path contains single quotes
  python3 -c "open('${CONFIG_FILE}')"

  # Right
  python3 -c "import sys; open(sys.argv[1])" "$CONFIG_FILE"
  ```

### Cached connections
- Stateful connection objects (CalDAV sessions, DB handles) that are cached
  across calls must clear themselves on connection errors so the next call
  reconnects instead of failing permanently. Pattern:
  ```python
  except (OSError, ConnectionError) as exc:
      self._connection = None   # force reconnect next time
      raise
  ```

### Cron expressions
- The minutes field of a cron expression only accepts 0–59. An interval such
  as `*/120` is silently ignored by most cron daemons. Validate or convert
  intervals > 59 minutes before writing a crontab. (Prefer a sleep loop over
  cron to avoid this entirely.)

### User-facing strings
- All messages, log lines directed at end users, CalDAV descriptions, and
  Mastodon posts must be in **German**.

---

## What NOT to do
- Do not add docstrings, comments, or type annotations to code that wasn't
  changed.
- Do not introduce backwards-compatibility shims for removed code.
- Do not over-engineer: three similar lines is better than a premature
  abstraction.
