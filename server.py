#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import gmail
import inbox
import telegram
from store import PLATFORMS, STAGES, Store, now
from trainer import Trainer

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('CAREER_DATA_DIR', Path.home() / '.local/share/career-template'))
SOURCE = Path(os.environ.get('CAREER_SOURCE_DIR', Path.home() / 'Documents/Career-template'))
TOKEN = secrets.token_urlsafe(32)
HH_LOCK = threading.Lock()
GMAIL_LOCK = threading.Lock()
TELEGRAM_CHATS_LOCK = threading.Lock()
GMAIL_LABEL = os.environ.get('CAREER_GMAIL_LABEL', 'Career')
IMPORT_LOCK = threading.Lock()


def import_data(force=False):
    with IMPORT_LOCK:
        return store.import_sources(force)


def sync_hh():
    if not HH_LOCK.acquire(blocking=False):
        return False
    def run():
        previous = {}
        try:
            previous = store.setting('hh_sync', {})
            store.set_setting('hh_sync', {**previous, 'state': 'running', 'startedAt': now()})
            result = subprocess.run(['node', str(ROOT / 'scripts/sync-hh.cjs')], capture_output=True, text=True, timeout=2 * 60 * 60, cwd=ROOT)
            if result.returncode:
                raise RuntimeError(result.stderr.strip()[-1200:] or 'Не удалось подключиться к браузеру')
            data = json.loads(result.stdout)
            count = store.apply_observations(data['records'])
            store.set_setting('hh_sync', {'state': 'success', 'at': now(), 'count': count, 'pages': data['pages'], 'chats': data['chats'], 'warnings': data['errors']})
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            store.set_setting('hh_sync', {**previous, 'state': 'error', 'error': str(exc), 'failedAt': now()})
        finally:
            HH_LOCK.release()
    threading.Thread(target=run, daemon=True).start()
    return True


def sync_gmail():
    if not GMAIL_LOCK.acquire(blocking=False):
        return False

    def run():
        try:
            store.set_setting('gmail_sync', {'state': 'running'})
            count = gmail.sync(store, GMAIL_LABEL)
            store.set_setting('gmail_sync', {'state': 'success', 'at': now(), 'count': count})
        except FileNotFoundError:
            store.set_setting('gmail_sync', {'state': 'error', 'error': 'Запустите manage.py gmail-auth'})
        except (RuntimeError, KeyError, ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, RuntimeError) else 'Проверьте OAuth и настройки Gmail'
            store.set_setting('gmail_sync', {'state': 'error', 'error': message})
        finally:
            GMAIL_LOCK.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def sync_telegram_chats(folder):
    if not TELEGRAM_CHATS_LOCK.acquire(blocking=False):
        return

    def run():
        try:
            count = telegram.sync_chats(store, folder)
            store.set_setting('telegram_chats_sync', {'state': 'success', 'count': count, 'at': now()})
        except (RuntimeError, KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
            store.set_setting('telegram_chats_sync', {
                'state': 'error', 'error': 'Проверьте вход в Telegram Web и рабочую папку',
            })
        finally:
            TELEGRAM_CHATS_LOCK.release()

    threading.Thread(target=run, daemon=True).start()


def documents():
    items = []
    folder = SOURCE / 'resumes'
    if folder.is_symlink():
        return items
    for path in sorted(folder.iterdir()) if folder.is_dir() else ():
        if path.suffix.lower() != '.pdf' or path.is_symlink() or not path.is_file():
            continue
        items.append({'id': str(path.relative_to(SOURCE)), 'name': path.name, 'size': path.stat().st_size})
    return items


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send(self, value, status=200, mime='application/json; charset=utf-8', extra=None):
        raw = json.dumps(value, ensure_ascii=False).encode() if mime.startswith('application/json') else value.encode() if isinstance(value, str) else value
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def valid_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def do_GET(self):
        if not self.valid_host():
            return self.send({'error': 'Недопустимый адрес сервера'}, 403)
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == '/api/bootstrap':
                return self.send({'token': TOKEN, 'stages': STAGES, 'platforms': PLATFORMS, 'inboxKinds': inbox.KINDS})
            if path == '/api/stats':
                return self.send(store.stats())
            if path == '/api/trainer/state':
                return self.send(trainer.state(params.get('category', ''), params.get('mode', 'learn')))
            if path == '/api/inbox':
                with store.connect() as db:
                    result = inbox.listing(db, int(params.get('page', 1)), params.get('pending') == '1')
                result['gmail'] = store.setting('gmail_sync', {'state': 'idle'})
                result['telegram'] = store.setting('telegram_sync', {})
                result['telegramChats'] = store.setting('telegram_chats_sync', {})
                return self.send(result)
            if path.startswith('/api/inbox/'):
                with store.connect() as db:
                    item = db.execute('SELECT * FROM inbox WHERE id=?', (path[len('/api/inbox/'):],)).fetchone()
                return self.send(dict(item) if item else {'error': 'Сообщение не найдено'}, 200 if item else 404)
            if path == '/api/jobs':
                return self.send(store.jobs(params))
            if path.startswith('/api/jobs/'):
                item = store.detail(path[len('/api/jobs/'):])
                return self.send(item if item else {'error': 'Отклик не найден'}, 200 if item else 404)
            if path == '/api/export.csv':
                return self.send(store.export_csv(params), mime='text/csv; charset=utf-8', extra={'Content-Disposition': 'attachment; filename="career-applications.csv"'})
            if path == '/api/documents':
                return self.send(documents())
            if path.startswith('/api/documents/'):
                ident = path[len('/api/documents/'):]
                allowed = {x['id'] for x in documents()}
                if ident not in allowed:
                    return self.send({'error': 'Документ не найден'}, 404)
                return self.send((SOURCE / ident).read_bytes(), mime='application/pdf')
            files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8'), '/icon.svg': ('icon.svg', 'image/svg+xml')}
            files.update({'/trainer': ('trainer.html', 'text/html; charset=utf-8'),
                          '/trainer.js': ('trainer.js', 'text/javascript; charset=utf-8'),
                          '/trainer.css': ('trainer.css', 'text/css; charset=utf-8')})
            if path in files:
                name, mime = files[path]
                return self.send((ROOT / 'public' / name).read_bytes(), mime=mime)
            self.send({'error': 'Не найдено'}, 404)
        except ValueError:
            self.send({'error': 'Некорректные параметры запроса'}, 400)

    def do_POST(self):
        if not self.valid_host() or self.headers.get('X-Career-Token') != TOKEN:
            return self.send({'error': 'Обновите страницу и повторите действие'}, 403)
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}:
            return self.send({'error': 'Недопустимый источник запроса'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 100000:
                return self.send({'error': 'Слишком большой запрос'}, 413)
            body = json.loads(self.rfile.read(length) or '{}')
            if not isinstance(body, dict):
                raise ValueError('Некорректные данные')
            path = unquote(urlparse(self.path).path)
            if path in ('/api/trainer/answer', '/api/trainer/grade'):
                return self.send(trainer.save(body, grade=path.endswith('/grade')))
            if path == '/api/sync':
                return self.send(import_data())
            if path == '/api/sync/hh':
                started = sync_hh()
                return self.send({'started': started}, 202 if started else 200)
            if path == '/api/sync/gmail':
                started = sync_gmail()
                return self.send({'started': started}, 202 if started else 200)
            if path.startswith('/api/inbox/'):
                with store.connect() as db:
                    result = inbox.update(db, path[len('/api/inbox/'):], body)
                return self.send(result)
            if path == '/api/settings':
                if not isinstance(body.get('autoHH'), bool):
                    raise ValueError('Неверное значение автоматизации')
                store.set_setting('auto_hh', body['autoHH'])
                return self.send({'autoHH': body['autoHH']})
            if path.startswith('/api/jobs/'):
                return self.send(store.update(path[len('/api/jobs/'):], body))
            self.send({'error': 'Не найдено'}, 404)
        except (ValueError, TypeError) as exc:
            self.send({'error': str(exc)}, 400)
        except KeyError:
            self.send({'error': 'Запись не найдена'}, 404)


def sync_and_report():
    try:
        hh_enabled = store.setting('auto_hh', False)
        gmail_enabled = gmail.CREDENTIALS.exists()
        if hh_enabled:
            sync_hh()
        if gmail_enabled:
            sync_gmail()
        config = telegram.configuration()
        folder = config.get('watch_folder')
        if folder:
            sync_telegram_chats(folder)
        for enabled, lock in ((hh_enabled, HH_LOCK), (gmail_enabled, GMAIL_LOCK),
                              (bool(folder), TELEGRAM_CHATS_LOCK)):
            if enabled:
                # Дожидаемся также синхронизации, ранее запущенной кнопкой.
                with lock:
                    pass
        if config:
            import_data()
            telegram.report(store, config, (
                ('HH', 'hh_sync', hh_enabled, 'проверено откликов'),
                ('Gmail', 'gmail_sync', gmail_enabled, 'новых сообщений'),
                ('Telegram', 'telegram_chats_sync', bool(folder), 'изменений в рабочих чатах'),
            ))
            store.set_setting('telegram_report', {'state': 'success', 'at': now()})
    except (RuntimeError, ValueError, OSError):
        store.set_setting('telegram_report', {'state': 'error', 'at': now(),
                                             'error': 'Проверьте настройки и доступность Telegram'})


def automation():
    next_sync = time.monotonic()
    cycle = None
    while True:
        try:
            import_data()
            if time.monotonic() >= next_sync and (cycle is None or not cycle.is_alive()):
                next_sync = time.monotonic() + 2 * 60 * 60
                cycle = threading.Thread(target=sync_and_report, daemon=True)
                cycle.start()
            try:
                notified = telegram.notify(store)
            except (RuntimeError, ValueError, OSError) as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else 'Проверьте настройки Telegram'
                store.set_setting('telegram_sync', {'state': 'error', 'error': message})
            else:
                if notified or store.setting('telegram_sync', {}).get('state') == 'error':
                    store.set_setting('telegram_sync', {'state': 'success', 'at': now()})
        except Exception as exc:
            store.set_setting('automation_error', {'at': now(), 'error': str(exc)})
        time.sleep(30)


def main():
    global store, trainer
    parser = argparse.ArgumentParser(description='Локальный дашборд поиска работы')
    parser.add_argument('--port', type=int, default=4317)
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.fdopen(os.open(DATA / 'server.lock', os.O_CREAT | os.O_RDWR, 0o600), 'r+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, 'Дашборд для этой базы уже работает.\n')
    store = Store(DATA, SOURCE)
    trainer = Trainer(DATA, SOURCE)
    import_data()
    # После перезапуска убираем зависшее состояние прошлой синхронизации.
    previous = store.setting('hh_sync', {})
    if previous.get('state') == 'running':
        store.set_setting('hh_sync', {**previous, 'state': 'error', 'error': 'Сервер был перезапущен; повторите синхронизацию'})
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    threading.Thread(target=automation, daemon=True).start()
    print(f'Career dashboard: http://127.0.0.1:{args.port}', flush=True)
    print(f'Данные: {DATA}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == '__main__':
    main()
