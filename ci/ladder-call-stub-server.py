#!/usr/bin/env python3
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/v1/chat/completions':
            self.send_response(404)
            self.end_headers()
            return
        if self.path == '/zen/go/v1/chat/completions':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            body = {'choices': [{'message': {'content': 'action-ok'}}]}
            self.wfile.write(json.dumps(body).encode())
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_):
        pass


def main():
    server = HTTPServer(('127.0.0.1', 0), StubHandler)
    port = server.server_address[1]
    print(port, flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    server.shutdown()


if __name__ == '__main__':
    main()