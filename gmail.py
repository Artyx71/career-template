import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import inbox
from store import canonical_url

SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
ORIGIN = 'https://gmail.googleapis.com/gmail/v1/users/me/'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SECRET_DIR = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'career-template'
CREDENTIALS = SECRET_DIR / 'gmail-credentials.json'


class RemoteError(RuntimeError):
    def __init__(self, service, status):
        self.status = status
        super().__init__(f'{service}: HTTP {status}')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(service, url, *, data=None, token=None, proxy=None):
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    handlers = [NoRedirect()]
    if proxy:
        handlers.append(ProxyHandler({'https': proxy}))
    try:
        with build_opener(*handlers).open(
            Request(url, data=urlencode(data).encode() if data is not None else None, headers=headers), timeout=20
        ) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RemoteError(service, exc.code) from None
    except (OSError, URLError, ValueError):
        raise RuntimeError(f'{service}: нет корректного ответа от сервиса') from None


def credentials():
    with CREDENTIALS.open() as file:
        if os.fstat(file.fileno()).st_mode & 0o077:
            raise RuntimeError('Gmail: установите chmod 600 для файла авторизации')
        return json.load(file)


def authorize(client_file, account):
    client = json.loads(client_file.read_text()).get('installed', {})
    if not client.get('client_id', '').endswith('.apps.googleusercontent.com') or not client.get('client_secret'):
        raise ValueError('Нужен JSON OAuth-клиента типа Desktop app')
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    reply = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            valid = (self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'
                     and parsed.path == '/oauth' and secrets.compare_digest(params.get('state', [''])[0], state))
            if valid:
                reply.update(params)
            self.send_response(200 if valid else 400)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(('Вернитесь в терминал.' if valid else 'Недействительный запрос.').encode())

    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = 1
        redirect = f'http://127.0.0.1:{server.server_port}/oauth'
        url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(dict(
            client_id=client['client_id'], redirect_uri=redirect, response_type='code', scope=SCOPE,
            state=state, code_challenge=challenge, code_challenge_method='S256', access_type='offline',
            prompt='consent', login_hint=account,
        ))
        print('Откройте ссылку в браузере на этом компьютере:\n' + url, flush=True)
        deadline = time.monotonic() + 300
        while not reply and time.monotonic() < deadline:
            server.handle_request()
    if not reply.get('code'):
        raise RuntimeError('Gmail: согласие не получено или время ожидания истекло')
    client = {key: client[key] for key in ('client_id', 'client_secret')}
    token = request('Google OAuth', TOKEN_URL, data=client | dict(
        code=reply['code'][0], code_verifier=verifier, redirect_uri=redirect, grant_type='authorization_code',
    ))
    if SCOPE not in token.get('scope', '').split() or not token.get('refresh_token'):
        raise RuntimeError('Gmail: доступ на чтение или refresh token отсутствует')
    profile = request('Gmail', ORIGIN + 'profile', token=token['access_token'])
    if profile['emailAddress'].casefold() != account.casefold():
        raise RuntimeError('Gmail: выбран другой аккаунт, прежняя авторизация сохранена')
    SECRET_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=SECRET_DIR, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(client | {'refresh_token': token['refresh_token'], 'account': profile['emailAddress']}, file)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(CREDENTIALS)
        finally:
            temporary.unlink(missing_ok=True)


class Gmail:
    def __init__(self, label):
        creds = credentials()
        try:
            response = request('Google OAuth', TOKEN_URL, data={
                **{key: creds[key] for key in ('client_id', 'client_secret', 'refresh_token')},
                'grant_type': 'refresh_token',
            })
        except RemoteError as exc:
            if exc.status in (400, 401):
                raise RuntimeError('Gmail: повторите manage.py gmail-auth') from None
            raise
        self.token = response['access_token']
        self.profile = self.get('profile')
        self.account = self.profile['emailAddress']
        if self.account.casefold() != creds['account'].casefold():
            raise RuntimeError('Gmail: авторизован другой аккаунт')
        self.account_key = hashlib.sha256(self.account.casefold().encode()).hexdigest()[:20]
        labels = self.get('labels').get('labels', [])
        self.label = next((item['id'] for item in labels if item['name'] == label and item['type'] == 'user'), None)
        if not self.label:
            raise RuntimeError(f'Gmail: создайте пользовательский ярлык «{label}» и отметьте карьерные диалоги')

    def get(self, path, **params):
        return request('Gmail', ORIGIN + path + ('?' + urlencode(params, doseq=True) if params else ''), token=self.token)

    def pages(self, path, **params):
        while True:
            page = self.get(path, **params)
            yield page
            if not page.get('nextPageToken'):
                return
            params['pageToken'] = page['nextPageToken']


class PlainHTML(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.parts = []
        self.skip = None
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script'):
            self.skip = tag
        elif tag in ('p', 'br', 'div', 'li', 'tr', 'blockquote'):
            self.parts.append('\n')
        elif tag == 'a':
            href = dict(attrs).get('href', '')
            if href.startswith(('https://', 'http://')):
                self.parts.append(' ' + href + ' ')

    def handle_endtag(self, tag):
        if tag == self.skip:
            self.skip = None

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def decode(item, account):
    raw = item['raw']
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    subject = str(message.get('Subject', ''))
    sender = str(message.get('From', ''))
    addresses = getaddresses([str(message.get('Reply-To') or message.get('From') or '')])
    recipient = addresses[0][1] if len(addresses) == 1 and '@' in addresses[0][1] else ''
    part = message.get_body(preferencelist=('plain', 'html'))
    body = part.get_content() if part else ''
    if part and part.get_content_type() == 'text/html':
        body = ''.join(PlainHTML(body).parts)
    kind, urgent = inbox.classify(subject, body, {k.lower(): str(v) for k, v in message.items()}, recipient)
    if kind is None:
        return None
    at = datetime.fromtimestamp(int(item['internalDate']) / 1000, timezone.utc).isoformat(timespec='milliseconds')
    return dict(at=at, subject=subject[:1000], text=body[:50000], kind=kind, urgent=urgent,
                sender=sender[:1000], recipient=recipient if recipient.casefold() != account.casefold() else '')


def sync(store, label):
    api = Gmail(label)
    key = 'gmail:' + api.account_key + ':' + api.label
    cursor = store.setting(key)
    next_cursor = api.profile['historyId']
    changed = None
    if cursor:
        try:
            changed = set()
            # Новые письма в помеченном диалоге могут не унаследовать ярлык.
            for page in api.pages('history', startHistoryId=cursor,
                                  historyTypes=['messageAdded', 'labelAdded']):
                for event in page.get('history', []):
                    for change in event.get('messagesAdded', []) + event.get('labelsAdded', []):
                        thread = change['message'].get('threadId')
                        if thread:
                            changed.add(thread)
                next_cursor = page['historyId']
        except RemoteError as exc:
            if exc.status != 404:
                raise
            changed = None
            next_cursor = api.profile['historyId']
    scoped = {item['id'] for page in api.pages('threads', labelIds=api.label)
              for item in page.get('threads', [])}
    changed = scoped if changed is None else scoped & changed

    messages, sent_after = [], {}
    with store.connect() as db:
        existing = {row[0] for row in db.execute(
            "SELECT id FROM inbox WHERE source='gmail' AND thread LIKE ?", (api.account_key + ':%',))}
    for thread in sorted(changed):
        try:
            overview = api.get('threads/' + quote(thread, safe=''), format='metadata',
                               fields='messages(id,threadId,labelIds,internalDate)')
        except RemoteError as exc:
            if exc.status == 404:
                continue
            raise
        items = overview.get('messages', [])
        if not any(api.label in item.get('labelIds', []) and not set(item['labelIds']) & {'SPAM', 'TRASH'} for item in items):
            continue
        thread_key = f'{api.account_key}:{thread}'
        sent_after[thread_key] = 0
        for item in items:
            if 'SENT' not in item.get('labelIds', []):
                continue
            timestamp = item.get('internalDate')
            if timestamp is None:
                try:
                    timestamp = api.get('messages/' + quote(item['id'], safe=''), format='raw',
                                        fields='internalDate').get('internalDate')
                except RemoteError as exc:
                    if exc.status != 404:
                        raise
            if timestamp is not None:
                sent_after[thread_key] = max(sent_after[thread_key], int(timestamp))
        for item in items:
            ident = item['id']
            ident_key = f'gmail:{api.account_key}:{ident}'
            if ident_key in existing or set(item.get('labelIds', [])) & {'SENT', 'DRAFT', 'SPAM', 'TRASH'}:
                continue
            try:
                full = api.get('messages/' + quote(ident, safe=''), format='raw')
            except RemoteError as exc:
                if exc.status == 404:
                    continue
                raise
            parsed = decode(full, api.account)
            if parsed is None:
                continue
            messages.append(parsed | dict(
                id=ident_key, source='gmail', thread=thread_key,
                url=f'https://mail.google.com/mail/u/{quote(api.account, safe="@")}/#all/{quote(thread, safe="")}',
                next_action=inbox.ACTIONS.get(parsed['kind'], ''),
                notify=bool(cursor and parsed['kind'] in inbox.ACTIONS),
            ))

    stages = {'invitation': 'invited', 'offer': 'offer', 'rejection': 'rejected', 'test': 'reply', 'question': 'reply'}
    count = 0
    with store.connect() as db:
        for message in sorted(messages, key=lambda item: item['at']):
            # Автопривязка только по одной точной ссылке на уже учтённую вакансию.
            candidate = set()
            for platform in ('hh', 'habr', 'getmatch', 'geekjob'):
                prefix = canonical_url(platform, '')
                candidate.update(platform + ':' + ident for ident in re.findall(re.escape(prefix) + r'([\w-]+)', message['text']))
            matched = [item for item in candidate if db.execute('SELECT 1 FROM jobs WHERE key=?', (item,)).fetchone()]
            message['job_key'] = matched[0] if len(matched) == 1 else None
            created = inbox.insert(db, message)
            count += created
            stage = stages.get(message['kind'])
            if created and stage and message['job_key']:
                db.execute('''UPDATE jobs SET response_stage=?,response_at=?
                              WHERE key=? AND confirmed=1 AND (response_at IS NULL OR julianday(response_at)<julianday(?))''',
                           (stage, message['at'], message['job_key'], message['at']))
        for thread, timestamp in sent_after.items():
            if timestamp:
                sent_at = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat(timespec='milliseconds')
                db.execute("UPDATE inbox SET done=1, notify=0 WHERE source='gmail' AND thread=? AND at<=?", (thread, sent_at))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(next_cursor)))
    return count


def context(store, label, ident):
    with store.connect() as db:
        row = db.execute("SELECT * FROM inbox WHERE id=? AND source='gmail'", (ident,)).fetchone()
    if not row:
        raise KeyError(ident)
    api = Gmail(label)
    _, account_key, message = ident.split(':', 2)
    if account_key != api.account_key:
        raise RuntimeError('Gmail: диалог принадлежит другому аккаунту')
    thread = row['thread'].split(':', 1)[1]
    overview = api.get('threads/' + quote(thread, safe=''), format='minimal').get('messages', [])
    if not any(api.label in item.get('labelIds', []) and not set(item['labelIds']) & {'SPAM', 'TRASH'}
               for item in overview) or not any(
        item['id'] == message and not set(item.get('labelIds', [])) & {'TRASH', 'SPAM'} for item in overview
    ):
        raise RuntimeError('Gmail: карьерный ярлык или исходное письмо больше недоступны')
    messages = []
    for item in overview:
        if set(item.get('labelIds', [])) & {'DRAFT', 'TRASH', 'SPAM'}:
            continue
        try:
            full = api.get('messages/' + quote(item['id'], safe=''), format='raw')
        except RemoteError as exc:
            if exc.status == 404:
                continue
            raise
        parsed = decode(full, api.account)
        if parsed:
            messages.append(parsed | {'outgoing': 'SENT' in item.get('labelIds', [])})
    messages.sort(key=lambda item: item['at'])
    return {'reply_to': row['recipient'], 'url': row['url'], 'messages': messages,
            'reply_to_latest': bool(messages and not messages[-1]['outgoing'] and messages[-1]['at'] == row['at'])}
