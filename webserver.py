"""Маленький веб-сервер для мінізастосунку «Розрахунки» (webapp/calc.html).
Працює поруч з агентом в окремому потоці. На Railway потрібна адреса: Settings → Networking → Generate Domain."""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import texts as T

log = logging.getLogger("webapp")
BOT_USERNAME = "Olenaprofinancebot"  # агент сам уточнює при запуску
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "calc.html")
ADVENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "advent.html")


def page():
    with open(PAGE, encoding="utf-8") as f:
        html = f.read()
    return (html.replace("{{RATE}}", str(T.CALC_RATE)).replace("{{INDEX}}", str(T.CALC_INDEX))
            .replace("{{PENSION_YEARS}}", json.dumps(list(T.CALC_PENSION_YEARS))).replace("{{BOT}}", BOT_USERNAME))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0].split("#")[0]
        if path in ("/calc", "/calc/"):
            body, ctype = page().encode("utf-8"), "text/html; charset=utf-8"
        elif path in ("/advent", "/advent/"):  # адвент-календар
            with open(ADVENT, "rb") as f:
                body, ctype = f.read(), "text/html; charset=utf-8"
        else:
            body, ctype = b"ok", "text/plain; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start(port):
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        log.warning("webapp server: %s", e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Мінізастосунок слухає порт %s", port)
