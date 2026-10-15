from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from test_runtime import start_test_web


class WebHostSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = str(Path(temporary.name) / "host.db")

    def test_unauthenticated_server_rejects_foreign_host_for_reads_and_same_origin_writes(self):
        base = start_test_web(self, self.db)
        foreign_host = "cater.attacker.example:8766"
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                request = Request(base + "/api/tasks", method=method,
                    data=json.dumps({"goal": "Must not be created"}).encode() if method == "POST" else None,
                    headers={"Host": foreign_host, "Origin": "http://" + foreign_host,
                             "Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=3)
                self.assertEqual(raised.exception.code, 403)
        with urlopen(base + "/api/tasks", timeout=3) as response:
            self.assertEqual(json.load(response), [])

    def test_localhost_and_ipv6_loopback_hostnames_remain_usable(self):
        base = start_test_web(self, self.db)
        for host in ("localhost", "LOCALHOST", "127.0.0.1", "[::1]"):
            with self.subTest(host=host), urlopen(Request(base + "/api/tasks", headers={"Host": host}), timeout=3) as response:
                self.assertEqual(response.status, 200)

    def test_malformed_and_duplicate_host_headers_are_rejected(self):
        base, server, _ = start_test_web(self, self.db, return_server=True)
        for host in ("localhost.attacker.example", "localhost@attacker.example", "localhost/path",
                     "localhost?query", "localhost#fragment", "localhost:invalid", "localhost:99999"):
            with self.subTest(host=host):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(Request(base + "/api/tasks", headers={"Host": host}), timeout=3)
                self.assertEqual(raised.exception.code, 403)
        for hosts in ([], ["localhost", "cater.attacker.example"]):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.putrequest("GET", "/api/tasks", skip_host=True)
                for host in hosts:
                    connection.putheader("Host", host)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()

    def test_authenticated_remote_host_still_requires_token(self):
        base = start_test_web(self, self.db, access_token="fixture-access-token")
        headers = {"Host": "cater.example", "Origin": "https://cater.example", "Content-Type": "application/json"}
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(base + "/api/tasks", headers=headers), timeout=3)
        self.assertEqual(raised.exception.code, 401)
        headers["X-Cater-Access-Token"] = "fixture-access-token"
        with urlopen(Request(base + "/api/tasks", method="POST", headers=headers,
                     data=b'{"goal":"Authenticated proxy task"}'), timeout=3) as response:
            self.assertEqual(response.status, 201)


if __name__ == "__main__":
    unittest.main()
