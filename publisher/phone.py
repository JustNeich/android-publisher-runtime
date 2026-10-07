import ipaddress
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from .config import UserError


def endpoint(raw):
    parsed = urlsplit(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UserError('Нужен адрес Android MCP без пароля и параметров.')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise UserError('Для телефона в этой сети укажите его локальный IP-адрес.') from None
    if not address.is_private or address.is_unspecified or address.is_multicast or address.is_loopback:
        raise UserError('Коннектор должен находиться в локальной сети; внешний адрес не подходит.')
    if parsed.path.rstrip('/') not in ('', '/mcp'):
        raise UserError('Путь коннектора должен быть /mcp.')
    return urlunsplit((parsed.scheme, parsed.netloc, '/mcp', '', ''))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Phone:
    def __init__(self, address, token, timeout=45):
        self.address = endpoint(address)
        self.token = token
        self.timeout = timeout
        self.session = None
        self.protocol = '2025-03-26'
        self.counter = 0
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, params=None, notification=False):
        self.counter += 1
        payload = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            payload['id'] = self.counter
        if params is not None:
            payload['params'] = params
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if self.session:
            headers.update({'Mcp-Session-Id': self.session, 'MCP-Protocol-Version': self.protocol})
        try:
            with self.opener.open(Request(self.address, json.dumps(payload).encode(), headers, method='POST'), timeout=self.timeout) as response:
                self.session = response.headers.get('Mcp-Session-Id', self.session)
                raw = response.read(30 * 1024 * 1024 + 1)
        except HTTPError as error:
            code = error.code
            error.close()
            raise UserError(f'Телефон вернул HTTP {code}; действие не повторено.') from None
        except (URLError, OSError, TimeoutError):
            raise UserError('Телефон недоступен; результат действия неизвестен.') from None
        if notification and not raw:
            return None
        if len(raw) > 30 * 1024 * 1024:
            raise UserError('Ответ телефона слишком большой.')
        try:
            value = json.loads(raw)
        except ValueError:
            raise UserError('Телефон вернул некорректный JSON.') from None
        if notification and value is None:
            return None
        if not isinstance(value, dict):
            raise UserError('Телефон вернул некорректный MCP-ответ.')
        if 'error' in value or (not notification and value.get('id') != self.counter):
            raise UserError('Телефон отклонил MCP-запрос.')
        return value.get('result')

    def initialize(self):
        from . import __version__
        result = self.request('initialize', {'protocolVersion': self.protocol, 'capabilities': {}, 'clientInfo': {'name': 'android-publisher', 'version': __version__}})
        self.protocol = result.get('protocolVersion', self.protocol)
        self.request('notifications/initialized', notification=True)

    def tools(self):
        result = self.request('tools/list', {})
        tools = result['tools']
        while result.get('nextCursor'):
            result = self.request('tools/list', {'cursor': result['nextCursor']})
            tools.extend(result['tools'])
        return tools

    def call(self, name, arguments):
        return self.request('tools/call', {'name': name, 'arguments': arguments})

    def close(self):
        if not self.session:
            return
        headers = {'Authorization': 'Bearer ' + self.token, 'Mcp-Session-Id': self.session, 'MCP-Protocol-Version': self.protocol}
        self.session = None
        try:
            with self.opener.open(Request(self.address, headers=headers, method='DELETE'), timeout=5) as response:
                response.read(1024)
        except (HTTPError, URLError, OSError, TimeoutError):
            pass

    def __enter__(self):
        try:
            self.initialize()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *args):
        self.close()


def redact(value, *secrets_to_hide):
    raw = json.dumps(value, ensure_ascii=False)
    for secret in secrets_to_hide:
        if secret:
            raw = raw.replace(secret, '[REDACTED]')
    safe = json.loads(raw)
    if any(secret and secret in json.dumps(value, ensure_ascii=False) for secret in secrets_to_hide) and isinstance(safe, dict) and isinstance(safe.get('content'), list):
        safe['content'] = [b for b in safe['content'] if b.get('type') != 'image']
    return safe
