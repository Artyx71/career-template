#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

import gmail
import inbox

from store import MSK, PLATFORMS, Store, now


VACANCY_URLS = {
    'hh': re.compile(r'https?://(?:[a-z0-9-]+\.)*(?:hh\.(?:ru|kz|uz)|rabota\.by)/vacancy/([0-9]+)/?(?:[?#].*)?', re.I),
    'habr': re.compile(r'https?://career\.habr\.com/vacancies/([0-9]+)/?(?:[?#].*)?', re.I),
    'getmatch': re.compile(r'https?://getmatch\.ru/vacancies/([0-9]+)(?:-[^/?#]+)?/?(?:[?#].*)?', re.I),
    'geekjob': re.compile(r'https?://geekjob\.ru/vacancy/([a-f0-9]{24})/?(?:[?#].*)?', re.I),
}


def vacancy_key(url):
    for platform, pattern in VACANCY_URLS.items():
        match = pattern.fullmatch(url)
        if match:
            return platform, match[1].lower()
    return None


def direct_url_key(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None:
            return None
        path = parts.path.rstrip('/') or '/'
        if parts.hostname == 'jobs.ashbyhq.com' and re.fullmatch(r'/[^/]+/[0-9a-f-]{36}/application', path):
            path = path.removesuffix('/application')
        return 'url', parts._replace(netloc=parts.netloc.lower(), path=path).geturl()
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description='Учёт откликов для скриптов и агента')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('import', help='Перечитать все журналы')
    sub.add_parser('stats', help='Показать счётчики')
    unseen = sub.add_parser('unseen', help='Показать вакансии, которых ещё нет в журнале')
    unseen.add_argument('platform', choices=(*PLATFORMS, 'urls'))
    unseen.add_argument('ids', nargs='+')
    backup = sub.add_parser('backup', help='Сделать согласованную копию SQLite')
    backup.add_argument('destination', type=Path)
    record = sub.add_parser('record', help='Записать результат реального действия')
    record.add_argument('--platform', choices=PLATFORMS, required=True)
    record.add_argument('--id', required=True)
    record.add_argument('--title', required=True)
    record.add_argument('--company', default='')
    record.add_argument('--url', required=True)
    record.add_argument('--status', choices=['sent', 'already_sent', 'needs_input', 'out_of_scope', 'submitted_unconfirmed'], required=True)
    record.add_argument('--evidence', required=True)
    record.add_argument('--resume', default='')
    record.add_argument('--cover-file', type=Path)
    auth = sub.add_parser('gmail-auth', help='Авторизовать Gmail для чтения')
    auth.add_argument('--client', type=Path, required=True)
    auth.add_argument('--account', required=True)
    incoming = sub.add_parser('inbox', help='Показать входящие, требующие действий')
    incoming.add_argument('--page', type=int, default=1)
    context = sub.add_parser('inbox-context', help='Получить контекст выбранного карьерного письма')
    context.add_argument('id')
    draft = sub.add_parser('inbox-draft', help='Сохранить локальный черновик через работающий дашборд')
    draft.add_argument('id')
    draft.add_argument('--file', required=True, help='Файл с черновиком или - для stdin')
    draft.add_argument('--port', type=int, default=4317)
    args = parser.parse_args()
    if args.command == 'gmail-auth':
        gmail.authorize(args.client, args.account)
        print('Авторизация Gmail сохранена')
        return
    if args.command == 'inbox-draft':
        body = (sys.stdin.read() if args.file == '-' else Path(args.file).read_text()).strip()
        if not body or len(body) > 20000:
            raise ValueError('Нужен текст черновика длиной до 20 000 символов')
        if not 1 <= args.port <= 65535:
            raise ValueError('Некорректный порт')
        base = f'http://127.0.0.1:{args.port}'
        opener = build_opener(gmail.NoRedirect())
        with opener.open(base + '/api/bootstrap', timeout=5) as response:
            token = json.load(response)['token']
        with opener.open(Request(base + '/api/inbox/' + quote(args.id, safe=''),
                                 data=json.dumps({'draft': body}).encode(),
                                 headers={'X-Career-Token': token, 'Content-Type': 'application/json'}), timeout=5) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
        return
    source = Path(os.environ.get('CAREER_SOURCE_DIR', Path.home() / 'Documents/Career-template'))
    data = Path(os.environ.get('CAREER_DATA_DIR', Path.home() / '.local/share/career-template'))
    if args.command == 'unseen':
        candidates = {}
        for value in args.ids:
            key = (vacancy_key(value) or direct_url_key(value)) if args.platform == 'urls' else (args.platform, value)
            if key is None:
                unseen.error('В режиме urls нужны прямые HTTP(S) ссылки на вакансии')
            candidates.setdefault(key, value)
        platforms = tuple(dict.fromkeys(platform for platform, _ in candidates))
        placeholders = ','.join('?' for _ in platforms)
        uri = (data / 'career.sqlite3').resolve().as_uri() + '?mode=ro'
        db = sqlite3.connect(uri, uri=True)
        try:
            seen = set(db.execute(f'SELECT platform, external_id FROM jobs WHERE platform IN ({placeholders})',
                                  platforms))
            if any(platform in VACANCY_URLS for platform in platforms):
                for (url,) in db.execute("SELECT url FROM jobs WHERE platform = 'company' AND confirmed = 1"):
                    key = vacancy_key(url)
                    if key:
                        seen.add(key)
            if 'url' in platforms:
                for (url,) in db.execute("SELECT url FROM jobs WHERE url != ''"):
                    key = direct_url_key(url)
                    if key in candidates:
                        seen.add(key)
        finally:
            db.close()
        print(json.dumps([value for key, value in candidates.items() if key not in seen], ensure_ascii=False))
        return
    store = Store(data, source)
    if args.command == 'inbox':
        with store.connect() as db:
            print(json.dumps(inbox.listing(db, args.page, pending=True), ensure_ascii=False, indent=2))
    elif args.command == 'inbox-context':
        print(json.dumps(gmail.context(store, os.environ.get('CAREER_GMAIL_LABEL', 'Career'), args.id),
                         ensure_ascii=False, indent=2))
    elif args.command == 'record':
        directory = source / datetime.now(MSK).date().isoformat()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f'{args.platform}-applications.jsonl'
        row = {'id': args.id, 'title': args.title, 'company': args.company, 'url': args.url, 'status': args.status, 'evidence': args.evidence, 'time': now()}
        if args.resume:
            row['resume'] = args.resume
        if args.cover_file:
            row['coverLetter'] = args.cover_file.read_text()
            row['coverLetterSent'] = args.status == 'sent'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            file.write(json.dumps(row, ensure_ascii=False) + '\n')
            file.flush()
            os.fsync(file.fileno())
        store.import_sources()
        print(json.dumps({'recorded': f'{args.platform}:{args.id}'}, ensure_ascii=False))
    elif args.command == 'import':
        print(json.dumps(store.import_sources(force=True), ensure_ascii=False, indent=2))
    elif args.command == 'stats':
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.command == 'backup':
        args.destination.parent.mkdir(parents=True, exist_ok=True)
        with store.connect() as db:
            target = sqlite3.connect(args.destination)
            try:
                db.backup(target)
            finally:
                target.close()
        args.destination.chmod(0o600)
        print('Резервная копия создана: ' + str(args.destination))


if __name__ == '__main__':
    try:
        main()
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from None
