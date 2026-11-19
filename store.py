import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import inbox

MSK = timezone(timedelta(hours=3))
PLATFORMS = {'hh': 'HeadHunter', 'habr': 'Хабр Карьера', 'company': 'Сайты компаний', 'getmatch': 'getmatch', 'geekjob': 'GeekJob', 'superjob': 'SuperJob'}
STAGES = {'queued': 'В очереди', 'sent': 'Ожидание', 'viewed': 'Просмотрено', 'reply': 'Есть ответ', 'invited': 'Приглашение', 'interview': 'Встреча назначена', 'offer': 'Оффер', 'rejected': 'Отказ', 'blocked': 'Нужны данные', 'unconfirmed': 'Отправка не подтверждена', 'skipped': 'Не подходит'}
CONFIRMED = {'sent', 'already_sent'}


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_date(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def local_day(value):
    value = parse_date(value)
    return value.astimezone(MSK).date().isoformat() if value else None


def stage_for(status):
    if status in CONFIRMED or status == 'recruiter_contacted':
        return 'sent'
    if status == 'submitted_unconfirmed':
        return 'unconfirmed'
    if status in {'out_of_scope', 'remote_conflict', 'external_form', 'closed'}:
        return 'skipped'
    if status:
        return 'blocked'
    return 'queued'


def vacancy_id(row, platform):
    if row.get('id'):
        return str(row['id'])
    url = row.get('url', '')
    match = re.search(r'/(?:vacanc(?:y|ies))/(\w+)', url)
    if match:
        return match[1].split('-')[0] if platform == 'getmatch' else match[1]
    return hashlib.sha256((url or str(row.get('company', '')) + str(row.get('title', ''))).encode()).hexdigest()[:18]


def canonical_url(platform, ident):
    return {'hh': f'https://hh.ru/vacancy/{ident}', 'habr': f'https://career.habr.com/vacancies/{ident}', 'getmatch': f'https://getmatch.ru/vacancies/{ident}', 'geekjob': f'https://geekjob.ru/vacancy/{ident}'}.get(platform, '')


class Store:
    def __init__(self, data_dir, source_dir):
        self.data_dir, self.source_dir = Path(data_dir), Path(source_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.data_dir / 'career.sqlite3'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, platform TEXT NOT NULL, external_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT 'queued', confirmed INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT, new_send INTEGER NOT NULL DEFAULT 0, last_at TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '', next_action TEXT NOT NULL DEFAULT '',
                follow_up TEXT NOT NULL DEFAULT '', manual_stage TEXT, response_stage TEXT,
                platform_status TEXT NOT NULL DEFAULT '', chat_url TEXT NOT NULL DEFAULT '', response_at TEXT,
                response TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, job_key TEXT NOT NULL REFERENCES jobs(key), kind TEXT NOT NULL,
                at TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS events_job ON events(job_key, at);
            CREATE TABLE IF NOT EXISTS imports (path TEXT PRIMARY KEY, stamp TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            db.executescript(inbox.SCHEMA)
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def _upsert(self, db, platform, row, at):
        ident = vacancy_id(row, platform)
        key = f'{platform}:{ident}'
        company = row.get('company', '')
        if isinstance(company, list):
            company = next((x for x in company if x), '')
        title = row.get('title', '')
        db.execute('INSERT OR IGNORE INTO jobs(key,platform,external_id,last_at) VALUES (?,?,?,?)', (key, platform, ident, at))
        current = db.execute('SELECT payload FROM jobs WHERE key=?', (key,)).fetchone()
        payload = json.loads(current['payload'])
        payload.update({k: v for k, v in row.items() if v not in (None, '', [], {})})
        db.execute('UPDATE jobs SET title=CASE WHEN ? != \'\' THEN ? ELSE title END, company=CASE WHEN ? != \'\' THEN ? ELSE company END, url=CASE WHEN ? != \'\' THEN ? ELSE url END,payload=? WHERE key=?',
                   (title, title, company, company, row.get('url') or canonical_url(platform, ident), row.get('url') or canonical_url(platform, ident), json.dumps(payload, ensure_ascii=False), key))
        return key

    def import_sources(self, force=False):
        warnings, imported = [], 0
        if not self.source_dir.exists():
            result = {'at': now(), 'imported': 0, 'warnings': ['Папка с журналами недоступна'], 'source': str(self.source_dir)}
            self.set_setting('last_import', result)
            return result
        filenames = {f'{platform}-{suffix}' for platform in PLATFORMS for suffix in [
            'job-queue.json', 'job-research.json', 'vacancy-research.json', 'applications.jsonl']}
        filenames.update(['hh-chat-review.json', 'hh-chat-current-state.json', 'hh-chat-read-final.json', 'hh-chat-actions.jsonl'])
        directories = sorted(Path(directory) for directory, _, files in os.walk(self.source_dir)
                             if filenames.intersection(files))
        seen_events = {}
        with self.connect() as db:
            for directory in directories:
                for platform in PLATFORMS:
                    for suffix in ['job-queue.json', 'job-research.json', 'vacancy-research.json', 'applications.jsonl']:
                        path = directory / f'{platform}-{suffix}'
                        if not path.exists():
                            continue
                        stat = path.stat()
                        stamp = f'{stat.st_mtime_ns}:{stat.st_size}'
                        prior = db.execute('SELECT stamp FROM imports WHERE path=?', (str(path),)).fetchone()
                        if not force and prior and prior[0] == stamp:
                            continue
                        try:
                            raw = path.read_text()
                            if suffix.endswith('jsonl'):
                                rows = []
                                for number, line in enumerate(raw.splitlines(), 1):
                                    if not line.strip():
                                        continue
                                    try:
                                        rows.append(json.loads(line))
                                    except json.JSONDecodeError:
                                        warnings.append(f'{path.name}: некорректная строка {number}; остальные записи импортированы')
                            else:
                                rows = json.loads(raw)
                                if isinstance(rows, dict) and isinstance(rows.get('jobs'), list):
                                    rows = rows['jobs']
                                if not isinstance(rows, list):
                                    raise ValueError('ожидался список')
                            for row in rows:
                                if not isinstance(row, dict):
                                    continue
                                at = row.get('time') or row.get('timestamp') or datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                                key = self._upsert(db, platform, row, at)
                                if suffix != 'applications.jsonl':
                                    continue
                                content = json.dumps(row, sort_keys=True, ensure_ascii=False)
                                if key not in seen_events:
                                    seen_events[key] = {
                                        json.dumps(json.loads(event[0]), sort_keys=True, ensure_ascii=False)
                                        for event in db.execute("SELECT payload FROM events WHERE job_key=? AND kind IN ('application','delivery_failure')", (key,))
                                    }
                                if content in seen_events[key]:
                                    continue
                                seen_events[key].add(content)
                                event_id = hashlib.sha256((key + ':' + content).encode()).hexdigest()
                                text = row.get('verification') or row.get('evidence') or row.get('reason') or row.get('status', '')
                                kind = 'delivery_failure' if row.get('status') == 'delivery_failed' else 'application'
                                created = db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)', (event_id, key, kind, at, str(text), str(path), json.dumps(row, ensure_ascii=False))).rowcount
                                imported += created
                                status = row.get('status', '')
                                job = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
                                confirmed = job['confirmed'] or status in CONFIRMED
                                new_send = job['new_send'] or status == 'sent'
                                sent_at = job['sent_at']
                                if status == 'sent' and parse_date(row.get('time') or row.get('timestamp')):
                                    sent_at = min(sent_at, at) if sent_at else at
                                if status == 'recruiter_contacted' and row.get('type') == 'recruiter_outreach' and parse_date(row.get('time') or row.get('timestamp')):
                                    sent_at = min(sent_at, at) if sent_at else at
                                stage = 'sent' if confirmed else stage_for(status)
                                db.execute('UPDATE jobs SET stage=?,confirmed=?,new_send=?,sent_at=?,last_at=? WHERE key=?', (stage, int(confirmed), int(new_send), sent_at, max(at, job['last_at']) if job['confirmed'] else at, key))
                            db.execute('INSERT OR REPLACE INTO imports VALUES (?,?)', (str(path), stamp))
                        except (OSError, ValueError, TypeError) as exc:
                            warnings.append(f'{path.name}: {exc}')
            self._reconcile_delivery_failures(db)
            self._reconcile_duplicate_applications(db)
        self.import_chat_history(directories)
        result = {'at': now(), 'imported': imported, 'warnings': warnings, 'source': str(self.source_dir)}
        self.set_setting('last_import', result)
        return result

    def _reconcile_duplicate_applications(self, db):
        for event in db.execute("SELECT job_key,payload FROM events WHERE kind='application'").fetchall():
            row = json.loads(event['payload'])
            canonical = row.get('duplicateOf')
            if row.get('status') != 'duplicate' or not canonical or canonical == event['job_key']:
                continue
            original = db.execute('SELECT confirmed FROM jobs WHERE key=?', (canonical,)).fetchone()
            if original and original['confirmed']:
                db.execute("UPDATE jobs SET confirmed=0,new_send=0,sent_at=NULL,stage='skipped' WHERE key=?", (event['job_key'],))

    def _reconcile_delivery_failures(self, db):
        for job in db.execute("SELECT DISTINCT job_key FROM events WHERE kind='delivery_failure'").fetchall():
            events = db.execute("SELECT at,payload FROM events WHERE job_key=? AND kind IN ('application','delivery_failure')", (job['job_key'],)).fetchall()
            records = [(parse_date(e['at']), json.loads(e['payload']), e['at']) for e in events if parse_date(e['at'])]
            failed_at = max((at for at, row, _ in records if row.get('status') == 'delivery_failed'), default=None)
            if failed_at is None:
                continue
            delivered = [(row, at) for date, row, at in records if date > failed_at and row.get('status') in CONFIRMED]
            dated_sends = [at for row, at in delivered if row.get('status') == 'sent' and parse_date(row.get('time') or row.get('timestamp'))]
            sent_at = min(dated_sends, key=parse_date) if dated_sends else None
            db.execute('UPDATE jobs SET confirmed=?,new_send=?,sent_at=?,stage=? WHERE key=?',
                       (int(bool(delivered)), int(any(row.get('status') == 'sent' for row, _ in delivered)), sent_at, 'sent' if delivered else 'blocked', job['job_key']))

    def import_chat_history(self, directories):
        for directory in directories:
            mapping = {}
            candidates = []
            for filename in ['hh-chat-review.json', 'hh-chat-current-state.json', 'hh-chat-read-final.json', 'hh-chat-actions.jsonl']:
                path = directory / filename
                if not path.exists():
                    continue
                try:
                    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if filename.endswith('jsonl') else json.loads(path.read_text())
                except (ValueError, OSError):
                    continue
                for row in rows:
                    chat_id = str(row.get('id', ''))
                    controls = row.get('controls', '')
                    match = re.search(r'https://[^\s"<>]*hh\.ru/vacancy/(\d+)', controls)
                    if match:
                        mapping[chat_id] = match[1]
                    candidates.append((row, path))
            with self.connect() as db:
                for row, path in candidates:
                    chat_id = str(row.get('id', ''))
                    vacancy = mapping.get(chat_id)
                    if not vacancy:
                        title = (row.get('title') or '').split('\n')[0].strip()
                        found = db.execute("SELECT external_id FROM jobs WHERE platform='hh' AND title=?", (title,)).fetchall() if title else []
                        if len(found) == 1:
                            vacancy = found[0][0]
                    if not vacancy:
                        continue
                    key = f'hh:{vacancy}'
                    if not db.execute('SELECT 1 FROM jobs WHERE key=?', (key,)).fetchone():
                        continue
                    db.execute('UPDATE jobs SET chat_url=? WHERE key=?', (f'https://hh.ru/chat/{chat_id}', key))
                    messages = row.get('messages') or []
                    if not messages:
                        messages = [{'text': t} for t in row.get('texts', [])]
                    if row.get('last'):
                        messages = [{'text': row['last']}]
                    if row.get('textSent') and not messages:
                        messages = [{'text': row['textSent']}]
                    for message in messages:
                        text = message.get('text', '')
                        if not text:
                            continue
                        event_id = hashlib.sha256((key + str(path) + str(message.get('id') or text)).encode()).hexdigest()
                        timestamp = row.get('time') or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                        db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)', (event_id, key, 'chat_archive', timestamp, text, path.name, json.dumps({'chat_id': chat_id, 'direction': 'unknown', 'timeIsImport': not bool(row.get('time'))}, ensure_ascii=False)))

    def apply_observations(self, records):
        count = 0
        with self.connect() as db:
            for row in records:
                if not row.get('id'):
                    continue
                at = row.get('at') or now()
                key = 'hh:' + str(row['id'])
                previous = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
                self._upsert(db, 'hh', row, at)
                current = previous or db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
                status = row.get('platform_status') or current['platform_status']
                response_stage = 'rejected' if 'Отказ' in status else 'invited' if any(t in status for t in ['Собеседование', 'Приглашение']) else 'viewed' if status == 'Просмотрен' else None
                inbox.record_hh(db, key, previous, row, response_stage, at)
                if status == current['platform_status']:
                    response_stage = current['response_stage']
                manual_response = db.execute("SELECT 1 FROM events WHERE job_key=? AND kind='manual_response' LIMIT 1", (key,)).fetchone()
                response = current['response'] if manual_response else row.get('response') or current['response']
                changed = status != current['platform_status'] or response != current['response']
                response_at = at if changed and response_stage else current['response_at']
                db.execute('UPDATE jobs SET confirmed=1,stage=\'sent\',platform_status=?,response_stage=?,chat_url=CASE WHEN ? != \'\' THEN ? ELSE chat_url END,response_at=?,response=?,last_at=? WHERE key=?', (status, response_stage, row.get('chat_url', ''), row.get('chat_url', ''), response_at, response, at, key))
                event_data = {'id': row['id'], 'platform_status': status, 'response': row.get('response')}
                event_id = hashlib.sha256(('hh-browser:' + json.dumps(event_data, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)', (event_id, key, 'platform_status', at, status + ('\n' + row['response'] if row.get('response') else ''), 'hh-browser', json.dumps(row, ensure_ascii=False)))
                count += 1
        return count

    def _row(self, row):
        value = dict(row)
        value['effective_stage'] = row['manual_stage'] or row['response_stage'] or row['stage']
        value['stage_label'] = STAGES[value['effective_stage']]
        value['platform_label'] = PLATFORMS[row['platform']]
        value['confirmed'] = bool(value['confirmed'])
        value['new_send'] = bool(value['new_send'])
        value['payload'] = json.loads(value['payload'])
        return value

    def jobs(self, params):
        where, args = [], []
        stage_expr = 'COALESCE(manual_stage,response_stage,stage)'
        query = params.get('q', '').strip()
        if query:
            where.append('(title LIKE ? OR company LIKE ? OR note LIKE ? OR response LIKE ?)')
            args.extend([f'%{query}%'] * 4)
        if params.get('platform') in PLATFORMS:
            where.append('platform=?')
            args.append(params['platform'])
        if params.get('stage') in STAGES:
            where.append(stage_expr + '=?')
            args.append(params['stage'])
        view = params.get('view', 'applications')
        if view == 'applications':
            where.append('(confirmed=1 OR stage=\'unconfirmed\')')
        elif view == 'replies':
            where.append(stage_expr + " IN ('reply','invited','interview','offer','rejected')")
        elif view == 'tasks':
            where.append("(next_action != '' OR stage='blocked' OR (follow_up != '' AND follow_up <= ?))")
            args.append(datetime.now(MSK).date().isoformat())
        elif view == 'queue':
            where.append('confirmed=0')
        if params.get('date'):
            where.append('sent_at IS NOT NULL AND date(sent_at,\'+3 hours\')=?')
            args.append(params['date'])
        condition = ' WHERE ' + ' AND '.join(where) if where else ''
        sort = {'company': 'company COLLATE NOCASE ASC', 'oldest': 'COALESCE(sent_at,last_at) ASC', 'follow_up': "CASE WHEN follow_up='' THEN 1 ELSE 0 END,follow_up ASC", 'recent': 'COALESCE(response_at,sent_at,last_at) DESC'}.get(params.get('sort', 'recent'), 'COALESCE(response_at,sent_at,last_at) DESC')
        page = max(1, min(10000, int(params.get('page', 1))))
        limit = max(1, min(100, int(params.get('limit', 20))))
        with self.connect() as db:
            total = db.execute('SELECT count(*) FROM jobs' + condition, args).fetchone()[0]
            rows = db.execute('SELECT * FROM jobs' + condition + ' ORDER BY ' + sort + ',key ASC LIMIT ? OFFSET ?', [*args, limit, (page - 1) * limit]).fetchall()
        return {'items': [self._row(row) for row in rows], 'total': total, 'page': page, 'limit': limit, 'pages': max(1, (total + limit - 1) // limit)}

    def detail(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
            if not row:
                return None
            value = self._row(row)
            value['events'] = [dict(x) | {'payload': json.loads(x['payload'])} for x in db.execute('SELECT * FROM events WHERE job_key=? ORDER BY at DESC,id DESC', (key,))]
            return value

    def update(self, key, values):
        allowed = {'note', 'next_action', 'follow_up', 'manual_stage', 'response'}
        if set(values) - allowed:
            raise ValueError('Неизвестное поле')
        if values.get('manual_stage') and values['manual_stage'] not in STAGES:
            raise ValueError('Неизвестный статус')
        if values.get('follow_up'):
            try:
                datetime.strptime(values['follow_up'], '%Y-%m-%d')
            except ValueError:
                raise ValueError('Некорректная дата') from None
        for value in values.values():
            if value is not None and (not isinstance(value, str) or len(value) > 20000):
                raise ValueError('Слишком длинное или некорректное значение')
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
            if not row:
                raise KeyError(key)
            if 'response' in values and values['response'] and values['response'] != row['response']:
                at = now()
                db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)', (hashlib.sha256((key + at).encode()).hexdigest(), key, 'manual_response', at, values['response'], 'manual', '{}'))
                values['response_at'] = at
            if values:
                db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE key=?', [*values.values(), key])
        return self.detail(key)

    def stats(self):
        today = datetime.now(MSK).date().isoformat()
        with self.connect() as db:
            rows = [self._row(x) for x in db.execute('SELECT * FROM jobs')]
        confirmed = [x for x in rows if x['confirmed']]
        sent = [x for x in confirmed if x['new_send']]
        today_count = sum(local_day(x['sent_at']) == today for x in sent)
        counts = {s: sum(x['effective_stage'] == s for x in rows) for s in STAGES}
        dates = sorted({local_day(x['sent_at']) for x in sent if local_day(x['sent_at'])})[-14:]
        daily = [{'date': d, 'count': sum(local_day(x['sent_at']) == d for x in sent)} for d in dates]
        target_setting = self.setting('daily_application_target', {})
        daily_target = target_setting.get('target') if target_setting.get('date') == today else None
        platforms = [{'id': k, 'name': v, 'confirmed': sum(x['platform'] == k for x in confirmed), 'new': sum(x['platform'] == k for x in sent), 'queued': sum(x['platform'] == k and not x['confirmed'] for x in rows)} for k, v in PLATFORMS.items()]
        return {'confirmed': len(confirmed), 'new': len(sent), 'previous': sum(not x['new_send'] for x in confirmed), 'today': today_count, 'todayDate': today, 'dailyTarget': daily_target, 'remainingToday': max(0, daily_target - today_count) if isinstance(daily_target, int) else None, 'timezone': 'Europe/Moscow', 'rejected': counts['rejected'], 'invitations': counts['invited'], 'interviews': counts['interview'], 'offers': counts['offer'], 'needsAction': sum(bool(x['next_action']) or x['effective_stage'] == 'blocked' or bool(x['follow_up'] and x['follow_up'] <= today) for x in rows), 'counts': counts, 'platforms': platforms, 'daily': daily, 'lastImport': self.setting('last_import'), 'hhSync': self.setting('hh_sync', {'state': 'idle'}), 'autoHH': self.setting('auto_hh', False), 'generatedAt': now()}

    def export_csv(self, params):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Площадка', 'Компания', 'Вакансия', 'Статус', 'Отправлен', 'Ссылка', 'Ответ', 'Заметка', 'Следующее действие', 'Напомнить'])
        for page in range(1, 10001):
            result = self.jobs(dict(params, page=str(page), limit='100'))
            for row in result['items']:
                values = [row['platform_label'], row['company'], row['title'], row['stage_label'], row['sent_at'] or '', row['url'], row['response'], row['note'], row['next_action'], row['follow_up']]
                writer.writerow(["'" + str(v) if str(v).startswith(('=', '+', '-', '@')) else v for v in values])
            if page >= result['pages']:
                break
        return '\ufeff' + output.getvalue()
