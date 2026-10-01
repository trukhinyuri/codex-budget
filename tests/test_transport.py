"""Offline fake-process tests; these never launch the installed Codex CLI."""

import copy
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))
from codex_budget import transport


@unittest.skipUnless(sys.platform in ("darwin", "linux", "win32"), "supported process fixtures")
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "CLI fixtures"
        self.directory.mkdir()
        self.log = self.directory / "requests.jsonl"

    def fake(self, body):
        script = self.directory / ("fake-codex.py" if sys.platform == "win32" else "fake-codex")
        prefix = (
            f"#!{sys.executable}\n"
            "import json, os, signal, subprocess, sys, time\n"
            f"log_path = {str(self.log)!r}\n"
            "def read():\n"
            "    line = sys.stdin.readline()\n"
            "    message = json.loads(line)\n"
            "    with open(log_path, 'a') as log:\n"
            "        log.write(json.dumps(message) + '\\n')\n"
            "    return message\n"
            "def send(message):\n"
            "    print(json.dumps(message), flush=True)\n"
        )
        script.write_text(prefix + body, encoding="utf-8")
        script.chmod(0o700)
        if sys.platform == "win32":
            launcher = self.directory / "fake-codex.cmd"
            launcher.write_text(f'@echo off\n@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
            return launcher
        return script

    def call(self, body, timeout=2):
        return transport.read_rate_limits(self.fake(body), timeout=timeout)

    def logged(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_handshake_order_whitelist_notifications_and_result(self):
        result = self.call(
            "assert sys.argv[1:] == ['app-server']\n"
            f"assert os.getcwd() == {str(TOOL_DIR / 'codex_budget')!r}\n"
            "first = read()\n"
            "send({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {}}})\n"
            "send({'id': first['id'], 'result': {'userAgent': 'fake'}})\n"
            "read()\n"
            "last = read()\n"
            "send({'jsonrpc': '2.0', 'method': 'remoteControl/status/changed', 'params': {}, 'emittedAtMs': 1234})\n"
            "send({'id': last['id'], 'result': {'rateLimits': {'secondary': {'usedPercent': 12}}}})\n"
            "time.sleep(10)\n"
        )
        self.assertEqual(result, {"rateLimits": {"secondary": {"usedPercent": 12}}})
        self.assertEqual(
            self.logged(),
            [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "codex_budget", "version": "1.0"},
                        "capabilities": None,
                    },
                },
                {"method": "initialized"},
                {
                    "id": 2,
                    "method": "account/rateLimits/read",
                    "params": {"supportsLunaReserve": False, "excludeResetCreditDetails": True},
                },
            ],
        )

    def test_rpc_error_is_safe_and_no_followup_after_failed_initialize(self):
        with self.assertRaises(transport.ServerRejectedError) as captured:
            self.call(
                "first = read()\n"
                "print('CREDENTIAL_SECRET_STDERR', file=sys.stderr, flush=True)\n"
                "send({'id': first['id'], 'error': {'code': -32600, 'message': 'CREDENTIAL_SECRET'}})\n"
                "time.sleep(10)\n"
            )
        self.assertIn("-32600", str(captured.exception))
        self.assertNotIn("CREDENTIAL_SECRET", str(captured.exception))
        self.assertEqual(len(self.logged()), 1)

    def test_read_rpc_error(self):
        with self.assertRaisesRegex(transport.TransportError, "rejected"):
            self.call(
                "first = read()\n"
                "send({'id': first['id'], 'result': {}})\n"
                "read()\n"
                "last = read()\n"
                "send({'id': last['id'], 'error': {'code': -32000, 'message': 'private'}})\n"
            )
        self.assertEqual(len(self.logged()), 3)

    @unittest.skipIf(sys.platform == "win32", "POSIX signals; Windows tree fixture below")
    def test_global_timeout_and_group_termination(self):
        marker = self.directory / "child-terminated"
        pidfile = self.directory / "parent-pid"
        child_code = (
            "import signal, pathlib, time; "
            f"signal.signal(signal.SIGTERM, lambda *_: (pathlib.Path({str(marker)!r}).write_text('done'), exit(0))); "
            "print('ready', flush=True); time.sleep(20)"
        )
        binary = self.fake(
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], stdout=subprocess.PIPE)\n"
            "child.stdout.readline()\n"
            "def stop(*_):\n"
            "    child.wait(timeout=1)\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "read()\n"
            "time.sleep(20)\n"
        )
        started = time.monotonic()
        with self.assertRaisesRegex(transport.TransportTimeoutError, "timed out"):
            transport.read_rate_limits(binary, timeout=2)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(marker.read_text(), "done")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    @unittest.skipIf(sys.platform == "win32", "POSIX signals")
    def test_sigkill_for_process_ignoring_term(self):
        pidfile = self.directory / "parent-pid"
        binary = self.fake(
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "read()\n"
            "time.sleep(20)\n"
        )
        with self.assertRaisesRegex(transport.TransportError, "timed out"):
            transport.read_rate_limits(binary, timeout=1.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_missing_binary_and_no_process(self):
        with mock.patch.object(transport.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(transport.TransportError, "unavailable"):
                transport.read_rate_limits(self.directory / "missing")
        popen.assert_not_called()

    def test_default_binary_resolution_embedded_then_path(self):
        fake = self.fake("")
        with (
            mock.patch.object(transport.sys, "platform", "darwin"),
            mock.patch.object(transport, "MACOS_CLI_PATHS", (str(fake),)),
            mock.patch.object(transport.shutil, "which") as which,
        ):
            self.assertEqual(transport._resolve_cli(None), str(fake))
            which.assert_not_called()
        with (
            mock.patch.object(transport.sys, "platform", "darwin"),
            mock.patch.object(transport, "MACOS_CLI_PATHS", (str(self.directory / "missing"),)),
            mock.patch.object(transport.shutil, "which", return_value=str(fake)) as which,
        ):
            self.assertEqual(transport._resolve_cli(None), str(fake))
            which.assert_called_once_with("codex")
        with (
            mock.patch.object(transport.sys, "platform", "darwin"),
            mock.patch.object(transport, "MACOS_CLI_PATHS", (str(self.directory / "missing"),)),
            mock.patch.object(transport.shutil, "which", return_value=None),
        ):
            with self.assertRaises(transport.CLIUnavailableError):
                transport._resolve_cli(None)

    def test_macos_codex_bundle_fallback_and_explicit_path(self):
        fake = self.fake("")
        unreadable = self.directory / "not-executable"
        unreadable.write_text("")
        unreadable.chmod(0o600)
        real_access = os.access
        with (
            mock.patch.object(transport.sys, "platform", "darwin"),
            mock.patch.object(
                transport,
                "MACOS_CLI_PATHS",
                (str(self.directory / "ChatGPT.app-missing"), str(unreadable), str(fake)),
            ),
            mock.patch.object(
                transport.os,
                "access",
                side_effect=lambda path, mode: path != str(unreadable) and real_access(path, mode),
            ),
            mock.patch.object(transport.shutil, "which") as which,
        ):
            self.assertEqual(transport._resolve_cli(None), str(fake))
            self.assertEqual(transport._resolve_cli(fake), str(fake))
            which.assert_not_called()
        with mock.patch.object(transport.shutil, "which") as which:
            with self.assertRaises(transport.CLIUnavailableError):
                transport._resolve_cli(self.directory / "explicit-missing")
            which.assert_not_called()

    def test_linux_uses_path_without_macos_bundles(self):
        fake = self.fake("")
        with (
            mock.patch.object(transport.sys, "platform", "linux"),
            mock.patch.object(transport, "MACOS_CLI_PATHS", ("/unused/mac/path",)),
            mock.patch.object(transport.shutil, "which", return_value=str(fake)) as which,
            mock.patch.object(transport.os.path, "isfile", wraps=os.path.isfile) as isfile,
        ):
            self.assertEqual(transport._resolve_cli(None), str(fake))
            which.assert_called_once_with("codex")
            self.assertNotIn(mock.call("/unused/mac/path"), isfile.call_args_list)

    def test_malformed_and_unexpected_output(self):
        cases = [
            "print('private non-JSON log', flush=True)",
            "print('{', flush=True)",
            "send([])",
            "send({'id': 999, 'result': {}})",
            "send({'id': True, 'result': {}})",
            "send({'id': 1.0, 'result': {}})",
            "send({'id': '1', 'result': {}})",
            "send({'id': None, 'result': {}})",
            "send({'result': {}})",
            "send({'id': 1, 'result': []})",
            "send({'id': 1, 'result': {}, 'error': {}})",
            "send({'id': 1, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}})",
            "send({'jsonrpc': '1.0', 'id': 1, 'result': {}})",
            "send({'id': 1, 'result': {}, 'params': {}})",
            'print(\'{"id":1,"id":2,"result":{}}\', flush=True)',
            'print(\'{"id":1,"result":{"x":1,"x":2}}\', flush=True)',
            'print(\'{"id":1,"result":{"x":NaN}}\', flush=True)',
            'print(\'{"id":1,"result":{"x":Infinity}}\', flush=True)',
            'print(\'{"id":1,"result":{"x":1e999}}\', flush=True)',
            "sys.stdout.buffer.write(b'\\xff\\n'); sys.stdout.buffer.flush()",
        ]
        for output in cases:
            with self.subTest(output=output):
                with self.assertRaises(transport.ProtocolError) as captured:
                    self.call("read()\n" + output + "\ntime.sleep(10)\n")
                self.assertNotIn("private", str(captured.exception))
                self.assertNotIn("chatgptAuthTokens", str(captured.exception))

    def test_callbacks_stop_without_sending_any_callback_response(self):
        for identifier in [0, 1, 2, None, "callback"]:
            with self.subTest(identifier=identifier):
                self.log.unlink(missing_ok=True)
                with self.assertRaises(transport.ProtocolError):
                    self.call(
                        "read()\n"
                        + f"send({{'id': {identifier!r}, 'method': 'some/callback', 'params': {{}}}})\n"
                        + "time.sleep(10)\n"
                    )
                self.assertEqual(len(self.logged()), 1)

    def test_duplicate_handshake_response_during_read_is_rejected(self):
        with self.assertRaisesRegex(transport.ProtocolError, "identifier"):
            self.call(
                "first = read()\n"
                "send({'id': first['id'], 'result': {}})\n"
                "read()\nread()\n"
                "send({'id': first['id'], 'result': {}})\n"
            )

    def test_depth_limit_ignores_quoted_brackets_and_escaped_quotes(self):
        with mock.patch.object(transport, "MAX_JSON_DEPTH", 4):
            with self.assertRaises(transport.OutputLimitError):
                self.call(
                    "read()\nprint('{\"id\":1,\"result\":{\"x\":' + '[' * 5 + '0' + ']' * 5 + '}}', flush=True)\n"
                )
            text = ("[]{}" * 100) + '"\\'
            result = self.call(
                "first = read()\nsend({'id': first['id'], 'result': {}})\n"
                "read()\nlast = read()\n"
                + f"send({{'id': last['id'], 'result': {{'text': {text!r}}}}})\n"
            )
            self.assertEqual(result, {"text": text})

    def test_early_eof_and_partial_line(self):
        for output in ["", "sys.stdout.write('{'); sys.stdout.flush()"]:
            with self.subTest(output=output):
                with self.assertRaisesRegex(transport.TransportError, "closed output"):
                    self.call("read()\n" + output + "\n")

    def test_bounded_line_session_and_notifications(self):
        with mock.patch.object(transport, "MAX_LINE_BYTES", 64):
            with self.assertRaisesRegex(transport.TransportError, "line limit"):
                self.call("read()\nprint('x' * 65, flush=True)\ntime.sleep(10)\n")
        with mock.patch.object(transport, "MAX_STDOUT_BYTES", 100):
            with self.assertRaisesRegex(transport.TransportError, "session limit"):
                self.call(
                    "read()\nsys.stdout.write(' ' * 101); sys.stdout.flush()\ntime.sleep(10)\n"
                )
        with mock.patch.object(transport, "MAX_MESSAGES", 3):
            with self.assertRaisesRegex(transport.TransportError, "too many messages"):
                self.call("read()\nfor _ in range(4): send({'method': 'n'})\ntime.sleep(10)\n")

    def test_outgoing_whitelist_rejects_before_write(self):
        process = mock.Mock()
        for method in [
            "thread/start",
            "turn/start",
            "account/logout",
            "account/rateLimitResetCredit/consume",
        ]:
            with self.subTest(method=method):
                with self.assertRaisesRegex(transport.TransportError, "Disallowed"):
                    transport._send(process, {"method": method})
        process.stdin.write.assert_not_called()

    def test_whitelisted_methods_cannot_change_envelope_or_parameters(self):
        process = mock.Mock()
        messages = [
            {"method": []},
            [],
            {"method": "initialized", "id": 1},
            {"id": 2, "method": "account/rateLimits/read"},
            {"id": True, "method": "initialize", "params": transport._REQUESTS[0]["params"]},
        ]
        changed = copy.deepcopy(transport._REQUESTS[2])
        changed["params"]["excludeResetCreditDetails"] = False
        messages.append(changed)
        changed = copy.deepcopy(transport._REQUESTS[0])
        changed["params"]["capabilities"] = {"experimentalApi": True}
        messages.append(changed)
        for message in messages:
            with self.subTest(message=message):
                with self.assertRaises(transport.ProtocolError):
                    transport._send(process, message)
        process.stdin.write.assert_not_called()

    def test_send_failures_and_partial_writes_are_safe(self):
        for effect in [BrokenPipeError("SECRET"), OSError("SECRET"), ValueError("SECRET")]:
            process = mock.Mock()
            process.stdin.write.side_effect = effect
            with (
                self.subTest(effect=type(effect)),
                self.assertRaises(transport.TransportError) as captured,
            ):
                transport._send(process, transport._REQUESTS[0])
            self.assertNotIn("SECRET", str(captured.exception))
        process = mock.Mock()
        process.stdin.write.return_value = 1
        with self.assertRaises(transport.TransportError):
            transport._send(process, transport._REQUESTS[0])
        process.stdin.flush.assert_not_called()

    def test_invalid_timeout_does_not_start_a_process(self):
        for timeout in [0, -1, float("nan"), float("inf"), True, "20", 10**1000]:
            with (
                self.subTest(timeout=timeout),
                mock.patch.object(transport.subprocess, "Popen") as popen,
            ):
                with self.assertRaises(transport.TransportError):
                    transport.read_rate_limits(timeout=timeout)
                popen.assert_not_called()

    def test_invalid_paths_never_start_process_or_fall_back(self):
        for path in ["", b"bytes-path", "private\x00path", object()]:
            with (
                self.subTest(path=type(path)),
                mock.patch.object(transport.subprocess, "Popen") as popen,
                mock.patch.object(transport.shutil, "which") as which,
            ):
                with self.assertRaises(transport.CLIUnavailableError):
                    transport.read_rate_limits(path)
                popen.assert_not_called()
                which.assert_not_called()

    def test_startup_failure_is_typed_and_safe(self):
        binary = self.fake("")
        with mock.patch.object(transport.subprocess, "Popen", side_effect=OSError("SECRET/path")):
            with self.assertRaises(transport.CLIUnavailableError) as captured:
                transport.read_rate_limits(binary)
        self.assertEqual(str(captured.exception), "Could not start Codex app-server")

    @unittest.skipIf(sys.platform == "win32", "controlled POSIX selector clock")
    def test_one_deadline_covers_handshake_and_rate_read(self):
        # A controlled clock removes process-startup timing from this assertion:
        # initialization takes 0.75s of a 1s budget, then the read takes 0.75s.
        current = [10.0]
        process = mock.Mock(stdin=io.BytesIO(), stdout=mock.Mock())
        selector = mock.Mock()

        def ready(_remaining):
            current[0] += 0.75
            return [mock.sentinel.ready]

        selector.select.side_effect = ready
        with (
            mock.patch.object(transport.time, "monotonic", side_effect=lambda: current[0]),
            mock.patch.object(transport.subprocess, "Popen", return_value=process),
            mock.patch.object(transport.selectors, "DefaultSelector", return_value=selector),
            mock.patch.object(
                transport.os,
                "read",
                side_effect=[b'{"id":1,"result":{}}\n', b'{"id":2,"result":{}}\n'],
            ),
            mock.patch.object(transport, "_terminate") as terminate,
        ):
            with self.assertRaises(transport.TransportTimeoutError):
                transport.read_rate_limits(self.fake(""), timeout=1)
        messages = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(len(messages), 3)
        self.assertEqual(selector.select.call_args_list, [mock.call(1.0), mock.call(0.25)])
        terminate.assert_called_once_with(process)

    @unittest.skipIf(sys.platform == "win32", "POSIX selector failure")
    def test_process_and_pipes_cleaned_when_selector_setup_fails(self):
        binary = self.fake("time.sleep(10)\n")
        started = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            started.append(process)
            return process

        selector = mock.Mock()
        selector.register.side_effect = OSError("SECRET")
        with (
            mock.patch.object(transport.subprocess, "Popen", side_effect=spawn),
            mock.patch.object(transport.selectors, "DefaultSelector", return_value=selector),
        ):
            with self.assertRaises(transport.TransportError) as captured:
                transport.read_rate_limits(binary)
        self.assertNotIn("SECRET", str(captured.exception))
        selector.close.assert_called_once()
        self.assertIsNotNone(started[0].poll())
        self.assertTrue(started[0].stdin.closed)
        self.assertTrue(started[0].stdout.closed)

    @unittest.skipIf(sys.platform == "win32", "POSIX selector failure")
    def test_selector_and_read_errors_never_include_os_text(self):
        process = mock.Mock(stdout=mock.Mock())
        for operation in ["select", "read"]:
            selector = mock.Mock()
            selector.select.return_value = [mock.sentinel.ready]
            if operation == "select":
                selector.select.side_effect = OSError("SECRET")
            with (
                self.subTest(operation=operation),
                mock.patch.object(transport.selectors, "DefaultSelector", return_value=selector),
                mock.patch.object(transport.os, "read", side_effect=OSError("SECRET")),
            ):
                responses = transport._Responses(process, time.monotonic() + 1)
                try:
                    with self.assertRaises(transport.TransportError) as captured:
                        responses.response(1)
                    self.assertNotIn("SECRET", str(captured.exception))
                finally:
                    responses.close()
            selector.close.assert_called_once()

    @unittest.skipIf(sys.platform == "win32", "POSIX signals")
    def test_cleanup_signals_group_even_when_leader_exited(self):
        process = mock.Mock(pid=123456, stdin=None, stdout=None, stderr=None)
        process.wait.return_value = 0
        with mock.patch.object(transport.os, "killpg") as killpg:
            transport._terminate(process)
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(123456, signal.SIGTERM), mock.call(123456, signal.SIGKILL)],
        )


class PlatformTests(unittest.TestCase):
    def test_unsupported_platform_fails_closed_before_cli_lookup(self):
        for platform in ["cygwin", "freebsd"]:
            with (
                self.subTest(platform=platform),
                mock.patch.object(transport.sys, "platform", platform),
                mock.patch.object(transport, "_resolve_cli") as resolve,
                mock.patch.object(transport.subprocess, "Popen") as popen,
            ):
                with self.assertRaises(transport.UnsupportedPlatformError):
                    transport.read_rate_limits()
                resolve.assert_not_called()
                popen.assert_not_called()


class WindowsPolicyTests(unittest.TestCase):
    def test_native_and_batch_windows_commands(self):
        with (
            mock.patch.object(transport.sys, "platform", "win32"),
            mock.patch.object(
                transport, "_windows_system_tool", return_value=r"C:\Windows\System32\cmd.exe"
            ),
        ):
            command, options = transport._command(r"C:\CLI fixtures\codex.cmd")
            self.assertEqual(
                command,
                '"C:\\Windows\\System32\\cmd.exe" /d /v:off /s /c ""C:\\CLI fixtures\\codex.cmd" "app-server""',
            )
            self.assertEqual(options["creationflags"], 0x200)
            self.assertNotIn("start_new_session", options)
            command, options = transport._command(r"C:\CLI fixtures\codex.exe")
            self.assertEqual(command, [r"C:\CLI fixtures\codex.exe", "app-server"])
            self.assertNotIn("executable", options)
            for path in [r"C:\%PRIVATE%\codex.cmd", "C:\\bad\npath\\codex.cmd"]:
                with self.assertRaises(transport.CLIUnavailableError):
                    transport._command(path)

    def test_assignment_failure_is_closed_before_rpc_and_cleans_process(self):
        process = mock.Mock()
        job = mock.Mock()
        job.assign.side_effect = transport.TransportError(
            "Could not assign Windows app-server process containment"
        )
        with (
            mock.patch.object(transport.sys, "platform", "win32"),
            mock.patch.object(transport, "_resolve_cli", return_value=r"C:\codex.exe"),
            mock.patch.object(transport, "_WindowsJob", return_value=job),
            mock.patch.object(transport.subprocess, "Popen", return_value=process),
            mock.patch.object(transport, "_Responses") as responses,
            mock.patch.object(transport, "_send") as send,
            mock.patch.object(transport, "_terminate") as terminate,
        ):
            with self.assertRaises(transport.TransportError):
                transport.read_rate_limits()
        responses.assert_not_called()
        send.assert_not_called()
        terminate.assert_called_once_with(process, job)

    def test_windows_cleanup_fallback_and_timeout_are_bounded(self):
        for failure in [None, OSError("PRIVATE"), subprocess.TimeoutExpired("taskkill", 2)]:
            process = mock.Mock(pid=123456)
            job = mock.Mock()
            with (
                self.subTest(failure=type(failure)),
                mock.patch.object(transport.sys, "platform", "win32"),
                mock.patch.object(transport, "_windows_system_tool", return_value="taskkill.exe"),
                mock.patch.object(transport.subprocess, "run", side_effect=failure) as run,
            ):
                transport._terminate(process, job)
            self.assertEqual(run.call_args.args[0], ["taskkill.exe", "/PID", "123456", "/T", "/F"])
            self.assertEqual(run.call_args.kwargs["timeout"], transport._WINDOWS_CLEANUP_TIMEOUT)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            job.close.assert_called_once_with()
            process.kill.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=transport._CLEANUP_GRACE)
            process.stdin.close.assert_called_once_with()
            process.stdout.close.assert_called_once_with()


class WindowsReaderTests(unittest.TestCase):
    def test_threaded_pipe_read_and_eof_on_each_host(self):
        reader, writer = os.pipe()
        stdout = os.fdopen(reader, "rb", buffering=0)
        self.addCleanup(stdout.close)
        os.write(writer, b'{"id":1,"result":{}}\n')
        os.close(writer)
        with mock.patch.object(transport.sys, "platform", "win32"):
            responses = transport._Responses(mock.Mock(stdout=stdout), time.monotonic() + 2)
            try:
                self.assertEqual(responses.response(1), {})
                with self.assertRaisesRegex(transport.TransportError, "closed output"):
                    responses.response(2)
            finally:
                responses.close()
                responses.join()
        self.assertFalse(responses.worker.is_alive())

    def test_reader_queue_never_grows_without_a_consumer(self):
        with (
            mock.patch.object(transport.sys, "platform", "win32"),
            mock.patch.object(
                transport.os, "read", return_value=b"x" * transport._READ_CHUNK
            ) as read,
        ):
            responses = transport._Responses(mock.Mock(stdout=mock.Mock()), time.monotonic() + 2)
            try:
                deadline = time.monotonic() + 2
                while (
                    responses.chunks.qsize() < transport._QUEUE_CHUNKS
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.001)
                self.assertEqual(responses.chunks.qsize(), transport._QUEUE_CHUNKS)
                self.assertLessEqual(read.call_count, transport._QUEUE_CHUNKS + 1)
            finally:
                responses.close()
                responses.join()
        self.assertFalse(responses.worker.is_alive())

    def test_threaded_read_error_is_safe(self):
        with (
            mock.patch.object(transport.sys, "platform", "win32"),
            mock.patch.object(transport.os, "read", side_effect=OSError("PRIVATE")),
        ):
            responses = transport._Responses(mock.Mock(stdout=mock.Mock()), time.monotonic() + 2)
            try:
                with self.assertRaises(transport.TransportError) as captured:
                    responses.response(1)
                self.assertNotIn("PRIVATE", str(captured.exception))
            finally:
                responses.close()
                responses.join()
        self.assertFalse(responses.worker.is_alive())


@unittest.skipUnless(sys.platform == "win32", "real Windows Job Object/process fixtures")
class WindowsProcessTests(unittest.TestCase):
    setUp = TransportTests.setUp
    fake = TransportTests.fake

    def open_process(self, pid):
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, own fixture only
        self.assertTrue(handle)
        self.addCleanup(api.CloseHandle, handle)
        return api, handle

    def test_windows_timeout_cleans_cmd_launcher_parent_and_child(self):
        parent_file = self.directory / "parent-pid"
        child_file = self.directory / "child-pid"
        binary = self.fake(
            "read()\n"
            f"open({str(parent_file)!r}, 'w').write(str(os.getpid()))\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(child_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(30)\n"
        )
        handles = []
        real_terminate = transport._terminate

        def observe_and_terminate(process, job):
            try:
                handles.append(self.open_process(process.pid))
                handles.append(self.open_process(int(parent_file.read_text())))
                handles.append(self.open_process(int(child_file.read_text())))
            finally:
                real_terminate(process, job)

        started = time.monotonic()
        with mock.patch.object(transport, "_terminate", side_effect=observe_and_terminate):
            with self.assertRaises(transport.TransportTimeoutError):
                transport.read_rate_limits(binary, timeout=3)
        self.assertLess(time.monotonic() - started, 7)
        self.assertEqual(len(handles), 3)
        for api, handle in handles:
            self.assertEqual(api.WaitForSingleObject(handle, 3000), 0)

    def test_windows_job_kills_child_after_assigned_parent_exits(self):
        child_file = self.directory / "surviving-child-pid"
        script = self.directory / "early-exit.py"
        script.write_text(
            "import subprocess, sys\n"
            "sys.stdin.readline()\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(child_file)!r}, 'w').write(str(child.pid))\n",
            encoding="utf-8",
        )
        job = transport._WindowsJob()
        process = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            job.assign(process.pid)
            process.stdin.write(b"go\n")
            process.stdin.flush()
            process.wait(timeout=5)
            api, child_handle = self.open_process(int(child_file.read_text()))
            self.assertEqual(api.WaitForSingleObject(child_handle, 0), 258)  # WAIT_TIMEOUT
            job.close()
            self.assertEqual(api.WaitForSingleObject(child_handle, 3000), 0)
        finally:
            transport._terminate(process, job)

    def test_windows_job_assignment_failure_sends_no_rpc(self):
        binary = self.fake("time.sleep(30)\n")
        job = mock.Mock()
        job.assign.side_effect = transport.TransportError(
            "Could not assign Windows app-server process containment"
        )
        real_terminate = transport._terminate
        observed = []

        def observe_and_terminate(process, containment):
            try:
                observed.append(self.open_process(process.pid))
            finally:
                real_terminate(process, containment)

        with (
            mock.patch.object(transport, "_WindowsJob", return_value=job),
            mock.patch.object(transport, "_terminate", side_effect=observe_and_terminate),
        ):
            with self.assertRaises(transport.TransportError):
                transport.read_rate_limits(binary)
        self.assertFalse(self.log.exists())
        self.assertEqual(len(observed), 1)
        api, handle = observed[0]
        self.assertEqual(api.WaitForSingleObject(handle, 3000), 0)
        job.close.assert_called_once_with()

    def test_windows_path_discovers_cmd_without_macos_bundles(self):
        binary = self.fake("")
        command = self.directory / "codex.cmd"
        command.write_bytes(binary.read_bytes())
        with (
            mock.patch.dict(os.environ, {"PATH": str(self.directory), "PATHEXT": ".EXE;.CMD"}),
            mock.patch.object(transport, "MACOS_CLI_PATHS", ("/unused/mac/path",)),
        ):
            self.assertEqual(transport._resolve_cli(None), str(command))


if __name__ == "__main__":
    unittest.main()
