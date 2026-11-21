"""Explicit public-service policy; desktop defaults never read hosted environment."""
import os
import re
import threading
import time
from urllib.parse import urlsplit

from flask import g, jsonify, request


class HostedPolicy:
    def __init__(self, origin):
        parsed = urlsplit(origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.netloc != parsed.hostname
                or parsed.path or parsed.query or parsed.fragment
                or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', parsed.hostname)
                or '..' in parsed.hostname):
            raise ValueError('COLE_PUBLIC_ORIGIN must be an exact HTTPS origin without path or port.')
        self.origin = origin
        self.host = parsed.hostname
        self.slots = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.tokens = 40.0
        self.updated = time.monotonic()

    @staticmethod
    def rejection(code, message, status):
        response = jsonify(error={'code': code, 'message': message, 'fields': []})
        response.status_code = status
        if status in (429, 503):
            response.headers['Retry-After'] = '2'
        return response

    def check_request(self):
        # Do not trust client-supplied forwarded host/proto/IP headers. The dedicated
        # TLS-terminated entry point sets the WSGI scheme; Host is still allowlisted.
        if request.host.lower() != self.host or request.scheme != 'https':
            return self.rejection('invalid_host', 'Use the official HTTPS COLE address.', 403)
        if request.method == 'POST' and request.path in ('/api/query', '/api/preview', '/api/county'):
            with self.lock:
                now = time.monotonic()
                self.tokens = min(40.0, self.tokens + (now - self.updated) * 10)
                self.updated = now
                if self.tokens < 1:
                    return self.rejection('rate_limited', 'COLE is receiving many requests. Try again shortly.', 429)
                self.tokens -= 1
            if not self.slots.acquire(blocking=False):
                return self.rejection('server_busy', 'COLE is busy. Your current results are retained. Try again shortly.', 503)
            g.cole_slot = True

    def install(self, app):
        app.extensions['cole_hosted_policy'] = self

        @app.teardown_request
        def release_slot(error):
            if g.pop('cole_slot', False):
                self.slots.release()

        @app.after_request
        def hosted_headers(response):
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
            if request.path == '/' or (request.path.startswith('/api/') and not request.path.startswith('/api/geography/')):
                response.headers['Cache-Control'] = 'no-store'
            return response


def create_hosted_app():
    from .app import create_app
    origin = os.environ.get('COLE_PUBLIC_ORIGIN')
    if not origin:
        hostname = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
        if not hostname:
            raise RuntimeError('Configure COLE_PUBLIC_ORIGIN or RENDER_EXTERNAL_HOSTNAME.')
        origin = 'https://' + hostname
    return create_app(hosted_origin=origin)


def main():
    from waitress import serve
    app = create_hosted_app()
    # Render terminates TLS. Desktop serving uses its existing loopback launcher.
    serve(app, host='0.0.0.0', port=int(os.environ.get('PORT', '10000')),
          threads=4, connection_limit=64, channel_timeout=30,
          max_request_body_size=1024 * 1024, max_request_header_size=16384,
          url_scheme='https', clear_untrusted_proxy_headers=True)


if __name__ == '__main__':
    main()
