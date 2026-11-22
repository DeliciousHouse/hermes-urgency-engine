"""Loopback contract tests for the optional gateway liveness probe."""
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'gateway_liveness.py'


class ResponseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, response):
        self.response = response
        self.request_bytes = b''
        super().__init__(('127.0.0.1', 0), ResponseHandler)


class ResponseHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(2)
            with self.request.makefile('rb') as incoming:
                request = b''
                while len(request) < 4096 and not request.endswith(b'\r\n\r\n'):
                    line = incoming.readline(4096 - len(request))
                    if not line:
                        return
                    request += line
            self.server.request_bytes = request  # type: ignore[attr-defined]
            response = self.server.response  # type: ignore[attr-defined]
            if isinstance(response, bytes):
                self.request.sendall(response)
            else:
                for piece in response:
                    self.request.sendall(piece)
                    time.sleep(0.2)
        except OSError:
            pass


class GatewayLivenessTests(unittest.TestCase):
    def probe_response(self, response):
        with ResponseServer(response) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                return subprocess.run([sys.executable, str(SCRIPT), str(server.server_address[1])],
                                      capture_output=True, text=True, timeout=6)
            finally:
                server.shutdown()
                thread.join()

    def test_documented_healthy_response(self):
        body = b'{"status":"ok"}'
        response = b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n' + body
        result = self.probe_response(response)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_native_healthy_response(self):
        body = b'{"status":"ok","platform":"hermes-agent","version":"1.2"}'
        response = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
        self.assertEqual(self.probe_response(response).returncode, 0)

    def test_rejects_nonexact_json(self):
        for body in (b'{"status":"fail"}', b'{"status":"ok","extra":1}',
                     b'{"status":"ok","status":"ok"}', b'{bad', b'[]',
                     b'{"status":"ok","platform":"hermes-agent","version":"1","version":"1"}',
                     b'{"status":"ok","platform":"hermes-agent","version":""}',
                     b'{"status":"ok","platform":"other","version":"1"}',
                     b'{"status":"ok","platform":"hermes-agent","version":1}'):
            with self.subTest(body=body):
                response = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
                result = self.probe_response(response)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, 'Gateway health check failed.\n')

    def test_rejects_non_utf8_json(self):
        body = '{"status":"ok"}'.encode('utf-16')
        response = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
        self.assertEqual(self.probe_response(response).returncode, 1)

    def test_rejects_invalid_http_and_oversized_responses(self):
        body = b'{"status":"ok"}'
        for response in (
            b'HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\nContent-Length: 15\r\n\r\n' + body,
            b'HTTP/1.1 500 Error\r\nContent-Length: 15\r\n\r\n' + body,
            b'HTTP/1.1 200 OK\r\n\r\n' + body,
            b'HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n' + body,
            b'HTTP/1.1 200 OK\r\nContent-Length: 9000\r\n\r\n' + body,
            b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\nContent-Length: 15\r\n\r\n' + body,
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + body,
            b'HTTP/1.1 200 OK\r\nX-Large: ' + b'x' * 17000 + b'\r\nContent-Length: 15\r\n\r\n' + body,
        ):
            with self.subTest(response=response[:100]):
                self.assertEqual(self.probe_response(response).returncode, 1)

    def test_redirect_does_not_contact_the_destination(self):
        healthy = b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{"status":"ok"}'
        with ResponseServer(healthy) as target:
            thread = threading.Thread(target=target.serve_forever, kwargs={'poll_interval': 0.01})
            thread.start()
            try:
                port = target.server_address[1]
                redirect = (f'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/health\r\n'
                            'Content-Length: 0\r\n\r\n').encode('ascii')
                self.assertEqual(self.probe_response(redirect).returncode, 1)
                self.assertEqual(target.request_bytes, b'')
            finally:
                target.shutdown()
                thread.join()

    def test_slow_trickle_is_bounded(self):
        response = [b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n'] + [b' '] * 30
        start = time.monotonic()
        result = self.probe_response(response)
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, 'Gateway health check failed.\n')
        # gstack-timing-ok: distinguish the shared 3s deadline from a per-read timeout.
        self.assertGreater(elapsed, 2.5)
        self.assertLess(elapsed, 5)

    def test_stalled_headers_are_bounded(self):
        start = time.monotonic()
        result = self.probe_response([b'HTTP/1.1 200 OK\r\n'] + [b''] * 25)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, 'Gateway health check failed.\n')
        # gstack-timing-ok: a silent peer cannot outlast the probe deadline.
        self.assertLess(time.monotonic() - start, 5)

    def test_request_is_fixed_and_ignores_proxy_environment(self):
        response = b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n{"status":"ok"}'
        with ResponseServer(response) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                env = dict(os.environ, HTTP_PROXY='http://127.0.0.1:1',
                           ALL_PROXY='http://127.0.0.1:1', NO_PROXY='')
                result = subprocess.run([sys.executable, str(SCRIPT), str(server.server_address[1])],
                                        capture_output=True, text=True, timeout=6, env=env)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')
        self.assertEqual(server.request_bytes, b'GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')

    def test_unreachable_and_usage_are_distinct_and_safe(self):
        for args in ([], ['0'], ['65536'], ['-1'], ['1.2'], ['localhost'], ['１２'], ['123', 'extra']):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=6)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertEqual(result.stderr, 'Usage: gateway_liveness.py PORT\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            result = subprocess.run([sys.executable, str(SCRIPT), str(port)], capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'Gateway health check failed.\n')


if __name__ == '__main__':
    unittest.main()
