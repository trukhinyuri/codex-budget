"""Read-only, bounded stdio transport for the stock Codex app-server.

This module sends no turn, authentication, reset-credit or configuration RPCs.
It deliberately does not expose a general-purpose RPC client.
"""

from __future__ import annotations

import json
import math
import os
import queue
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

MACOS_CLI_PATHS = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
    "/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex",
)
MAX_LINE_BYTES = 256 * 1024
MAX_STDOUT_BYTES = 1024 * 1024
MAX_MESSAGES = 256
MAX_JSON_DEPTH = 64
_READ_CHUNK = 16 * 1024
_CLEANUP_GRACE = 0.3
_WINDOWS_CLEANUP_TIMEOUT = 2.0
_QUEUE_CHUNKS = 4
_READ_FAILED = object()
_RPC_METHODS = frozenset({"initialize", "initialized", "account/rateLimits/read"})


class TransportError(RuntimeError):
    """A safe transport failure; server text and stderr are never included."""


class UnsupportedPlatformError(TransportError):
    """The platform cannot provide the required pipe and process-group behavior."""


class CLIUnavailableError(TransportError):
    """The executable is missing, invalid, or cannot be started."""


class TransportTimeoutError(TransportError):
    """The single deadline covering initialization and the read expired."""


class ProtocolError(TransportError):
    """Malformed, unexpected, or disallowed protocol traffic."""


class OutputLimitError(ProtocolError):
    """The server exceeded the bounded output allowance."""


class ServerRejectedError(TransportError):
    """The server rejected the read-only request; no server text is exposed."""


def _require_supported_platform() -> None:
    if sys.platform not in ("darwin", "linux", "win32"):
        raise UnsupportedPlatformError(
            "Codex budget transport supports macOS, Linux, and Windows only"
        )


def _resolve_cli(cli_path: str | os.PathLike[str] | None) -> str:
    if cli_path is not None:
        try:
            candidate = os.fspath(cli_path)
        except (TypeError, ValueError):
            raise CLIUnavailableError("Codex CLI path is invalid") from None
        if not isinstance(candidate, str) or not candidate or "\x00" in candidate:
            raise CLIUnavailableError("Codex CLI path is invalid")
    else:
        candidate = None
        if sys.platform == "darwin":
            candidate = next(
                (
                    path
                    for path in MACOS_CLI_PATHS
                    if os.path.isfile(path) and os.access(path, os.X_OK)
                ),
                None,
            )
        if candidate is None:
            candidate = shutil.which("codex")
    if not candidate or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        raise CLIUnavailableError("Codex CLI executable is unavailable")
    return os.path.abspath(candidate)


def _windows_system_tool(name: str) -> str:
    """Resolve Windows' own helpers, avoiding a project-local shell executable."""
    root = os.environ.get("SystemRoot")
    candidate = str(Path(root) / "System32" / name) if root else shutil.which(name)
    if not candidate or not os.path.isfile(candidate):
        raise CLIUnavailableError("Required Windows process helper is unavailable")
    return os.path.abspath(candidate)


def platform_command(binary: str, *arguments: str) -> tuple[list[str] | str, dict]:
    """Build argv, quoting every batch argument without cmd variable expansion."""
    if sys.platform != "win32":
        return [binary, *arguments], {"start_new_session": True}
    options = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    if Path(binary).suffix.lower() not in (".cmd", ".bat"):
        return [binary, *arguments], options
    # cmd's quoted paths still expand %variables%. Reject those rare path forms
    # rather than allowing expansion; the only argument is the fixed app-server.
    if any(any(char in value for char in ('"', "%", "\r", "\n")) for value in (binary, *arguments)):
        raise CLIUnavailableError("Windows batch CLI path cannot be safely launched")
    shell = _windows_system_tool("cmd.exe")
    options["executable"] = shell
    quoted = " ".join(f'"{value}"' for value in (binary, *arguments))
    return f'"{shell}" /d /v:off /s /c "{quoted}"', options


def _command(binary: str) -> tuple[list[str] | str, dict]:
    return platform_command(binary, "app-server")


class _WindowsJob:
    """Kill assigned processes on close, using documented Win32 job APIs.

    Assignment happens before RPC traffic, after Popen. Children created before
    assignment are outside that guarantee; no private Popen handles are used.
    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.handle = None
        try:
            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = {
                "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
                "SetInformationJobObject": (
                    [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                    wintypes.BOOL,
                ),
                "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
                "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            }
            for name, (arguments, result) in signatures.items():
                function = getattr(self.api, name)
                function.argtypes = arguments
                function.restype = result
            self.handle = self.api.CreateJobObjectW(None, None)
            if not self.handle:
                raise TransportError("Could not create Windows process containment")
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise TransportError("Could not configure Windows process containment")
        except (OSError, AttributeError):
            self.close()
            raise TransportError("Windows process containment is unavailable") from None
        except TransportError:
            self.close()
            raise

    def assign(self, pid: int) -> None:
        # AssignProcessToJobObject requires PROCESS_SET_QUOTA | PROCESS_TERMINATE.
        handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        if not handle:
            raise TransportError("Could not assign Windows app-server process containment")
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise TransportError("Could not assign Windows app-server process containment")
        finally:
            if not self.api.CloseHandle(handle):
                raise TransportError("Could not release Windows process handle")

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle and not self.api.CloseHandle(handle):
            raise TransportError("Could not close Windows process containment")


# Exact wire envelopes prevent future callers from widening initialization or
# changing the account read's privacy-related parameters while retaining a
# whitelisted method name. These are the entire outgoing protocol surface.
_REQUESTS = (
    {
        "id": 1,
        "method": "initialize",
        "params": {"clientInfo": {"name": "codex_budget", "version": "1.0"}, "capabilities": None},
    },
    {"method": "initialized"},
    {
        "id": 2,
        "method": "account/rateLimits/read",
        "params": {"supportsLunaReserve": False, "excludeResetCreditDetails": True},
    },
)
_ALLOWED_WIRE_MESSAGES = frozenset(
    json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    for message in _REQUESTS
)


def _send(process: subprocess.Popen[bytes], message: dict) -> None:
    """Enforce the outgoing whitelist even if internal callers are changed."""
    if (
        not isinstance(message, dict)
        or not isinstance(message.get("method"), str)
        or message["method"] not in _RPC_METHODS
    ):
        raise ProtocolError("Disallowed app-server RPC")
    try:
        payload = json.dumps(
            message, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ProtocolError("Invalid read-only app-server request") from None
    if payload not in _ALLOWED_WIRE_MESSAGES:
        raise ProtocolError("Invalid read-only app-server request")
    if process.stdin is None:
        raise TransportError("Could not send the read-only app-server request")
    try:
        wire = payload + b"\n"
        if process.stdin.write(wire) != len(wire):
            raise TransportError("Could not send the read-only app-server request")
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        raise TransportError("Could not send the read-only app-server request") from None


def _reject_constant(_value: str) -> None:
    raise ValueError("non-JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _check_depth(line: bytes) -> None:
    """Bound nesting before the decoder allocates a deeply nested structure."""
    depth = 0
    in_string = False
    escaped = False
    for char in line:
        if in_string:
            if escaped:
                escaped = False
            elif char == 92:  # backslash
                escaped = True
            elif char == 34:  # quote
                in_string = False
        elif char == 34:
            in_string = True
        elif char in (91, 123):  # [ {
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise OutputLimitError("Codex app-server JSON exceeded the depth limit")
        elif char in (93, 125):  # ] }
            depth -= 1


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class _Responses:
    def __init__(self, process: subprocess.Popen[bytes], deadline: float):
        assert process.stdout is not None
        self.stdout = process.stdout
        self.deadline = deadline
        self.buffer = bytearray()
        self.total_bytes = 0
        self.messages = 0
        self.selector = None
        self.worker = None
        self.stopping = threading.Event()
        self.chunks = queue.Queue(maxsize=_QUEUE_CHUNKS)
        try:
            if sys.platform == "win32":
                # Windows selectors do not monitor anonymous subprocess pipes.
                # At most four chunks plus one worker-held chunk can wait here.
                self.worker = threading.Thread(
                    target=self._read_worker, name="codex-budget-stdout", daemon=True
                )
                self.worker.start()
            else:
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.stdout, selectors.EVENT_READ)
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise TransportError("Could not monitor Codex app-server output") from None

    def close(self) -> None:
        self.stopping.set()
        if self.selector is not None:
            try:
                self.selector.close()
            except OSError:
                pass

    def join(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=_CLEANUP_GRACE)

    def _read_worker(self) -> None:
        while not self.stopping.is_set():
            try:
                chunk = os.read(self.stdout.fileno(), _READ_CHUNK)
            except (OSError, ValueError):
                chunk = _READ_FAILED
            while not self.stopping.is_set():
                try:
                    self.chunks.put(chunk, timeout=0.05)
                    break
                except queue.Full:
                    continue
            if chunk is _READ_FAILED or not chunk:
                return

    def _chunk(self, remaining: float) -> bytes | None:
        if self.worker is not None:
            try:
                chunk = self.chunks.get(timeout=min(remaining, 60.0))
            except queue.Empty:
                return None
            if chunk is _READ_FAILED:
                raise TransportError("Could not read Codex app-server output")
            return chunk
        try:
            # Selector APIs have platform-specific maximum wait values.
            ready = self.selector.select(min(remaining, 60.0))
        except (OSError, ValueError, OverflowError):
            raise TransportError("Could not monitor Codex app-server output") from None
        if not ready:
            return None
        try:
            return os.read(self.stdout.fileno(), _READ_CHUNK)
        except OSError:
            raise TransportError("Could not read Codex app-server output") from None

    def _line(self) -> bytes:
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeoutError("Codex app-server read timed out")
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_LINE_BYTES:
                    raise OutputLimitError("Codex app-server output exceeded the line limit")
                line = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                return line
            if len(self.buffer) > MAX_LINE_BYTES:
                raise OutputLimitError("Codex app-server output exceeded the line limit")
            chunk = self._chunk(remaining)
            if chunk is None:
                continue
            if not chunk:
                raise TransportError("Codex app-server closed output before replying")
            self.total_bytes += len(chunk)
            if self.total_bytes > MAX_STDOUT_BYTES:
                raise OutputLimitError("Codex app-server output exceeded the session limit")
            self.buffer.extend(chunk)

    def response(self, expected_id: int) -> dict:
        while True:
            line = self._line()
            if not line.strip():
                continue
            self.messages += 1
            if self.messages > MAX_MESSAGES:
                raise OutputLimitError("Codex app-server sent too many messages")
            _check_depth(line)
            try:
                message = json.loads(
                    line.decode("utf-8"),
                    parse_constant=_reject_constant,
                    parse_float=_finite_float,
                    object_pairs_hook=_unique_object,
                )
            except (UnicodeError, ValueError, RecursionError):
                raise ProtocolError("Malformed Codex app-server output") from None
            if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
                raise ProtocolError("Unexpected Codex app-server message")
            if "method" in message:
                if (
                    "id" in message
                    or "result" in message
                    or "error" in message
                    or not isinstance(message["method"], str)
                    or not message["method"]
                    or not message.keys() <= {"jsonrpc", "method", "params", "emittedAtMs"}
                    or (
                        message.get("emittedAtMs") is not None
                        and (
                            type(message["emittedAtMs"]) is not int
                            or not -(2**63) <= message["emittedAtMs"] < 2**63
                        )
                    )
                ):
                    raise ProtocolError("Unexpected Codex app-server request")
                # Notifications need no reply. Their bytes and count remain bounded.
                continue
            if type(message.get("id")) is not int or message["id"] != expected_id:
                raise ProtocolError("Unexpected Codex app-server response identifier")
            if not message.keys() <= {"jsonrpc", "id", "result", "error"}:
                raise ProtocolError("Unexpected Codex app-server response")
            if ("result" in message) == ("error" in message):
                raise ProtocolError("Malformed Codex app-server response")
            if "error" in message:
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                suffix = f" (code {code})" if type(code) is int and -32768 <= code <= 32767 else ""
                raise ServerRejectedError(
                    f"Codex app-server rejected the read-only request{suffix}"
                )
            if not isinstance(message["result"], dict):
                raise ProtocolError("Unexpected Codex app-server result")
            return message["result"]


def _terminate(process: subprocess.Popen[bytes], job: _WindowsJob | None = None) -> None:
    """Stop the isolated process group, including surviving descendants."""
    if sys.platform == "win32":
        # Kill the tree before closing stdin, which could make the group leader
        # exit before taskkill can discover its children.
        try:
            subprocess.run(  # noqa: S603 - documented system taskkill with fixed options and child PID
                [_windows_system_tool("taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_WINDOWS_CLEANUP_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, TransportError):
            pass
        # Assigned descendants remain contained even if the leader has exited.
        # Close before pipes: an inherited pipe may be held by those descendants.
        containment_error = None
        if job is not None:
            try:
                job.close()
            except TransportError as error:
                containment_error = error
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_CLEANUP_GRACE)
        except (subprocess.TimeoutExpired, OSError):
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if containment_error is not None:
            raise containment_error
        return
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            # Still reap the direct child if group termination is unavailable.
            try:
                process.send_signal(sig)
            except OSError:
                pass
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=_CLEANUP_GRACE)
            except subprocess.TimeoutExpired:
                pass
    try:
        process.wait(timeout=_CLEANUP_GRACE)
    except subprocess.TimeoutExpired:
        # Bounded cleanup; signal delivery can race an OS-level process teardown.
        pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def read_rate_limits(cli_path: str | os.PathLike[str] | None = None, timeout: float = 20) -> dict:
    """Read the account snapshot through a new stock CLI app-server process.

    The timeout covers the handshake and usage read together. Error messages omit
    server response text, stderr and binary paths. No credential file is accessed
    by this Python module; the unmodified CLI uses its normal account session.
    """
    _require_supported_platform()
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TransportError("Transport timeout must be a positive finite number")
    try:
        valid_timeout = math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise TransportError("Transport timeout must be a positive finite number")
    binary = _resolve_cli(cli_path)
    command, process_options = _command(binary)
    deadline = time.monotonic() + timeout
    job = _WindowsJob() if sys.platform == "win32" else None
    try:
        process = subprocess.Popen(  # noqa: S603 - validated stock CLI; fixed app-server invocation
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).resolve().parent,
            bufsize=0,
            **process_options,
        )
    except (OSError, ValueError):
        if job is not None:
            job.close()
        raise CLIUnavailableError("Could not start Codex app-server") from None
    responses = None
    try:
        if job is not None:
            job.assign(process.pid)
        responses = _Responses(process, deadline)
        _send(process, _REQUESTS[0])
        responses.response(1)
        _send(process, _REQUESTS[1])
        _send(process, _REQUESTS[2])
        return responses.response(2)
    finally:
        try:
            if responses is not None:
                responses.close()
        finally:
            try:
                if job is None:
                    _terminate(process)
                else:
                    _terminate(process, job)
            finally:
                if responses is not None:
                    responses.join()
