#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with Path(sys.argv[1]).open('a') as log:
            print(json.dumps({'path': self.path, 'body': body, 'headers': dict(self.headers)}), file=log)
        messages = body['messages']
        prompt = str(next(message['content'] for message in reversed(messages) if message['role'] == 'user'))
        if self.path.startswith('/opencode-go/') or self.path.startswith('/vercel-ai-gateway/'):
            self.send_response(402 if self.path.startswith('/opencode-go/') else 429)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"provider unavailable"}}')
            return
        if 'glm-task' in prompt:
            delta, finish = {'content': 'glm-ok'}, 'stop'
        elif self.path.startswith('/vercel-ai-gateway-zai/'):
            delta, finish = {'content': ''}, 'stop'
        elif 'child-task' in prompt:
            delta, finish = {'content': 'child-ok'}, 'stop'
        else:
            tools = [message for message in messages if message['role'] == 'tool']
            if not tools:
                name, arguments = 'write', {'path': 'agent-marker.txt', 'content': 'tools-work'}
            elif len(tools) == 1:
                name, arguments = 'bash', {'command': 'pi -p "child-task" > child-answer.txt', 'timeout': 30}
            else:
                name, arguments = None, None
            if name:
                delta = {'tool_calls': [{'index': 0, 'id': f'call_{len(tools)}', 'type': 'function',
                                         'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
                finish = 'tool_calls'
            else:
                delta, finish = {'content': 'agent-ok'}, 'stop'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        chunks = [
            {'choices': [{'index': 0, 'delta': {'role': 'assistant', **delta}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]},
        ]
        for chunk in chunks:
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 0), StubHandler)
    print(server.server_port, flush=True)
    server.serve_forever()
