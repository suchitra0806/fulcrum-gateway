import io

from ax_cli.commands import gateway_ui as gateway_cmd

EXPECTED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}


def _make_handler(*, path="/healthz"):
    handler_cls = gateway_cmd._build_gateway_ui_handler(activity_limit=5, refresh_ms=1000)

    class FakeHandler(handler_cls):
        def __init__(self):
            self.path = path
            self.headers = {"Host": "127.0.0.1"}
            self.rfile = io.BytesIO(b"")
            self.status = None
            self.body = b""
            self.response_headers = []

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            self.response_headers.append((name, value))

        def end_headers(self):
            return None

        @property
        def wfile(self):
            outer = self

            class Writer:
                def write(self, data):
                    outer.body += data

            return Writer()

    return FakeHandler()


def _header_dict(handler):
    return {name: value for name, value in handler.response_headers}


class TestSecurityHeaders:
    def test_json_response_includes_security_headers(self):
        h = _make_handler(path="/healthz")
        h.do_GET()
        headers = _header_dict(h)
        for name, value in EXPECTED_HEADERS.items():
            assert headers.get(name) == value, f"Missing or wrong: {name}"
        assert "Content-Security-Policy" in headers
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    def test_html_response_includes_security_headers(self):
        h = _make_handler(path="/")
        h.do_GET()
        headers = _header_dict(h)
        for name, value in EXPECTED_HEADERS.items():
            assert headers.get(name) == value, f"Missing or wrong: {name}"

    def test_favicon_includes_security_headers(self):
        h = _make_handler(path="/favicon.svg")
        h.do_GET()
        headers = _header_dict(h)
        for name, value in EXPECTED_HEADERS.items():
            assert headers.get(name) == value, f"Missing or wrong: {name}"

    def test_csp_uses_nonce_for_html(self):
        h = _make_handler(path="/")
        h.do_GET()
        headers = _header_dict(h)
        csp = headers["Content-Security-Policy"]
        assert "script-src 'nonce-" in csp
        assert "style-src 'nonce-" in csp
        assert "'unsafe-inline'" not in csp

    def test_csp_no_nonce_for_json(self):
        h = _make_handler(path="/healthz")
        h.do_GET()
        headers = _header_dict(h)
        csp = headers["Content-Security-Policy"]
        assert "nonce-" not in csp
        assert "script-src" not in csp

    def test_html_injects_nonce_into_tags(self):
        h = _make_handler(path="/")
        h.do_GET()
        html = h.body.decode("utf-8")
        assert '<script nonce="' in html
        assert '<style nonce="' in html
        assert "<script>" not in html
        assert "<style>" not in html

    def test_no_hsts_header(self):
        h = _make_handler(path="/healthz")
        h.do_GET()
        header_names = {name for name, _ in h.response_headers}
        assert "Strict-Transport-Security" not in header_names

    def test_permissions_policy_present(self):
        h = _make_handler(path="/healthz")
        h.do_GET()
        headers = _header_dict(h)
        assert "Permissions-Policy" in headers
        assert "camera=()" in headers["Permissions-Policy"]
