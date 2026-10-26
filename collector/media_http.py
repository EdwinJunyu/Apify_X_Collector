"""Allowlisted HTTPS media transport. Resolve once and pin the connection IP."""
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit, urljoin


class MediaError(RuntimeError):
    pass


def validate_url(url, resolver=socket.getaddrinfo):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise MediaError('UNSAFE_URL')
    try:
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname not in ('pbs.twimg.com', 'video.twimg.com') or
                parts.port not in (None, 443) or parts.username or parts.password or parts.fragment):
            raise MediaError('UNSAFE_URL')
        addresses = resolver(parts.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses:
            raise MediaError('UNSAFE_DNS')
        for address in addresses:
            if not ipaddress.ip_address(address[4][0]).is_global:
                raise MediaError('UNSAFE_DNS')
        return parts, addresses[0][4][0]
    except (ValueError, OSError):
        raise MediaError('UNSAFE_URL_OR_DNS') from None


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, timeout=30, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        raw = socket.create_connection((self.ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class MediaHTTP:
    def stream(self, url):
        # Generator closes connection even if caller rejects size or MIME.
        for _ in range(4):
            parts, ip = validate_url(url)
            connection = PinnedConnection(parts.hostname, ip)
            try:
                target = parts.path or '/'
                if parts.query:
                    target += '?' + parts.query
                connection.request('GET', target, headers={'Accept': 'image/*,video/mp4', 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise MediaError('UNSAFE_REDIRECT')
                    url = urljoin(url, location)
                    continue
                if response.status in (403, 410):
                    raise MediaError('EXPIRED_URL')
                if response.status == 404:
                    raise MediaError('NOT_FOUND')
                if response.status != 200:
                    raise MediaError('NETWORK_ERROR')
                if response.getheader('Content-Encoding', 'identity') != 'identity':
                    raise MediaError('UNSUPPORTED_FORMAT')
                length = response.getheader('Content-Length')
                yield {'mime': response.getheader('Content-Type', '').split(';')[0].lower(),
                       'length': int(length) if length is not None else None}
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    yield chunk
                return
            except (OSError, ValueError, http.client.HTTPException):
                raise MediaError('NETWORK_ERROR') from None
            finally:
                connection.close()
        raise MediaError('UNSAFE_REDIRECT')


class FakeMediaHTTP:
    def __init__(self, data, mime='image/png', error=None):
        self.data, self.mime, self.error = data, mime, error
        self.calls = 0

    def stream(self, url):
        self.calls += 1
        if self.error:
            raise MediaError(self.error)
        yield {'mime': self.mime, 'length': len(self.data)}
        for offset in range(0, len(self.data), 16):
            yield self.data[offset:offset + 16]
