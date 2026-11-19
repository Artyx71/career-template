import base64
import json
import tempfile
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import gmail
import inbox
import server
import telegram
from store import Store


def raw_item(text, *, subject='Frontend', timestamp='1795017600000', **headers):
    message = EmailMessage()
    message['From'] = headers.pop('From', 'Recruiter <recruiter@example.test>')
    message['Subject'] = subject
    for key, value in headers.items():
        message[key] = value
    message.set_content(text)
    return {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip('='), 'internalDate': timestamp}


class GmailFixture:
    account = 'candidate@example.test'
    account_key = 'test-account'
    label = 'Label_test'
    profile = {'historyId': '11'}

    def __init__(self):
        self.incremental = False
        self.fail = False
        self.expired = False
        self.sent = False
        self.body_reads = []

    def pages(self, path, **params):
        if path == 'threads':
            assert params == {'labelIds': self.label}
            yield {'threads': [{'id': 'thread'}]}
        elif path == 'history':
            if self.expired:
                raise gmail.RemoteError('Gmail', 404)
            ids = [('fresh', 'thread'), ('private', 'private-thread')]
            yield {'historyId': '12', 'history': [{'messagesAdded': [
                {'message': {'id': mid, 'threadId': tid}} for mid, tid in ids
            ]}]}
            yield {'historyId': '13', 'history': []}

    def get(self, path, **params):
        if path == 'threads/thread':
            messages = [{'id': 'initial', 'labelIds': [self.label]}]
            if self.incremental:
                messages.append({'id': 'fresh', 'labelIds': ['INBOX']})
            if self.sent:
                messages.append({'id': 'sent', 'labelIds': ['SENT'], 'internalDate': '1795017601000'})
            return {'messages': messages}
        self.body_reads.append(path)
        if path == 'messages/initial':
            return raw_item('Мы получили ваш отклик')
        if path == 'messages/sent':
            return raw_item('Спасибо!', timestamp='1795017601000', From='candidate@example.test')
        assert path == 'messages/fresh'
        if self.fail:
            raise gmail.RemoteError('Gmail', 503)
        return raw_item('Приглашаем на собеседование завтра https://hh.ru/vacancy/42', timestamp='1795017600500',
                        **{'Reply-To': 'hr@example.test'})


class InboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name)
        self.store = Store(path / 'data', path / 'source')
        self.api = GmailFixture()
        self.credentials = path / 'telegram-credentials.json'
        self.credentials.write_text(json.dumps({'token': '123:fixture', 'chat_id': '42', 'proxy': 'http://127.0.0.1:2080'}))
        self.credentials.chmod(0o600)

    def list(self, pending=False):
        with self.store.connect() as db:
            return inbox.listing(db, pending=pending)['items']

    def test_классификация_почты_и_исключение_кодов(self):
        cases = [
            ('Хотим уточнить: удобно завтра?', 'question'),
            ('Высылаем вам оффер', 'offer'),
            ('Thank you for your application\n> Please confirm your interview', 'automatic'),
            ('Ваше обращение зарегистрировано и передано в отдел подбора персонала. Ожидайте.', 'automatic'),
            ('Ваше обращение зарегистрировано и передано в отдел подбора. Пришлите ссылку на портфолио.', 'question'),
            ('Выбрали другого кандидата', 'rejection'),
            ('Thank you for your application. We are not currently hiring for this position.', 'rejection'),
            ('К сожалению, мы не рассматриваем варианты удаленного сотрудничества.', 'rejection'),
            ('Please complete the coding challenge', 'test'),
            ('Your verification code is 123456', None),
        ]
        for body, kind in cases:
            with self.subTest(kind=kind):
                result = gmail.decode(raw_item(body), self.api.account)
                self.assertEqual(result['kind'] if result else None, kind)

    def test_oauth_pkce_и_проверка_аккаунта_перед_заменой(self):
        folder = self.credentials.parent
        client = folder / 'google-client.json'
        saved = folder / 'gmail-credentials.json'
        client.write_text(json.dumps({'installed': {
            'client_id': 'fixture.apps.googleusercontent.com', 'client_secret': 'fixture'
        }}))
        saved.write_text('предыдущая авторизация')
        from threading import Thread

        def consent(output, **kwargs):
            query = parse_qs(urlsplit(output.splitlines()[-1]).query)
            self.assertEqual(query['scope'], [gmail.SCOPE])
            self.assertEqual(query['code_challenge_method'], ['S256'])
            def callback():
                with urlopen(query['redirect_uri'][0] + '?' + urlencode({
                    'code': 'fixture-code', 'state': query['state'][0],
                }), timeout=5) as response:
                    response.read()
            Thread(target=callback, daemon=True).start()

        token = {'scope': gmail.SCOPE, 'access_token': 'access', 'refresh_token': 'refresh'}
        with patch('gmail.SECRET_DIR', folder), patch('gmail.CREDENTIALS', saved), patch('builtins.print', side_effect=consent):
            with patch('gmail.request', side_effect=[token, {'emailAddress': 'wrong@example.test'}]):
                with self.assertRaises(RuntimeError):
                    gmail.authorize(client, self.api.account)
                self.assertEqual(saved.read_text(), 'предыдущая авторизация')
            with patch('gmail.request', side_effect=[token, {'emailAddress': self.api.account}]) as request:
                gmail.authorize(client, self.api.account)
            self.assertEqual(request.call_args_list[0].kwargs['data']['code'], 'fixture-code')
            self.assertGreater(len(request.call_args_list[0].kwargs['data']['code_verifier']), 43)
            self.assertEqual(gmail.credentials()['account'], self.api.account)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    @patch('gmail.Gmail')
    def test_новое_письмо_в_помеченном_диалоге_курсор_и_повтор(self, mock):
        mock.return_value = self.api
        key = 'gmail:test-account:Label_test'
        self.store.apply_observations([{'id': '42', 'platform_status': 'Просмотрен', 'at': '2020-01-01T12:00:00Z'}])
        self.store.update('hh:42', {'manual_stage': 'interview'})
        self.assertEqual(gmail.sync(self.store, 'Career'), 1)
        self.assertEqual(self.store.setting(key), '11')
        self.assertEqual(self.list(pending=True), [])
        self.api.incremental = self.api.fail = True
        with self.assertRaises(gmail.RemoteError):
            gmail.sync(self.store, 'Career')
        self.assertEqual(self.store.setting(key), '11')
        self.api.fail = False
        self.assertEqual(gmail.sync(self.store, 'Career'), 1)
        self.assertEqual(self.store.setting(key), '13')
        latest, = self.list(pending=True)
        self.assertEqual((latest['kind'], latest['urgent'], latest['job_key']), ('invitation', 1, 'hh:42'))
        self.assertEqual(self.store.detail('hh:42')['effective_stage'], 'interview')
        with self.store.connect() as db:
            inbox.update(db, latest['id'], {'draft': 'Здравствуйте! Спасибо за приглашение.'})
        self.assertTrue(gmail.context(self.store, 'Career', latest['id'])['reply_to_latest'])
        self.assertEqual(gmail.context(self.store, 'Career', latest['id'])['reply_to'], 'hr@example.test')
        self.assertEqual(gmail.sync(self.store, 'Career'), 0)
        self.assertTrue(self.list()[0]['draft'])
        self.assertNotIn('messages/private', self.api.body_reads)
        self.api.sent = True
        self.assertEqual(gmail.sync(self.store, 'Career'), 0)
        self.assertEqual(self.list(pending=True), [])
        self.assertEqual(self.list()[0]['draft'], 'Здравствуйте! Спасибо за приглашение.')

    @patch('gmail.Gmail')
    def test_просроченный_курсор_восстанавливается_без_дублей(self, mock):
        mock.return_value = self.api
        gmail.sync(self.store, 'Career')
        self.api.incremental = self.api.expired = True
        self.api.profile = {'historyId': '17'}
        self.assertEqual(gmail.sync(self.store, 'Career'), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 2)
        self.assertEqual(self.store.setting('gmail:test-account:Label_test'), '17')

    def test_hh_новый_фрагмент_виден_один_раз(self):
        row = {'id': '1', 'company': 'Пример', 'chat_url': 'https://hh.ru/chat/12', 'messages': ['Первое', 'Новое']}
        self.store.apply_observations([row, row])
        self.store.apply_observations([row | {'messages': []}, row])
        latest, = self.list()
        self.assertEqual(latest['kind'], 'review')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 1)

    def test_hh_исходящее_не_создаёт_входящее_и_закрывает_старый_фрагмент(self):
        row = {'id': '1', 'company': 'Пример', 'messages': ['Мой отклик']}
        outgoing = row | {'last_message_outgoing': True}
        self.store.apply_observations([outgoing])
        self.assertEqual(self.list(), [])
        self.store.apply_observations([row | {'messages': ['Другой текст']}, row])
        self.store.apply_observations([outgoing])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT done, notify FROM inbox WHERE text='Мой отклик'").fetchone()[:], (1, 0))
        self.store.apply_observations([row | {'messages': ['Мой отклик', 'Ответ работодателя']}])
        self.assertEqual(self.list()[0]['kind'], 'review')

    def test_hh_сохраняет_ручной_черновик_при_смене_признака_автора(self):
        row = {'id': '1', 'company': 'Пример', 'messages': ['Текст из чата']}
        self.store.apply_observations([row])
        message, = self.list()
        with self.store.connect() as db:
            inbox.update(db, message['id'], {'draft': 'Черновик'})
        self.store.apply_observations([row | {'last_message_outgoing': True}])
        self.assertEqual(self.list()[0]['draft'], 'Черновик')

    @patch('telegram.read_unread')
    def test_телеграм_повторный_снимок_не_создаёт_уведомление(self, unread):
        folder = {'id': 11, 'name': "HR's"}
        unread.return_value = [{'peer': '51', 'count': 1, 'stamp': '10:00'}]
        self.assertEqual(telegram.sync_chats(self.store, folder), 1)
        self.assertEqual(telegram.sync_chats(self.store, folder), 0)
        unread.return_value[0]['count'] = 2
        self.assertEqual(telegram.sync_chats(self.store, folder), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM inbox WHERE source='telegram' AND notify=1").fetchone()[0], 1)
        latest, = self.list()
        unread.return_value = []
        self.assertEqual(telegram.sync_chats(self.store, folder), 0)
        self.assertEqual(latest['source'], 'telegram')
        self.assertEqual(latest['next_action'], 'Проверить рабочий диалог')

    @patch('telegram.request')
    def test_телеграм_напоминает_без_текста_диалога_один_раз(self, request):
        self.store.apply_observations([{'id': '5', 'platform_status': 'Приглашение', 'company': 'Личное имя'}])
        latest, = self.list()
        with self.store.connect() as db:
            inbox.update(db, latest['id'], {'due': '2020-01-01T10:00'})
        request.return_value = {'ok': True}
        with patch('telegram.CONFIG', self.credentials):
            self.assertEqual(telegram.notify(self.store), 1)
            self.assertEqual(telegram.notify(self.store), 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs['proxy'], 'http://127.0.0.1:2080')
        self.assertNotIn('Личное имя', request.call_args.kwargs['data']['text'])

    @patch('telegram.request', return_value={'ok': True})
    def test_сводка_дожидается_синхронизации_и_сообщает_об_ошибке_источника(self, request):
        self.store.set_setting('auto_hh', True)
        self.store.apply_observations([{'id': '5', 'platform_status': 'Приглашение', 'company': 'Личное имя'}])
        lock = threading.Lock()
        lock.acquire()
        started = threading.Event()
        release = threading.Event()

        def finish_hh():
            if release.wait(3):
                self.store.set_setting('hh_sync', {'state': 'success', 'count': 1, 'warnings': ['Приватный чат']})
            lock.release()

        hh = threading.Thread(target=finish_hh)
        hh.start()
        with (patch.object(server, 'store', self.store, create=True), patch.object(server, 'HH_LOCK', lock),
              patch.object(server, 'sync_hh', side_effect=started.set),
              patch.object(server, 'sync_gmail', side_effect=lambda: self.store.set_setting('gmail_sync', {'state': 'error'})),
              patch.object(gmail, 'CREDENTIALS', self.credentials), patch.object(telegram, 'CONFIG', self.credentials)):
            cycle = threading.Thread(target=server.sync_and_report)
            cycle.start()
            try:
                self.assertTrue(started.wait(2))
                cycle.join(0.05)
                request.assert_not_called()
            finally:
                release.set()
                hh.join(3)
                cycle.join(3)
        self.assertEqual(self.store.setting('telegram_report')['state'], 'success')
        request.assert_called_once()
        message = request.call_args.kwargs['data']['text']
        self.assertIn('HH — проверено откликов: 1 · не прочитано чатов: 1', message)
        self.assertIn('Gmail — ошибка проверки', message)
        self.assertIn('Telegram — проверка выключена', message)
        self.assertIn('Приглашения: 1', message)
        self.assertNotIn('Личное имя', message)
        self.assertNotIn('Приватный чат', message)
