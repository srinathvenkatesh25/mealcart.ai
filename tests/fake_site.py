"""A tiny local stand-in for Instacart's login flow, served over real HTTP."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = """<!doctype html><html><body>
<header id="hdr"></header>
<div id="dlg" role="dialog" hidden></div>
<script>
const MODE = "%(mode)s";
const loggedIn = document.cookie.includes("session=1");
const hdr = document.getElementById("hdr"), dlg = document.getElementById("dlg");
hdr.innerHTML = loggedIn ? '<button>Account</button>' : '<button id="login">Log in</button><button>Sign up</button>';
function step(html, onContinue) {
  dlg.hidden = false;
  dlg.innerHTML = html + '<button id="cont">Continue</button>';
  document.getElementById("cont").onclick = onContinue;
}
function finish() {
  document.cookie = "session=1; expires=Fri, 01 Jan 2100 00:00:00 GMT; path=/";
  location.reload();
}
if (!loggedIn) document.getElementById("login").onclick = () =>
  step('<h2>Log in</h2><input type="email" id="email">', () => {
    if (!document.getElementById("email").value.includes("@")) return;
    if (MODE === "password")
      step('<input type="password" id="pw">', () => {
        if (document.getElementById("pw").value === "hunter22") finish();
      });
    else if (MODE === "autosubmit") {
      // Like real Instacart: no button; the code submits itself at 6 digits.
      dlg.innerHTML = '<input autocomplete="one-time-code" id="code">';
      document.getElementById("code").oninput = (e) => {
        if (e.target.value === "123456") finish();
      };
    } else
      step('<input autocomplete="one-time-code" id="code">', () => {
        if (document.getElementById("code").value === "123456") finish();
      });
  });
</script></body></html>"""


class FakeInstacart:
    def __init__(self, mode: str = "code"):
        self.mode = mode
        self.requests: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body: str, ctype="text/html"):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):
                site.requests.append(f"GET {self.path}")
                if self.path.startswith("/store/checkout"):
                    self._send("<h1>CHECKOUT</h1>")
                else:
                    self._send(HOME % {"mode": site.mode})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    op = json.loads(raw or b"{}").get("operationName")
                except ValueError:
                    op = "<binary>"
                site.requests.append(f"POST {self.path} {op}")
                self._send("{}", "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
