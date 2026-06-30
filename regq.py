#!/usr/bin/env python3
"""regq.py — Interactive register query CLI for reg.db (curses TUI)"""

import curses
import re
import shutil
import sqlite3
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Console classes (UART / SSH) — inlined from uart_console.py
# ─────────────────────────────────────────────────────────────────────────────

_HEX_LINE_RE_CON = re.compile(r'^0x[0-9a-fA-F]+$')

try:
    import serial as _serial
    _SERIAL_OK = True
except ImportError:
    _SERIAL_OK = False

try:
    import paramiko as _paramiko
    _PARAMIKO_OK = True
except ImportError:
    _PARAMIKO_OK = False


class _ConsoleBase:
    def connect(self) -> bool:              raise NotImplementedError
    def disconnect(self):                   raise NotImplementedError
    def is_connected(self) -> bool:         raise NotImplementedError
    def devmem_read(self, addr: int) -> Optional[int]:   raise NotImplementedError
    def devmem_write(self, addr: int, value: int) -> bool: raise NotImplementedError


class UartConsole(_ConsoleBase):
    """UART serial connection to a Linux shell; uses devmem for register access."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 3.0):
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._ser = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return _SERIAL_OK

    def connect(self) -> bool:
        if not _SERIAL_OK:
            return False
        try:
            self._ser = _serial.Serial(
                self._port, self._baudrate,
                timeout=self._timeout, write_timeout=2.0,
            )
            self._ser.reset_input_buffer()
            time.sleep(0.05)
            self._ser.write(b'\r\n')
            self._ser.flush()
            time.sleep(0.2)
            self._ser.reset_input_buffer()
            return True
        except Exception:
            self._ser = None
            return False

    def disconnect(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def devmem_read(self, addr: int) -> Optional[int]:
        with self._lock:
            if not self.is_connected():
                return None
            cmd = f'devmem 0x{addr:08x}\r\n'.encode()
            try:
                self._ser.reset_input_buffer()
                self._ser.write(cmd)
                self._ser.flush()
                buf = b''
                deadline = time.monotonic() + self._timeout
                while time.monotonic() < deadline:
                    waiting = self._ser.in_waiting
                    chunk = self._ser.read(waiting if waiting > 0 else 1)
                    if chunk:
                        buf += chunk
                    for line in buf.decode('latin-1', errors='replace').splitlines():
                        if _HEX_LINE_RE_CON.match(line.strip()):
                            return int(line.strip(), 16)
                return None
            except Exception:
                return None

    def devmem_write(self, addr: int, value: int) -> bool:
        with self._lock:
            if not self.is_connected():
                return False
            cmd = f'devmem 0x{addr:08x} 32 0x{value:08x}\r\n'.encode()
            try:
                self._ser.reset_input_buffer()
                self._ser.write(cmd)
                self._ser.flush()
                time.sleep(0.15)
                return True
            except Exception:
                return False


class SshConsole(_ConsoleBase):
    """SSH connection to a Linux shell; uses devmem via exec_command."""

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str = 'root',
        password: Optional[str] = None,
        key_filename: Optional[str] = None,
        timeout: float = 5.0,
    ):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._key_filename = key_filename
        self._timeout = timeout
        self._client = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return _PARAMIKO_OK

    def connect(self) -> bool:
        if not _PARAMIKO_OK:
            return False
        try:
            client = _paramiko.SSHClient()
            client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
            if self._password is None and self._key_filename is None:
                # No credentials provided — try "none" auth (open embedded boards)
                t = _paramiko.Transport((self._host, self._port))
                t.start_client(timeout=self._timeout)
                remaining = t.auth_none(self._username)
                if remaining:
                    t.close()
                    raise Exception(f"none auth not sufficient; server wants: {remaining}")
                client._transport = t
            else:
                client.connect(
                    self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    key_filename=self._key_filename,
                    timeout=self._timeout,
                    allow_agent=True,
                    look_for_keys=True,
                )
            self._client = client
            return True
        except Exception as _e:
            self._client = None
            self._last_error = str(_e)
            return False

    def disconnect(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def is_connected(self) -> bool:
        return (self._client is not None
                and self._client.get_transport() is not None
                and self._client.get_transport().is_active())

    def devmem_read(self, addr: int) -> Optional[int]:
        with self._lock:
            if not self.is_connected():
                return None
            try:
                _, stdout, _ = self._client.exec_command(
                    f'devmem 0x{addr:08x}', timeout=self._timeout
                )
                line = stdout.read().decode('latin-1', errors='replace').strip()
                if _HEX_LINE_RE_CON.match(line):
                    return int(line, 16)
                return None
            except Exception:
                return None

    def devmem_write(self, addr: int, value: int) -> bool:
        with self._lock:
            if not self.is_connected():
                return False
            try:
                _, stdout, _ = self._client.exec_command(
                    f'devmem 0x{addr:08x} 32 0x{value:08x}', timeout=self._timeout
                )
                stdout.channel.recv_exit_status()
                return True
            except Exception:
                return False

DEFAULT_DB = Path(__file__).parent / "mercury/asic/reg.db"

# ─────────────────────────────────────────────────────────────────────────
# Color pair IDs
# ─────────────────────────────────────────────────────────────────────────
CP_SEL    = 1   # highlighted / selected row
CP_HEADER = 2   # column headers
CP_ADDR   = 3   # address values
CP_TITLE  = 4   # screen title bar
CP_FOOTER = 5   # footer bar
CP_DIM    = 6   # subdued text
CP_LIVE   = 7   # live value from hardware (green)


def _init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(CP_SEL,    curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(CP_HEADER, curses.COLOR_YELLOW, -1)
    curses.init_pair(CP_ADDR,   curses.COLOR_CYAN,   -1)
    curses.init_pair(CP_TITLE,  curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(CP_FOOTER, curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(CP_DIM,    curses.COLOR_WHITE,  -1)
    curses.init_pair(CP_LIVE,   curses.COLOR_GREEN,  -1)


def _draw_bar(win, y, text, pair):
    _, w = win.getmaxyx()
    try:
        win.addstr(y, 0, text.ljust(w)[:w], curses.color_pair(pair))
    except curses.error:
        pass


# ─────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────

def _map_path(conn, map_id):
    cur = conn.execute(
        """
        WITH RECURSIVE t(id, path) AS (
            SELECT id, name FROM maps WHERE belongs_to = -1
            UNION ALL
            SELECT m.id, t.path || '/' || m.name
            FROM maps m JOIN t ON m.belongs_to = t.id
        )
        SELECT path FROM t WHERE id = ?
        """,
        (map_id,),
    )
    row = cur.fetchone()
    return row[0] if row else "(unknown)"


def _addresses(conn, reg_id):
    cur = conn.execute(
        "SELECT value FROM addresses WHERE belongs_to = ? ORDER BY id", (reg_id,)
    )
    return [f"0x{int(r[0]):x}" for r in cur.fetchall()]


def _bitfields(conn, reg_id):
    cur = conn.execute(
        "SELECT id, name, type, hi, lo, def, comment FROM bitfields "
        "WHERE belongs_to = ? ORDER BY hi DESC",
        (reg_id,),
    )
    return cur.fetchall()


_BITRANGE_RE = re.compile(r'^\d+(?::\d+)?$')


def _bf_structs(conn, bf_id):
    """Return list of (type, header, data_rows) for a bitfield."""
    result = []
    cur = conn.execute(
        "SELECT id, type FROM structs WHERE belongs_to = ?", (bf_id,)
    )
    for s_id, s_type in cur.fetchall():
        cur2 = conn.execute(
            "SELECT id FROM struct_rows WHERE belongs_to = ? ORDER BY id", (s_id,)
        )
        all_rows = []
        for (row_id,) in cur2.fetchall():
            cur3 = conn.execute(
                "SELECT value FROM row_items WHERE belongs_to = ? ORDER BY id",
                (row_id,),
            )
            all_rows.append([r[0] or "" for r in cur3.fetchall()])
        if all_rows:
            first = all_rows[0]
            # If the first cell looks like a bit-range (e.g. "79:72"), the table
            # has no explicit header row — all rows are data.
            if first and _BITRANGE_RE.match(first[0].strip()):
                result.append((s_type, [], all_rows))
            else:
                result.append((s_type, first, all_rows[1:]))
    return result


_BF_ACCESS: dict[str, str] = {
    # Read-only
    "sample":                    "R",
    "fixed":                     "R",
    "status":                    "R",
    "sampleInterrupt":           "R",
    "internalSampleInterrupt":   "R",
    # Read/Write
    "configuration":             "RW",
    "mask":                      "RW",
    "enable":                    "RW",
    "external":                  "RW",
    "inc":                       "RW",
    "incCfg":                    "RW",
    # Write-1
    "isas":                      "W1",
    # Read/Write, Write-1-to-Clear
    "riseInterrupt":             "RW1C",
    "fallInterrupt":             "RW1C",
    "bothInterrupt":             "RW1C",
    "levelInterrupt":            "RW1C",
    "interrupt":                 "RW1C",
    "externalRiseInterrupt":     "RW1C",
    "externalFallInterrupt":     "RW1C",
    "externalBothInterrupt":     "RW1C",
    "externalLevelInterrupt":    "RW1C",
    "externalInterrupt":         "RW1C",
    # Clear on read
    "sampleClear":               "RC",
    "incSatClr":                 "RC",
    "incClr":                    "RC",
    # Write, self-clear
    "selfClear":                 "RWSC",
}


def _bf_access(type_str: str) -> str:
    return _BF_ACCESS.get(type_str or "", type_str or "—")


def _fmt_default(val):
    if not val:
        return "—"
    m = re.match(r"\d+'([bBoOhHdD])([\w_]+)$", val)
    if m:
        base_char, digits = m.group(1).lower(), m.group(2).replace("_", "")
        base = {"b": 2, "o": 8, "d": 10, "h": 16}.get(base_char, 16)
        try:
            n = int(digits, base)
            return "0" if n == 0 else f"0x{n:x}"
        except ValueError:
            pass
    return val


def _trunc(s, n):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _enrich(conn, rows):
    out = []
    for r in rows:
        addrs = _addresses(conn, r[0])
        out.append(r + (addrs[0] if addrs else "—",))
    return out


# ─────────────────────────────────────────────────────────────────────────
# UART / live-value helpers
# ─────────────────────────────────────────────────────────────────────────

def _primary_address(conn, reg_id) -> int | None:
    cur = conn.execute("SELECT value FROM addresses WHERE belongs_to=? LIMIT 1", (reg_id,))
    row = cur.fetchone()
    return int(row[0]) if row else None


def _reg_attrs(conn, reg_id) -> dict:
    cur = conn.execute(
        "SELECT name, value FROM attributes WHERE belongs_to=? AND type=1", (reg_id,)
    )
    return {r[0]: r[1] for r in cur.fetchall()}


def _extract_bf_value(raw: int, hi: int, lo: int) -> int:
    width = hi - lo + 1
    return (raw >> lo) & ((1 << width) - 1)


def _indirect_info(conn, reg_id, reg_name, map_id) -> dict | None:
    """
    Returns a dict describing how to do indirect access for this register, or None
    if the register is plain direct-access.

    Keys: is_indirect (bool), is_access (bool), access_addr (int|None),
          index_hi (int), index_lo (int), rbw_hi (int|None)
    """
    attrs = _reg_attrs(conn, reg_id)
    if attrs.get('indirect') != '1' and 'gram_base' not in attrs:
        return None

    is_access = (
        attrs.get('is_gram_ctl') == '1'
        or attrs.get('gram_access') == 'access'
        or (reg_name or '').upper().endswith('_ACCESS')
    )

    def _bfs_of(rid):
        rows = conn.execute(
            "SELECT name, hi, lo FROM bitfields WHERE belongs_to=?", (rid,)
        ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    if is_access:
        row = conn.execute(
            "SELECT value FROM addresses WHERE belongs_to=? LIMIT 1", (reg_id,)
        ).fetchone()
        access_addr = int(row[0]) if row else None
        bfs = _bfs_of(reg_id)
        gram_sel = attrs.get('gram_sel')
        if gram_sel and gram_sel in bfs:
            idx_hi, idx_lo = bfs[gram_sel]
        elif 'addr' in bfs:
            idx_hi, idx_lo = bfs['addr']
        else:
            candidates = sorted(
                (hi, lo) for name, (hi, lo) in bfs.items() if name != 'access'
            )
            idx_hi, idx_lo = candidates[0] if candidates else (0, 0)
        rbw_hi = bfs['rbw'][0] if 'rbw' in bfs else None
        return {
            'is_indirect': True, 'is_access': True,
            'access_addr': access_addr,
            'index_hi': idx_hi, 'index_lo': idx_lo, 'rbw_hi': rbw_hi,
        }

    # DATA register: find the sibling ACCESS register
    gram_base = attrs.get('gram_base')
    if gram_base:
        row = conn.execute(
            """
            SELECT r.id, addr.value
            FROM registers r
            JOIN attributes a  ON a.belongs_to=r.id AND a.type=1
                               AND a.name='is_gram_ctl' AND a.value='1'
            JOIN addresses addr ON addr.belongs_to=r.id
            WHERE r.belongs_to=?
            LIMIT 1
            """,
            (map_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT r.id, addr.value
            FROM registers r
            JOIN attributes a  ON a.belongs_to=r.id AND a.type=1
                               AND a.name='indirect' AND a.value='1'
            JOIN addresses addr ON addr.belongs_to=r.id
            WHERE r.belongs_to=? AND r.id!=?
              AND (r.name LIKE '%ACCESS' OR r.name LIKE '%_CTL')
            LIMIT 1
            """,
            (map_id, reg_id),
        ).fetchone()

    if not row:
        return {'is_indirect': True, 'is_access': False, 'access_addr': None,
                'index_hi': 0, 'index_lo': 0, 'rbw_hi': None}

    access_reg_id, access_addr_raw = row[0], row[1]
    access_addr = int(access_addr_raw)
    access_attrs = _reg_attrs(conn, access_reg_id)
    abfs = _bfs_of(access_reg_id)
    gram_sel = access_attrs.get('gram_sel')
    if gram_sel and gram_sel in abfs:
        idx_hi, idx_lo = abfs[gram_sel]
    elif 'addr' in abfs:
        idx_hi, idx_lo = abfs['addr']
    else:
        candidates = sorted(
            (hi, lo) for name, (hi, lo) in abfs.items() if name != 'access'
        )
        idx_hi, idx_lo = candidates[0] if candidates else (0, 0)
    rbw_hi = abfs['rbw'][0] if 'rbw' in abfs else None
    return {
        'is_indirect': True, 'is_access': False,
        'access_addr': access_addr,
        'index_hi': idx_hi, 'index_lo': idx_lo, 'rbw_hi': rbw_hi,
    }


def _build_access_write_value(info: dict, index: int) -> int:
    """Compute the value to write to an indirect ACCESS register to trigger a read."""
    val = 0
    idx_hi, idx_lo = info['index_hi'], info['index_lo']
    mask = (1 << (idx_hi - idx_lo + 1)) - 1
    val |= (index & mask) << idx_lo
    if info.get('rbw_hi') is not None:
        val |= (1 << info['rbw_hi'])
    val |= (1 << 31)   # access trigger bit (standard Mercury pattern)
    return val


def _uart_read_register(uart, conn, reg_id, ind_info, uart_state: dict) -> int | None:
    """Read the register via UART, handling indirect access if needed."""
    if ind_info and ind_info['is_indirect'] and not ind_info['is_access']:
        # DATA register: re-write ACCESS first if index was previously set
        index = uart_state.get('index')
        if index is not None and ind_info.get('access_addr') is not None:
            write_val = _build_access_write_value(ind_info, index)
            uart.devmem_write(ind_info['access_addr'], write_val)
    addr = _primary_address(conn, reg_id)
    return uart.devmem_read(addr) if addr else None


def search_symbol(conn, term):
    pat = f"%{term}%"
    cur = conn.execute(
        "SELECT id, define, name, comment, belongs_to FROM registers "
        "WHERE define LIKE ? OR name LIKE ? ORDER BY define LIMIT 500",
        (pat, pat),
    )
    return _enrich(conn, cur.fetchall())


def search_symbol_fallback(conn, term):
    """Symbol search with progressive suffix trimming on zero results.

    Returns (results, used_term, trimmed_suffix).
    If the original term yields results, trimmed_suffix is "".
    Otherwise, strips trailing _TOKEN parts one at a time until a match
    is found or no underscore remains.
    """
    results = search_symbol(conn, term)
    if results:
        return results, term, ""

    parts = term.rsplit("_", 1)
    trimmed = []
    current = term
    while len(parts) == 2:
        current, suffix = parts
        trimmed.append(suffix)
        results = search_symbol(conn, current)
        if results:
            return results, current, "_" + "_".join(reversed(trimmed))
        parts = current.rsplit("_", 1)

    return [], term, ""


def search_keyword(conn, term):
    pat = f"%{term}%"
    cur = conn.execute(
        """
        SELECT DISTINCT r.id, r.define, r.name, r.comment, r.belongs_to
        FROM registers r
        WHERE r.define LIKE ? OR r.name LIKE ? OR r.comment LIKE ?
        UNION
        SELECT DISTINCT r.id, r.define, r.name, r.comment, r.belongs_to
        FROM registers r JOIN bitfields b ON b.belongs_to = r.id
        WHERE b.name LIKE ? OR b.comment LIKE ?
        UNION
        SELECT DISTINCT r.id, r.define, r.name, r.comment, r.belongs_to
        FROM registers r
        JOIN bitfields b ON b.belongs_to = r.id
        JOIN structs s ON s.belongs_to = b.id
        JOIN struct_rows sr ON sr.belongs_to = s.id
        JOIN row_items ri ON ri.belongs_to = sr.id
        WHERE ri.value LIKE ?
        ORDER BY define LIMIT 500
        """,
        (pat, pat, pat, pat, pat, pat),
    )
    return _enrich(conn, cur.fetchall())


def search_address(conn, term):
    needle = term.lower().lstrip("0x") if term.lower().startswith("0x") else term.lower()
    cur = conn.execute("SELECT belongs_to, value FROM addresses")
    seen, results = set(), []
    for reg_id, value in cur.fetchall():
        if needle in f"{int(value):x}" or needle in value:
            if reg_id not in seen:
                seen.add(reg_id)
                rc = conn.execute(
                    "SELECT id, define, name, comment, belongs_to "
                    "FROM registers WHERE id = ?",
                    (reg_id,),
                )
                row = rc.fetchone()
                if row:
                    results.append(row + (f"0x{int(value):x}",))
    results.sort(key=lambda r: r[1] or "")
    return results[:500]


# ─────────────────────────────────────────────────────────────────────────
# Detail formatter  →  list of (text, attr) tuples
# ─────────────────────────────────────────────────────────────────────────

def _col_widths(header, data, avail):
    """Compute per-column widths fitting within avail chars.

    Non-last columns get their natural width (capped at 32, min 10).
    The last column gets all remaining space.
    """
    ncols = max(len(header), max((len(r) for r in data), default=0), 1)
    natural = []
    for ci in range(ncols):
        w = len(header[ci]) if ci < len(header) else 0
        for row in data:
            if ci < len(row):
                w = max(w, len((row[ci] or '').replace('\n', ' ').strip()))
        natural.append(w)

    widths = [max(10, min(n, 32)) for n in natural[:-1]]
    sep = 2 * (ncols - 1)
    widths.append(max(10, avail - sum(widths) - sep))
    return widths


def _wrap_row(drow, widths, indent):
    """Wrap each cell per its column width; return list of display lines."""
    cols = []
    for ci, v in enumerate(drow):
        text = (v or '').replace('\n', ' ').strip()
        cw = widths[ci] if ci < len(widths) else widths[-1]
        cols.append(textwrap.wrap(text, cw) or [''])
    n = max(len(c) for c in cols)
    result = []
    for li in range(n):
        parts = []
        for ci, c in enumerate(cols):
            w = widths[ci] if ci < len(widths) else widths[-1]
            parts.append(f"{(c[li] if li < len(c) else ''):<{w}}")
        result.append(indent + "  ".join(parts))
    return result


def _fmt_detail(conn, row, width=120, live_value=None):
    """Return list of (text, curses_attr) lines for the detail view."""
    N    = curses.A_NORMAL
    B    = curses.A_BOLD
    H    = curses.color_pair(CP_HEADER) | curses.A_BOLD
    A    = curses.color_pair(CP_ADDR)
    DIM  = curses.color_pair(CP_DIM)
    LIVE = curses.color_pair(CP_LIVE) | curses.A_BOLD

    reg_id, define, name, comment, map_id = row[0], row[1], row[2], row[3], row[4]
    path  = _map_path(conn, map_id)
    addrs = _addresses(conn, reg_id)
    bfs   = _bitfields(conn, reg_id)

    lines = []
    lines.append((f" {define} ", B))
    lines.append(("", N))
    lines.append((f"  Path    : {path}", N))
    for a in addrs:
        lines.append((f"  Address : {a}", A))
    if live_value is not None:
        lines.append((f"  Live    : 0x{live_value:08x}", LIVE))
    if comment:
        segs = textwrap.wrap(comment.strip(), max(40, width - 14))
        for i, seg in enumerate(segs):
            prefix = "  Comment : " if i == 0 else "            "
            lines.append((prefix + seg, DIM))
    lines.append(("", N))

    has_live = live_value is not None

    if bfs:
        WB, WT, WD, WF = 10, 6, 10, 22   # bits  access  default  field
        WV = 10                            # value column (only when live_value set)
        if has_live:
            prefix_w = 2 + WB + 2 + WT + 2 + WD + 2 + WV + 2 + WF + 2
        else:
            prefix_w = 2 + WB + 2 + WT + 2 + WD + 2 + WF + 2
        desc_w      = max(20, width - prefix_w)
        desc_indent = ' ' * prefix_w

        if has_live:
            hdr = (f"  {'Bits':<{WB}} {'Access':<{WT}} {'Default':<{WD}}"
                   f" {'Value':<{WV}} {'Field':<{WF}} {'Description'}")
        else:
            hdr = (f"  {'Bits':<{WB}} {'Access':<{WT}} {'Default':<{WD}}"
                   f" {'Field':<{WF}} {'Description'}")
        lines.append((hdr, H))
        lines.append(("  " + "─" * (prefix_w + desc_w - 2), H))

        for bf in bfs:
            bf_id, bf_name, bf_type, hi, lo, bf_def, bf_comment = bf
            bits    = f"{hi}:{lo}" if hi != lo else str(hi)
            access  = _bf_access(bf_type)
            default = _fmt_default(bf_def)
            desc_segs = []
            for _ln in (bf_comment or '').split('\n'):
                _ln = _ln.rstrip()
                desc_segs.extend(textwrap.wrap(_ln, desc_w) if _ln else [''])
            if not desc_segs:
                desc_segs = ['']

            if has_live:
                val_str  = f"0x{_extract_bf_value(live_value, hi, lo):x}"
                # Return as a list of (text, attr) segments so Value column is green
                prefix   = f"  {bits:<{WB}} {access:<{WT}} {default:<{WD}} "
                val_part = f"{val_str:<{WV}} "
                rest     = f"{(bf_name or ''):<{WF}} {desc_segs[0]}"
                lines.append(([
                    (prefix,   N),
                    (val_part, LIVE),
                    (rest,     N),
                ], None))
            else:
                lines.append((
                    f"  {bits:<{WB}} {access:<{WT}} {default:<{WD}}"
                    f" {(bf_name or ''):<{WF}} {desc_segs[0]}",
                    N,
                ))
            for seg in desc_segs[1:]:
                lines.append((desc_indent + seg, N))

            # encoding / table structs
            for s_type, header, data in _bf_structs(conn, bf_id):
                if not data:
                    continue
                avail = width - 4   # 4-char leading indent
                widths = _col_widths(header, data, avail)
                lines.append((f"    [{s_type}]", DIM))
                if header:
                    lines.append(("    " + "  ".join(f"{h:<{w}}" for h, w in zip(header, widths)), H))
                    lines.append(("    " + "  ".join("─" * w for w in widths), H))
                sep = ("    " + "  ".join("─" * w for w in widths), DIM)
                for drow in data:
                    for text in _wrap_row(drow, widths, "    "):
                        lines.append((text, N))
                    lines.append(sep)
                lines.append(("", N))
    else:
        lines.append(("  (no bitfields)", DIM))

    return lines


# ─────────────────────────────────────────────────────────────────────────
# TUI screens
# ─────────────────────────────────────────────────────────────────────────

FOOTER_MENU         = "  ↑/↓ move   Enter select   q quit"
FOOTER_INPUT        = "  Type to search   Enter confirm   ←/Esc/b back   q quit (when empty)"
FOOTER_LIST         = "  ↑/↓ move   PgUp/PgDn page   Enter select   ← back"
FOOTER_DETAIL       = "  ↑/↓ scroll   PgUp/PgDn page   ← back   q quit"
FOOTER_DETAIL_UART  = "  ↑/↓ scroll   PgUp/PgDn page   ← back   r read   q quit"
FOOTER_DETAIL_IND   = "  ↑/↓ scroll   PgUp/PgDn page   ← back   r read   i index   q quit"


def _safe_addstr(win, y, x, text, attr=curses.A_NORMAL):
    h, w = win.getmaxyx()
    if y < 0 or y >= h:
        return
    text = text[:max(0, w - x - 1)]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def screen_menu(stdscr, title, items):
    """Arrow-key menu. Returns index or -1 on Esc/q."""
    curses.curs_set(0)
    cur = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        _draw_bar(stdscr, 0, f"  {title}", CP_TITLE)
        for i, item in enumerate(items):
            y = 2 + i
            marker = "▶" if i == cur else " "
            attr = curses.color_pair(CP_SEL) if i == cur else curses.A_NORMAL
            _safe_addstr(stdscr, y, 2, f"{marker} {item}", attr)
        _draw_bar(stdscr, h - 1, FOOTER_MENU, CP_FOOTER)
        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_UP   and cur > 0:             cur -= 1
        elif key == curses.KEY_DOWN and cur < len(items)-1: cur += 1
        elif key in (curses.KEY_ENTER, 10, 13):            return cur
        elif key in (27, ord('q'), curses.KEY_LEFT):       return -1


def screen_input(stdscr, title, prompt_str):
    """Text input. Returns stripped string or None on Esc/b."""
    curses.curs_set(1)
    buf = []
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        _draw_bar(stdscr, 0, f"  {title}", CP_TITLE)
        text_x = 2 + len(prompt_str) + 2
        _safe_addstr(stdscr, 2, 2, prompt_str + ": ")
        _safe_addstr(stdscr, 2, text_x, "".join(buf))
        _draw_bar(stdscr, h - 1, FOOTER_INPUT, CP_FOOTER)
        stdscr.move(2, min(text_x + len(buf), w - 1))
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_ENTER, 10, 13):
            result = "".join(buf).strip()
            return result or None
        elif key in (27, curses.KEY_LEFT):                  return None
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if buf: buf.pop()
        elif key == ord('b') and not buf:                   return None
        elif key == ord('q') and not buf:                   raise SystemExit(0)
        elif 32 <= key <= 126:
            buf.append(chr(key))


def screen_list(stdscr, title, rows, col_specs, initial_cur=0):
    """
    Scrollable list. col_specs: [(header, width, row_index), ...]
    Returns selected row index or -1.
    """
    curses.curs_set(0)
    cur = max(0, min(initial_cur, len(rows) - 1)) if rows else 0
    offset = 0

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = h - 4            # title(1) + blank(1) + header(1) + footer(1)
        _draw_bar(stdscr, 0, f"  {title}  ({len(rows)} found)", CP_TITLE)

        # Column headers (row 2)
        hdr = "  " + "  ".join(f"{s[0]:<{s[1]}}" for s in col_specs)
        _safe_addstr(stdscr, 2, 0, hdr[:w], curses.color_pair(CP_HEADER) | curses.A_BOLD)

        # Data rows
        for i in range(body_h):
            abs_i = offset + i
            y = 3 + i
            if abs_i >= len(rows):
                break
            row = rows[abs_i]
            attr = curses.color_pair(CP_SEL) if abs_i == cur else curses.A_NORMAL
            line = "  " + "  ".join(
                f"{_trunc(str(row[s[2]] or ''), s[1]):<{s[1]}}"
                for s in col_specs
            )
            _safe_addstr(stdscr, y, 0, line[:w], attr)

        pos = f"{cur+1}/{len(rows)}" if rows else "0/0"
        _draw_bar(stdscr, h - 1, FOOTER_LIST + f"   {pos}", CP_FOOTER)
        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_UP:
            if cur > 0:
                cur -= 1
                if cur < offset: offset = cur
        elif key == curses.KEY_DOWN:
            if cur < len(rows) - 1:
                cur += 1
                if cur >= offset + body_h: offset = cur - body_h + 1
        elif key == curses.KEY_PPAGE:
            cur    = max(0, cur - body_h)
            offset = max(0, offset - body_h)
        elif key == curses.KEY_NPAGE:
            cur    = min(len(rows) - 1, cur + body_h)
            offset = min(max(0, len(rows) - body_h), offset + body_h)
        elif key == curses.KEY_HOME:
            cur = offset = 0
        elif key == curses.KEY_END:
            cur = len(rows) - 1
            offset = max(0, cur - body_h + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return cur if rows else -1
        elif key in (27, ord('b'), curses.KEY_LEFT):
            return -1
        elif key == ord('q'):
            raise SystemExit(0)


def screen_detail(stdscr, lines, has_uart=False, is_indirect=False):
    """Scrollable detail view. lines = list of (text, attr).

    Returns None on back/quit, 'refresh' on 'r', 'index' on 'i'.
    """
    curses.curs_set(0)
    offset = 0

    if is_indirect and has_uart:
        footer = FOOTER_DETAIL_IND
    elif has_uart:
        footer = FOOTER_DETAIL_UART
    else:
        footer = FOOTER_DETAIL

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        body_h = h - 1

        for i in range(body_h):
            idx = offset + i
            if idx >= len(lines):
                break
            text, attr = lines[idx]
            if attr is None:
                # Multi-segment line: each element is (text, attr)
                x = 0
                for seg_text, seg_attr in text:
                    if x >= w - 1:
                        break
                    seg_text = seg_text[:max(0, w - x - 1)]
                    try:
                        stdscr.addstr(i, x, seg_text, seg_attr)
                    except curses.error:
                        pass
                    x += len(seg_text)
            else:
                _safe_addstr(stdscr, i, 0, text[:w], attr)

        total = len(lines)
        pos = f"{offset+1}-{min(offset+body_h, total)}/{total}"
        _draw_bar(stdscr, h - 1, footer + f"   {pos}", CP_FOOTER)
        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_UP:
            offset = max(0, offset - 1)
        elif key == curses.KEY_DOWN:
            offset = min(max(0, total - body_h), offset + 1)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - body_h)
        elif key == curses.KEY_NPAGE:
            offset = min(max(0, total - body_h), offset + body_h)
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = max(0, total - body_h)
        elif key in (27, ord('b'), curses.KEY_LEFT):
            return None
        elif key == ord('q'):
            raise SystemExit(0)
        elif key == ord('r') and has_uart:
            return 'refresh'
        elif key == ord('i') and has_uart and is_indirect:
            return 'index'


# ─────────────────────────────────────────────────────────────────────────
# Main flow
# ─────────────────────────────────────────────────────────────────────────

MODE_LABELS = [
    "Symbol   — define / name    (e.g. VE_WAN, FIELD_CAM)",
    "Keyword  — name + comment   (e.g. vlan, drop_src)",
    "Address  — hex / partial    (e.g. 0x3d0c, 240c00)",
]
MODE_NAMES = ["Symbol", "Keyword", "Address"]

LIST_COLS = [
    ("Define",  52, 1),
    ("Address", 16, 5),
]


def _run(stdscr, conn, uart=None):
    _init_colors()
    curses.curs_set(0)

    # Stack entries:
    #   ("mode",)
    #   ("search", mode_idx)
    #   ("results", mode_idx, results, term, note, saved_cur)
    #   ("detail", row, live_value, uart_state)
    #     live_value: int|None — last read hardware value
    #     uart_state: dict with optional key "index" for indirect access
    stack = [("mode",)]

    uart_connected = uart is not None and uart.is_connected()

    while stack:
        state = stack[-1]
        kind  = state[0]

        if kind == "mode":
            idx = screen_menu(stdscr, "Register Query — Select search mode", MODE_LABELS)
            if idx == -1:
                break
            stack.append(("search", idx))

        elif kind == "search":
            mode_idx = state[1]
            term = screen_input(stdscr, f"{MODE_NAMES[mode_idx]} Search", "Search term")
            if term is None:
                stack.pop()
                continue
            if mode_idx == 0:
                results, used_term, trimmed = search_symbol_fallback(conn, term)
                note = f"  [trimmed '{trimmed}']" if trimmed else ""
            elif mode_idx == 1:
                results, used_term, note = search_keyword(conn, term), term, ""
            else:
                results, used_term, note = search_address(conn, term), term, ""
            stack.append(("results", mode_idx, results, used_term, note, 0))

        elif kind == "results":
            _, mode_idx, results, term, note, saved_cur = state
            title = f"{MODE_NAMES[mode_idx]} — '{term}'{note}"
            idx = screen_list(stdscr, title, results, LIST_COLS, saved_cur)
            if idx == -1:
                stack.pop()
            else:
                stack[-1] = (state[0], mode_idx, results, term, note, idx)
                stack.append(("detail", results[idx], None, {}))

        elif kind == "detail":
            row        = state[1]
            live_value = state[2]
            uart_state = state[3]
            _, w = stdscr.getmaxyx()

            ind_info    = _indirect_info(conn, row[0], row[2], row[4]) if uart_connected else None
            is_indirect = ind_info is not None

            detail_lines = _fmt_detail(conn, row, w, live_value=live_value)
            action = screen_detail(
                stdscr, detail_lines,
                has_uart=uart_connected,
                is_indirect=is_indirect,
            )

            if action is None:
                stack.pop()

            elif action == 'refresh':
                live_value = _uart_read_register(uart, conn, row[0], ind_info, uart_state)
                stack[-1] = ("detail", row, live_value, uart_state)

            elif action == 'index':
                index_str = screen_input(stdscr, "Indirect Access Index", "Index (hex or dec)")
                if index_str:
                    try:
                        index = int(index_str, 0)
                        uart_state = {**uart_state, 'index': index}
                        # For ACCESS registers: write the full access value to self
                        if ind_info and ind_info['is_access']:
                            write_val = _build_access_write_value(ind_info, index)
                            if ind_info.get('access_addr') is not None:
                                uart.devmem_write(ind_info['access_addr'], write_val)
                        # For DATA registers: write to companion ACCESS register
                        elif ind_info and ind_info.get('access_addr') is not None:
                            write_val = _build_access_write_value(ind_info, index)
                            uart.devmem_write(ind_info['access_addr'], write_val)
                        live_value = _uart_read_register(uart, conn, row[0], ind_info, uart_state)
                    except ValueError:
                        pass
                stack[-1] = ("detail", row, live_value, uart_state)


# ─────────────────────────────────────────────────────────────────────────
# CMD mode helpers
# ─────────────────────────────────────────────────────────────────────────

def _lookup_by_address(conn, addr_str):
    """Return (id, define, name, comment, belongs_to) for an exact address, or None."""
    try:
        needle = int(addr_str, 0)
    except ValueError:
        return None
    cur = conn.execute(
        "SELECT belongs_to, value FROM addresses WHERE CAST(value AS INTEGER) = ?",
        (needle,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    reg_id = row[0]
    rc = conn.execute(
        "SELECT id, define, name, comment, belongs_to FROM registers WHERE id = ?",
        (reg_id,),
    )
    return rc.fetchone()


def _fmt_detail_text(conn, row, width=120, live_value=None):
    """Plain-text version of _fmt_detail (no curses attributes)."""
    reg_id, define, name, comment, map_id = row[0], row[1], row[2], row[3], row[4]
    path  = _map_path(conn, map_id)
    addrs = _addresses(conn, reg_id)
    bfs   = _bitfields(conn, reg_id)

    lines = []
    lines.append(define)
    lines.append("")
    lines.append(f"  Path    : {path}")
    for a in addrs:
        lines.append(f"  Address : {a}")
    if live_value is not None:
        lines.append(f"  Live    : 0x{live_value:08x}")
    if comment:
        segs = textwrap.wrap(comment.strip(), max(40, width - 14))
        for i, seg in enumerate(segs):
            prefix = "  Comment : " if i == 0 else "            "
            lines.append(prefix + seg)
    lines.append("")

    has_live = live_value is not None

    if bfs:
        WB, WT, WD, WF = 10, 6, 10, 22
        WV = 10
        if has_live:
            prefix_w = 2 + WB + 2 + WT + 2 + WD + 2 + WV + 2 + WF + 2
        else:
            prefix_w = 2 + WB + 2 + WT + 2 + WD + 2 + WF + 2
        desc_w      = max(20, width - prefix_w)
        desc_indent = ' ' * prefix_w

        if has_live:
            hdr = (f"  {'Bits':<{WB}} {'Access':<{WT}} {'Default':<{WD}}"
                   f" {'Value':<{WV}} {'Field':<{WF}} {'Description'}")
        else:
            hdr = (f"  {'Bits':<{WB}} {'Access':<{WT}} {'Default':<{WD}}"
                   f" {'Field':<{WF}} {'Description'}")
        lines.append(hdr)
        lines.append("  " + "─" * (prefix_w + desc_w - 2))

        for bf in bfs:
            bf_id, bf_name, bf_type, hi, lo, bf_def, bf_comment = bf
            bits    = f"{hi}:{lo}" if hi != lo else str(hi)
            access  = _bf_access(bf_type)
            default = _fmt_default(bf_def)
            desc_segs = []
            for _ln in (bf_comment or '').split('\n'):
                _ln = _ln.rstrip()
                desc_segs.extend(textwrap.wrap(_ln, desc_w) if _ln else [''])
            if not desc_segs:
                desc_segs = ['']

            if has_live:
                val_str = f"0x{_extract_bf_value(live_value, hi, lo):x}"
                lines.append(
                    f"  {bits:<{WB}} {access:<{WT}} {default:<{WD}}"
                    f" {val_str:<{WV}} {(bf_name or ''):<{WF}} {desc_segs[0]}"
                )
            else:
                lines.append(
                    f"  {bits:<{WB}} {access:<{WT}} {default:<{WD}}"
                    f" {(bf_name or ''):<{WF}} {desc_segs[0]}"
                )
            for seg in desc_segs[1:]:
                lines.append(desc_indent + seg)

            for s_type, header, data in _bf_structs(conn, bf_id):
                if not data:
                    continue
                avail  = width - 4
                widths = _col_widths(header, data, avail)
                lines.append(f"    [{s_type}]")
                if header:
                    lines.append("    " + "  ".join(f"{h:<{w}}" for h, w in zip(header, widths)))
                    lines.append("    " + "  ".join("─" * w for w in widths))
                sep = "    " + "  ".join("─" * w for w in widths)
                for drow in data:
                    for text in _wrap_row(drow, widths, "    "):
                        lines.append(text)
                    lines.append(sep)
                lines.append("")
    else:
        lines.append("  (no bitfields)")

    return lines


def cmd_search(conn, search_type, term):
    if search_type == "symbol":
        results = search_symbol(conn, term)
    elif search_type == "keyword":
        results = search_keyword(conn, term)
    else:
        results = search_address(conn, term)

    if not results:
        print("(no results)", file=sys.stderr)
        sys.exit(1)

    col_w = max(len(r[1] or "") for r in results)
    for r in results:
        define = (r[1] or "").ljust(col_w)
        addr   = r[5] if len(r) > 5 else "—"
        print(f"{define}  {addr}")


def cmd_detail(conn, addr_str, uart=None, index=None):
    row = _lookup_by_address(conn, addr_str)
    if row is None:
        print(f"Error: no register found at address {addr_str}", file=sys.stderr)
        sys.exit(1)

    live_value = None
    if uart and uart.is_connected():
        ind_info = _indirect_info(conn, row[0], row[2], row[4])
        if ind_info and ind_info['is_indirect']:
            if index is None:
                print(
                    "Note: indirect register — use --index N to read live data",
                    file=sys.stderr,
                )
            else:
                write_val = _build_access_write_value(ind_info, index)
                access_addr = ind_info.get('access_addr')
                if ind_info['is_access']:
                    # Write this ACCESS register with the index
                    try:
                        access_addr = int(addr_str, 0)
                    except ValueError:
                        access_addr = None
                if access_addr is not None:
                    uart.devmem_write(access_addr, write_val)
                live_value = _uart_read_register(uart, conn, row[0], ind_info,
                                                  {'index': index})
        else:
            try:
                addr_int = int(addr_str, 0)
            except ValueError:
                addr_int = None
            if addr_int is not None:
                live_value = uart.devmem_read(addr_int)

    width = shutil.get_terminal_size(fallback=(120, 24)).columns
    for line in _fmt_detail_text(conn, row, width, live_value=live_value):
        print(line)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Interactive register query CLI")
    ap.add_argument("-db", default=str(DEFAULT_DB), metavar="PATH")
    ap.add_argument("--search-type", choices=["symbol", "keyword", "address"],
                    metavar="symbol|keyword|address")
    ap.add_argument("--search", metavar="TERM")
    ap.add_argument("--address", metavar="ADDR")
    # UART options
    ap.add_argument("--uart", metavar="PORT",
                    help="UART device for live register reads (e.g. /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=115200, metavar="RATE",
                    help="UART baud rate (default 115200)")
    # SSH options
    ap.add_argument("--ssh", metavar="HOST",
                    help="SSH host for live register reads (e.g. 192.168.1.1)")
    ap.add_argument("--ssh-port", type=int, default=22, metavar="PORT",
                    help="SSH port (default 22)")
    ap.add_argument("--ssh-user", default="root", metavar="USER",
                    help="SSH username (default root)")
    ap.add_argument("--ssh-password", default=None, metavar="PASS",
                    help="SSH password (omit to use key auth)")
    # Indirect access
    ap.add_argument("--index", type=lambda x: int(x, 0), default=None, metavar="N",
                    help="Index for indirect-access registers (used with --address)")
    args = ap.parse_args()

    # ── Validate cmd-mode argument combinations ──────────────────────────
    has_search      = args.search is not None
    has_search_type = args.search_type is not None
    has_address     = args.address is not None

    if has_address and (has_search or has_search_type):
        print("Error: --address and --search/--search-type are mutually exclusive",
              file=sys.stderr)
        sys.exit(1)
    if has_search and not has_search_type:
        print("Error: --search requires --search-type", file=sys.stderr)
        sys.exit(1)
    if has_search_type and not has_search:
        print("Error: --search-type requires --search", file=sys.stderr)
        sys.exit(1)

    cmd_mode = has_address or has_search

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    # ── Console (UART or SSH, mutually exclusive) ─────────────────────────
    if args.uart and args.ssh:
        print("Error: --uart and --ssh are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    uart = None
    if args.uart:
        try:
            uart = UartConsole(args.uart, baudrate=args.baud)
            if uart.connect():
                if not cmd_mode:
                    print(f"UART connected: {args.uart} @ {args.baud}", file=sys.stderr)
            else:
                print(f"Warning: could not connect to UART at {args.uart}", file=sys.stderr)
                uart = None
        except Exception as e:
            print(f"Warning: UART init error: {e}", file=sys.stderr)
            uart = None
    elif args.ssh:
        try:
            uart = SshConsole(
                args.ssh,
                port=args.ssh_port,
                username=args.ssh_user,
                password=args.ssh_password,
            )
            if uart.connect():
                if not cmd_mode:
                    print(f"SSH connected: {args.ssh_user}@{args.ssh}:{args.ssh_port}",
                          file=sys.stderr)
            else:
                reason = getattr(uart, '_last_error', 'unknown error')
                print(f"Warning: could not connect via SSH to {args.ssh}: {reason}", file=sys.stderr)
                uart = None
        except Exception as e:
            print(f"Warning: SSH init error: {e}", file=sys.stderr)
            uart = None

    conn = sqlite3.connect(str(db_path))
    try:
        if cmd_mode:
            if has_address:
                cmd_detail(conn, args.address, uart=uart, index=args.index)
            else:
                cmd_search(conn, args.search_type, args.search)
        else:
            try:
                curses.wrapper(_run, conn, uart)
            except SystemExit:
                pass
    finally:
        conn.close()
        if uart:
            uart.disconnect()


if __name__ == "__main__":
    main()
