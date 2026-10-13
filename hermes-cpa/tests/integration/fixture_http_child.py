"""Bounded generated HTTP child; models protocol failures, not real CPA."""
import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

port = int(sys.argv[1])
variant = sys.argv[2]
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        authorized = self.headers.get('Authorization') == 'Bearer generated-management'
        self.send_response(200 if authorized else 401)
        self.send_header('X-CPA-VERSION', 'v8.0.21')
        self.end_headers()
        self.wfile.write(json.dumps({'hello': 'stranger'} if variant == 'wrong-service' else {'config-version': 8}).encode())
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
