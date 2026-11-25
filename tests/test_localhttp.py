"""A dev server bound to every interface (0.0.0.0 or [::]) is seen as listening and healthy on localhost."""
import http.server
import socket
import threading
import unittest

from dev_factory import localhttp


class _Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()

    def log_message(self, *args):
        pass


class WildcardListenerTests(unittest.TestCase):
    def serve(self, family, host):
        server_cls = type("S", (http.server.HTTPServer,), {"address_family": family})
        srv = server_cls((host, 0), _Redirect)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_ipv4_wildcard(self):
        port = self.serve(socket.AF_INET, "0.0.0.0")
        self.assertTrue(localhttp.listening(f"http://localhost:{port}/"))
        self.assertTrue(localhttp.healthy(f"http://localhost:{port}/", 2))

    @unittest.skipUnless(socket.has_ipv6, "no IPv6")
    def test_ipv6_wildcard(self):
        port = self.serve(socket.AF_INET6, "::")
        self.assertTrue(localhttp.healthy(f"http://localhost:{port}/", 2))


if __name__ == "__main__":
    unittest.main()
