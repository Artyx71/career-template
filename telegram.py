import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from gmail import SECRET_DIR, request
from inbox import LATEST, insert
from store import MSK, now

CONFIG = SECRET_DIR / 'telegram-credentials.json'


def configuration():
    try:
        with CONFIG.open() as file:
            if os.fstat(file.fileno()).st_mode & 0o077:
                raise RuntimeError('Telegram: установите chmod 600 для файла настроек')
            config = json.load(file)
    except FileNotFoundError:
        return {}
    return config


def read_unread(folder):
    script = Path(__file__).resolve().parent / 'scripts' / 'watch-telegram.cjs'
    result = subprocess.run(['node', str(script), str(folder['id']), folder['name']],
                            capture_output=True, text=True, timeout=90, check=True)
    return json.loads(result.stdout)


def sync_chats(store, folder):
    snapshot = {row['peer']: [row['count'], row['stamp']] for row in read_unread(folder)}
    setting = 'telegram_unread:' + str(folder['id'])
    previous = store.setting(setting)
    timestamp = now()
    count = 0
    with store.connect() as db:
        for peer, (unread, stamp) in snapshot.items():
            old_count, old_stamp = (previous or {}).get(peer, [0, ''])
            if previous is not None and unread <= old_count and stamp == old_stamp:
                continue
            thread = 'telegram:' + peer
            ident = hashlib.sha256((thread + timestamp).encode()).hexdigest()
            count += insert(db, dict(id='telegram:' + ident, source='telegram', thread=thread,
                                     job_key=None, at=timestamp, sender='', recipient='',
                                     subject='Непрочитанный рабочий чат', text=f'Непрочитанных сообщений: {unread}',
                                     url=f'https://web.telegram.org/k/#{peer}', kind='review', urgent=False,
                                     next_action='Проверить рабочий диалог', notify=previous is not None))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (setting, json.dumps(snapshot)))
    return count


def send_message(config, text):
    token, chat_id = config.get('token', ''), str(config.get('chat_id', ''))
    proxy = config.get('proxy')
    if proxy is not None and (not isinstance(proxy, str) or not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}', proxy)):
        raise RuntimeError('Telegram: proxy должен быть адресом локального HTTP-прокси')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9]+:[a-zA-Z0-9_-]+', token) or not re.fullmatch(r'[1-9][0-9]*', chat_id):
        raise RuntimeError('Telegram: нужен токен бота и ID личного чата')
    result = request('Telegram', f'https://api.telegram.org/bot{token}/sendMessage',
                     data={'chat_id': chat_id, 'text': text}, proxy=proxy)
    if not result.get('ok'):
        raise RuntimeError('Telegram: уведомление не принято')


def report(store, config, sources):
    stats = store.stats()
    lines = [
        f"Карьерная сводка · {datetime.now(MSK):%d.%m %H:%M} МСК",
        f"Отклики: {stats['confirmed']} всего, {stats['today']} сегодня",
        f"Приглашения: {stats['invitations']} · Интервью: {stats['interviews']} · "
        f"Офферы: {stats['offers']} · Отказы: {stats['rejected']}",
        f"Нужны действия: {stats['needsAction']}",
        '',
    ]
    for name, key, enabled, label in sources:
        status = store.setting(key, {}) if enabled else {}
        if status.get('state') == 'success':
            detail = f"{label}: {status['count']}"
            if status.get('warnings'):
                detail += f" · не прочитано чатов: {len(status['warnings'])}"
        else:
            detail = 'ошибка проверки' if enabled else 'проверка выключена'
        lines.append(f'{name} — {detail}')
    send_message(config, '\n'.join(lines))


def notify(store):
    config = configuration()
    if not config:
        return 0
    now = datetime.now(MSK).strftime('%Y-%m-%dT%H:%M')
    with store.connect() as db:
        pending = db.execute(
            "SELECT id,notify,due,reminded_due FROM (" + LATEST + ") WHERE done=0 "
            "AND ((position=1 AND notify=1) OR (due!='' AND due<=? AND due!=reminded_due))", (now,),
        ).fetchall()
    if not pending:
        return 0
    send_message(config, 'В Career Dashboard есть новые сообщения или наступил срок напоминания.')
    with store.connect() as db:
        for row in pending:
            if row['notify']:
                db.execute('UPDATE inbox SET notify=0 WHERE id=?', (row['id'],))
            if row['due'] and row['due'] <= now:
                db.execute('UPDATE inbox SET reminded_due=? WHERE id=? AND due=?',
                           (row['due'], row['id'], row['due']))
    return len(pending)
