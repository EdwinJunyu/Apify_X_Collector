"""Bounded Apify REST transport; POST is deliberately never retried."""
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from .validation import strict_loads


class RemoteError(RuntimeError):
    pass


def remote_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_~.-]{1,120}', value):
        raise RemoteError('INVALID_REMOTE_ID')
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


class ApifyTransport:
    is_mock = False

    def __init__(self, token, enabled=False):
        if not enabled or not isinstance(token, str) or not token.strip() or '\n' in token or '\r' in token:
            raise PermissionError('LIVE_TRANSPORT_DISABLED_OR_NO_TOKEN')
        self._token = token
        self._opener = build_opener(NoRedirect)

    def _request(self, method, path, query=None, payload=None):
        url = 'https://api.apify.com/v2/' + path
        if query:
            url += '?' + urlencode(query)
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json'})
        try:
            with self._opener.open(request, timeout=45) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise RemoteError('RESPONSE_LIMIT')
                return strict_loads(raw)
        except (HTTPError, URLError, TimeoutError, ValueError):
            # Do not print response bodies, headers or credentials.
            raise RemoteError('REMOTE_REQUEST_FAILED') from None

    def create(self, actor, build, inputs, limit):
        result = self._request('POST', 'acts/' + remote_id(actor.replace('/', '~')) + '/runs',
                               {'build': build, 'maxTotalChargeUsd': limit, 'timeout': 180}, inputs)
        return result['data']

    def run(self, run_id):
        return self._request('GET', 'actor-runs/' + remote_id(run_id))['data']

    def dataset(self, dataset_id):
        return self._request('GET', 'datasets/' + remote_id(dataset_id))['data']

    def items(self, dataset_id, offset, limit):
        return self._request('GET', 'datasets/' + remote_id(dataset_id) + '/items',
                             {'offset': offset, 'limit': limit, 'clean': 'false', 'format': 'json'})


class FakeTransport:
    is_mock = True

    def __init__(self, rows, cost='0.10', unknown_create=False, failed=False):
        self.rows = rows
        self.cost = cost
        self.unknown_create = unknown_create
        self.failed = failed
        self.creates = 0
        self.fail_offset = None
        self.offsets = []

    def create(self, actor, build, inputs, limit):
        self.creates += 1
        if self.unknown_create:
            raise RemoteError('SIMULATED_POST_TIMEOUT')
        return {'id': 'fake-run', 'defaultDatasetId': 'fake-dataset'}

    def run(self, run_id):
        return {'id': run_id, 'status': 'FAILED' if self.failed else 'SUCCEEDED',
                'defaultDatasetId': 'fake-dataset', 'usageTotalUsd': self.cost}

    def dataset(self, dataset_id):
        return {'itemCount': len(self.rows)}

    def items(self, dataset_id, offset, limit):
        self.offsets.append(offset)
        if offset == self.fail_offset:
            raise RemoteError('SIMULATED_PAGE_TIMEOUT')
        return self.rows[offset:offset + limit]
