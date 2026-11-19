import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


STARTER_BANK = Path(__file__).resolve().parent / 'training/question-bank.json'


class Trainer:
    def __init__(self, data, source, clock=None):
        self.bank_path = Path(source) / 'interview-prep' / 'question-bank.json'
        self.path = Path(data) / 'trainer.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS progress (
                question_id TEXT PRIMARY KEY, answer TEXT NOT NULL DEFAULT '',
                score INTEGER, repetitions INTEGER NOT NULL DEFAULT 0,
                due TEXT, reviewed_at TEXT, revision INTEGER NOT NULL DEFAULT 0
            )''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bank(self):
        try:
            try:
                bank = self.bank_path.read_text(encoding='utf-8')
            except FileNotFoundError:
                bank = STARTER_BANK.read_text(encoding='utf-8')
            return json.loads(bank)
        except (OSError, ValueError) as exc:
            raise ValueError('Не удалось загрузить базу вопросов тренажёра') from exc

    def state(self, category='', mode='learn'):
        bank = self.bank()
        categories = list(dict.fromkeys(q['category'] for q in bank['questions']))
        if category and category not in categories:
            raise ValueError('Неизвестная тема')
        if mode not in ('learn', 'review'):
            raise ValueError('Неизвестный режим')
        with self.connect() as db:
            progress = {r['question_id']: dict(r) for r in db.execute('SELECT * FROM progress')}
        now = self.clock().isoformat()
        questions = [q for q in bank['questions'] if not category or q['category'] == category]
        reviewed = [q for q in questions if progress.get(q['id'], {}).get('repetitions', 0)]
        due = [q for q in reviewed if progress[q['id']]['due'] <= now]
        fresh = [q for q in questions if not progress.get(q['id'], {}).get('repetitions', 0)]
        order = {key: i for i, key in enumerate(bank.get('first_30', []))}
        fresh.sort(key=lambda q: (order.get(q['id'], 1000), q['priority'] != 'P0', q['id']))
        due.sort(key=lambda q: (progress[q['id']]['due'], q['id']))
        queue = due + (fresh if mode == 'learn' else [])
        item = None
        if queue:
            q = queue[0]
            saved = progress.get(q['id'], {})
            item = {**q, 'answer': saved.get('answer', ''), 'revision': saved.get('revision', 0),
                    'previousScore': saved.get('score'), 'isReview': bool(saved.get('repetitions')),
                    'sources': [{'title': bank['sources'][key][0], 'url': bank['sources'][key][1]}
                                for key in q.get('sources', []) if bank['sources'][key][1]]}
        future = [progress[q['id']]['due'] for q in reviewed if progress[q['id']]['due'] > now]
        return {'categories': categories, 'category': category, 'mode': mode, 'question': item,
                'stats': {'total': len(questions), 'reviewed': len(reviewed), 'new': len(fresh),
                          'confident': sum(progress[q['id']]['score'] >= 2 for q in reviewed), 'due': len(due)},
                'nextReview': min(future) if future else None}

    def save(self, body, grade=False):
        ident, answer, revision = body.get('id'), body.get('answer'), body.get('revision')
        if not isinstance(ident, str) or ident not in {q['id'] for q in self.bank()['questions']}:
            raise ValueError('Вопрос не найден')
        if not isinstance(answer, str) or len(answer) > 15000:
            raise ValueError('Ответ должен содержать не более 15 000 символов')
        if type(revision) is not int or revision < 0:
            raise ValueError('Некорректная версия ответа')
        score = body.get('score')
        if grade and (type(score) is not int or score not in range(4)):
            raise ValueError('Оценка должна быть от 0 до 3')
        with self.connect() as db:
            # Блокировка не даёт двум вкладкам одновременно оценить одну версию вопроса.
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO progress(question_id) VALUES (?)', (ident,))
            row = db.execute('SELECT * FROM progress WHERE question_id=?', (ident,)).fetchone()
            if row['revision'] != revision:
                raise ValueError('Этот вопрос уже оценён в другой вкладке. Обновите тренировку.')
            if grade:
                current = self.clock()
                delay = [timedelta(minutes=10), timedelta(days=1), timedelta(days=3), timedelta(days=7)][score]
                db.execute('''UPDATE progress SET answer=?, score=?, repetitions=repetitions+1,
                    due=?, reviewed_at=?, revision=revision+1 WHERE question_id=?''',
                           (answer, score, (current + delay).isoformat(), current.isoformat(), ident))
            else:
                db.execute('UPDATE progress SET answer=? WHERE question_id=?', (answer, ident))
        return {'saved': True}
