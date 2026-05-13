"""
PTY backend for the Windows wrapper.

Wraps pywinpty so the rest of the wrapper can stay protocol-focused
instead of dealing with ConPTY/winpty quirks. The Linux wrapper uses
the stdlib `pty` module; on Windows the equivalent capability ships
as a third-party package (pywinpty / winpty) that drives the OS's
ConPTY API.

pywinpty has no fileno() / select() integration — reads block until
data arrives or the child exits. We do not try to fix that here; the
caller spins a reader thread and pulls bytes into a queue, the same
shape used by the input-poller / output-uploader threads.

Required: pip install pywinpty
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional


class PtyChild:
    """Thin facade over winpty.PtyProcess.

    Methods:
        read(max_bytes)   -> bytes  (blocks until at least 1 byte or EOF)
        write(data)       -> int    (bytes accepted by the PTY)
        resize(cols, rows)
        alive()           -> bool
        wait(timeout=None)-> exit code (None if still running and timeout hit)
        kill()
        close()
    """

    def __init__(self, *, cmd, cwd: str, env: dict, cols: int, rows: int):
        try:
            from winpty import PtyProcess  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "pywinpty is not installed. Run:\n"
                "    pip install --user pywinpty\n"
                f"(import error: {e})"
            ) from e

        # PtyProcess.spawn takes a single command line string OR a list.
        # We accept a list and let pywinpty handle quoting; this matches
        # how the Linux side hands off argv.
        if isinstance(cmd, str):
            cmdline = cmd
        else:
            cmdline = list(cmd)

        # pywinpty 2.x removed the `encoding` kwarg; read() returns str
        # by default. We re-encode in our read() wrapper below so the
        # rest of the pipeline still sees bytes.
        self._proc = PtyProcess.spawn(
            cmdline,
            cwd=cwd,
            env=env,
            dimensions=(rows, cols),
        )

    def read(self, max_bytes: int = 65536) -> bytes:
        try:
            data = self._proc.read(max_bytes)
        except EOFError:
            return b""
        except OSError:
            return b""
        if data is None:
            return b""
        if isinstance(data, str):
            return data.encode("utf-8", errors="replace")
        return bytes(data)

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        # pywinpty 2.x expects str on write; the rest of the wrapper
        # speaks bytes (keyboard input arrives as raw bytes from the
        # browser via AEAD-decrypted records). Decode best-effort.
        try:
            payload = data.decode("utf-8", errors="replace")
        except Exception:
            return 0
        try:
            n = self._proc.write(payload)
            # write() in pywinpty 2.x returns the number of characters
            # written; report bytes-of-the-original as a best-effort.
            return len(data) if n else 0
        except OSError:
            return 0
        except TypeError:
            # Older pywinpty that still expects bytes — try the raw form.
            try:
                return self._proc.write(data)
            except Exception:
                return 0

    def resize(self, cols: int, rows: int) -> None:
        try:
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def alive(self) -> bool:
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if timeout is None:
            try:
                return self._proc.wait()
            except Exception:
                return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return self.exitstatus()
            time.sleep(0.05)
        return None

    def exitstatus(self) -> Optional[int]:
        try:
            return self._proc.exitstatus
        except Exception:
            return None

    def kill(self) -> None:
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._proc.close()
        except Exception:
            pass


def enable_vt_input(handle_stdin=None) -> Optional[int]:
    """Put the console's stdin into virtual-terminal-input mode and
    disable line/echo/processed so keystrokes (incl. arrow keys, Ctrl-C
    as ^C, etc.) reach the child unaltered. Returns the previous mode
    flags so the caller can restore them on shutdown. Returns None when
    stdin is not a real console.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    STD_INPUT_HANDLE = -10
    ENABLE_PROCESSED_INPUT = 0x0001
    ENABLE_LINE_INPUT = 0x0002
    ENABLE_ECHO_INPUT = 0x0004
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

    h = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    if not h or h == wintypes.HANDLE(-1).value:
        return None
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
        return None
    prev = mode.value
    new_mode = (prev & ~(ENABLE_PROCESSED_INPUT
                         | ENABLE_LINE_INPUT
                         | ENABLE_ECHO_INPUT)) | ENABLE_VIRTUAL_TERMINAL_INPUT
    if not kernel32.SetConsoleMode(h, new_mode):
        return None
    return prev


def restore_console_mode(prev: Optional[int]) -> None:
    if prev is None:
        return
    try:
        import ctypes
    except Exception:
        return
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    STD_INPUT_HANDLE = -10
    h = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    if h:
        kernel32.SetConsoleMode(h, prev)


def enable_vt_output(handle_stdout=None) -> Optional[int]:
    """Enable VT output on stdout so ANSI escape sequences from the child
    pass through (colours, cursor moves). Mirrors enable_vt_input."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    STD_OUTPUT_HANDLE = -11
    ENABLE_PROCESSED_OUTPUT = 0x0001
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    DISABLE_NEWLINE_AUTO_RETURN = 0x0008

    h = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    if not h or h == wintypes.HANDLE(-1).value:
        return None
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
        return None
    prev = mode.value
    new_mode = prev | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING | DISABLE_NEWLINE_AUTO_RETURN
    if not kernel32.SetConsoleMode(h, new_mode):
        return None
    return prev


def get_console_size() -> tuple[int, int]:
    """Return (cols, rows) of the attached console, or (80, 24) on failure."""
    try:
        sz = os.get_terminal_size()
        return (max(1, sz.columns), max(1, sz.lines))
    except OSError:
        return (80, 24)


def is_stdin_console() -> bool:
    """True if stdin is a real console (not a pipe / redirect)."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    STD_INPUT_HANDLE = -10
    h = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    if not h:
        return False
    mode = wintypes.DWORD()
    return bool(kernel32.GetConsoleMode(h, ctypes.byref(mode)))


def read_stdin_block(buf_size: int = 4096) -> bytes:
    """Blocking read from the real stdin handle, returning raw bytes.

    We can't use sys.stdin.buffer.read() on Windows after putting the
    console into VT_INPUT mode without it potentially translating
    things. ReadFile on the raw handle is the safe route.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        try:
            return sys.stdin.buffer.read(buf_size)
        except Exception:
            return b""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    STD_INPUT_HANDLE = -10
    h = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    if not h:
        return b""
    buf = ctypes.create_string_buffer(buf_size)
    nread = wintypes.DWORD()
    ok = kernel32.ReadFile(h, buf, buf_size, ctypes.byref(nread), None)
    if not ok or nread.value == 0:
        return b""
    return buf.raw[:nread.value]
