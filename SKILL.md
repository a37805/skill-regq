---
name: skill-regq
description: Query SoC register definitions (Mercury, Venus, etc.) from the internal reg.db file server. Trigger when the user mentions a register symbol, define, bitfield, hex address, or types /regq.
---

# regq — SoC Register Query Skill

## Trigger conditions
- User mentions a register symbol / define (e.g. `VE_WAN_PORT_CTRL`)
- User mentions a hex address (e.g. `0xf4300418`)
- User asks about register, bitfield, or encoding definitions
- User types `/regq`

## Cross-platform design

This skill works identically on **Windows** and **Linux/macOS**. All
OS-specific operations (create cache dir, download files, compare versions,
install Python dependencies) are handled by the bundled **`regq_boot.py`**
script — there is no dependency on `curl`, `wget`, `mkdir -p`, or a particular
shell.

Two rules make every command portable:

1. **Launching Python:** use `python` on Windows, `python3` on Linux/macOS. This
   only matters for the very first call (`regq_boot.py prepare`).
2. **After that, reuse `python_exe`** — the absolute interpreter path that
   `prepare` reports in its JSON. Use it verbatim for every later
   `regq_boot.py` and `regq.py` invocation. This guarantees the same
   interpreter (and its installed deps) is used throughout.

`regq_boot.py` lives in this skill's own directory, next to this `SKILL.md`.
Refer to it by its full path (called `BOOT` below).

## Constants

```
DB_LIST_URL = http://192.168.61.147:28683/regq/db-list.json
CACHE_DIR   = ~/.cache/regq        (resolved cross-platform via Path.home())
BOOT        = <this skill dir>/regq_boot.py
```

To point the skill at a different / test server (e.g. a local `http.server`),
set the `REGQ_BASE_URL` environment variable to that server's `regq/` base URL
before running the bootstrap — no code change or repackaging needed:

```bash
# Windows (PowerShell):  $env:REGQ_BASE_URL = "http://127.0.0.1:28683/regq"
# Linux / macOS:         export REGQ_BASE_URL="http://127.0.0.1:28683/regq"
```

`db-list.json` may use relative URLs (`regq.py`, `dbs/mercury.db`); they are
resolved against `REGQ_BASE_URL`, so one db-list works for every server.
The `prepare` output echoes the effective `base_url`.

---

## Procedure

### Step 1 — Bootstrap (cache + dependencies + db-list + regq.py + SoC detect)

Run the bootstrap. It creates the cache dir, installs any missing Python
packages, fetches `db-list.json` (falling back to cache if the server is down),
updates `regq.py`, and reports which SoC(s) match the current directory:

```bash
python  <BOOT> prepare      # Windows
python3 <BOOT> prepare      # Linux / macOS
```

Parse the JSON printed to stdout. Key fields:

| Field | Use |
|---|---|
| `python_exe` | Absolute interpreter path — **reuse for all later commands** |
| `regq.path` | Path to the cached `regq.py` |
| `socs` | List of `{name, keywords, matched, version, db_cached}` |
| `matched_socs` | SoC names whose keywords appear in the working path |
| `db_list_status` | `ok` / `cache` / `error` |
| `deps` | `{installed, failed, present}` |
| `status` | `ok` / `error` |

Handle the result:
- `status == "error"` → server unreachable **and** no local cache → abort and
  tell the user there is no cache available.
- `db_list_status == "cache"` → warn "regq server unreachable, using local
  cache" and continue.
- `deps.failed` contains `windows-curses` (Windows) → `regq.py` cannot import;
  tell the user to run `python -m pip install windows-curses` manually, then
  stop.
- `deps.failed` contains only `pyserial` / `paramiko` → continue; live UART/SSH
  reading is unavailable until those install.
- `regq.status == "error"` → `regq.py` could not be obtained → abort.

---

### Step 2 — Select the target SoC

Using `matched_socs` / `socs` from Step 1:
- **Exactly one match** → use it; inform the user: "Detected SoC: mercury"
- **Zero matches** → show the full SoC list (names) and ask the user to choose
- **Multiple matches** → show only the matching SoCs and ask the user to choose

Never guess or auto-select when ambiguous. Remember the selection for the rest
of this session; do not ask again.

---

### Step 3 — Ensure the selected SoC DB is up to date

```bash
<python_exe> <BOOT> db --soc <soc>
```

Parse the JSON. Use `db_path` as the database for queries.
- `db_status == "updated"` → tell the user: "Updated <soc>.db → <version>"
- `db_status == "cached"` → use silently
- `status == "error"` → abort and show `message`

---

### Step 4 — Run the query

Use these values from the previous steps:
```
PY   = python_exe        (Step 1)
REGQ = regq.path         (Step 1)
DB   = db_path           (Step 3)
```

All commands below are `<PY> <REGQ> -db <DB> ...` — quote paths that contain
spaces.

**Case A — user gave a symbol or define (exact name):**
```bash
<PY> <REGQ> -db <DB> --search-type symbol --search "<term>"
```
Present results as a table (define + address columns). To drill into one entry,
take its address and run:
```bash
<PY> <REGQ> -db <DB> --address <addr>
```

**Case B — user gave a descriptive keyword (not an exact define):**
```bash
<PY> <REGQ> -db <DB> --search-type keyword --search "<term>"
```
Same as Case A — present results and offer to drill into details.

**Case C — user gave a hex address directly:**
```bash
<PY> <REGQ> -db <DB> --address <addr>
```
Display the full bitfield detail directly.

**Case D — user wants live hardware values (UART or SSH):**

Add the appropriate connection flag. Note the **serial port name differs by
host OS**: `COM3`, `COM4`, … on Windows; `/dev/ttyUSB0`, `/dev/ttyS0`, … on
Linux. (`devmem` always runs on the *target* board, which is Linux regardless.)

```bash
# UART — Windows host
<PY> <REGQ> -db <DB> --address <addr> --uart COM3 --baud 115200
# UART — Linux host
<PY> <REGQ> -db <DB> --address <addr> --uart /dev/ttyUSB0 --baud 115200

# SSH (key auth, default user=root)
<PY> <REGQ> -db <DB> --address <addr> --ssh 192.168.1.1

# SSH (password, custom user/port)
<PY> <REGQ> -db <DB> --address <addr> --ssh 192.168.1.1 --ssh-user admin --ssh-port 2222 --ssh-password mypass
```

For **indirect-access registers** (registers with `indirect=1` attribute, e.g.
table memory), also provide `--index`:
```bash
<PY> <REGQ> -db <DB> --address <addr> --ssh 192.168.1.1 --index 5
```

The output includes a `Live` line with the raw hardware value and a `Value`
column in the bitfield table showing each field's decoded value.

**TUI mode with live reading:**
```bash
<PY> <REGQ> -db <DB> --uart COM3        # Windows
<PY> <REGQ> -db <DB> --ssh 192.168.1.1
```
In the detail view: press `r` to read the register from hardware; press `i` to
enter an index for indirect-access registers, then read.

Dependencies for live reading (`pyserial`, `paramiko`) and for the TUI on
Windows (`windows-curses`) are installed automatically by Step 1. If live
reading still fails due to a missing package, re-run `<PY> <BOOT> prepare`.

---

## Error handling

| Condition | Action |
|---|---|
| Server unreachable, local cache exists | Warn the user, continue with cache (`db_list_status == "cache"`) |
| Server unreachable, no local cache | Abort with a clear message (`status == "error"`) |
| `regq.py` could not be downloaded and none cached | Abort (`regq.status == "error"`) |
| `windows-curses` missing on Windows (regq.py won't import) | `deps.failed` lists it → tell user to `python -m pip install windows-curses`, then stop |
| `pyserial` / `paramiko` failed to install | Continue without live reading; re-run `prepare` to retry |
| `--address` finds no register | Tell the user the address is not in the DB |
| `--search` returns no results | Suggest trying keyword search instead |
| DB download fails | Abort and show the `message` from `db --soc` |
| `--uart` / `--ssh` connection fails | Warn and continue without live reading |
| Indirect register without `--index` | Print note suggesting `--index N` |

---

## Notes

- Never guess the SoC or auto-select when ambiguous — always ask the user
- Do not auto-delete `~/.cache/regq/`; let the user manage the cache manually
- `--address` is an exact match; if not found, do not fall back to a search
- `--uart` and `--ssh` are mutually exclusive
- `regq_boot.py` uses only the Python standard library; `pyserial` / `paramiko`
  are imported lazily by `regq.py` and only for live reads
