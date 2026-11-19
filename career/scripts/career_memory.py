#!/usr/bin/env python3
import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Memory:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'memory.sqlite3'
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS items (
                key TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                summary TEXT NOT NULL, evidence TEXT NOT NULL, updated_at TEXT NOT NULL
            ); CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL, status TEXT NOT NULL,
                summary TEXT NOT NULL, evidence TEXT NOT NULL, at TEXT NOT NULL
            );''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def record(self, key, kind, status, summary, evidence):
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError('Нужен устойчивый ключ записи длиной до 200 символов')
        if kind not in ('task', 'decision', 'action', 'blocker') or status not in ('open', 'waiting', 'done', 'paused'):
            raise ValueError('Неизвестный тип или статус записи')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            raise ValueError('Нужно краткое описание длиной до 4000 символов')
        if not isinstance(evidence, list) or any(not isinstance(x, str) or len(x) > 1500 for x in evidence):
            raise ValueError('Ссылки на подтверждения должны быть списком строк')
        if status == 'done' and not evidence:
            raise ValueError('Завершённая работа требует ссылки на результат или подтверждение')
        at = datetime.now(timezone.utc).isoformat()
        raw = json.dumps(evidence, ensure_ascii=False)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM items WHERE key=?', (key,)).fetchone()
            if old and old['kind'] != kind:
                raise ValueError('Нельзя менять вид существующей записи')
            if old and all(old[k] == v for k,v in {'status':status,'summary':summary,'evidence':raw}.items()):
                return {'changed':False,'key':key}
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET status=excluded.status,summary=excluded.summary,evidence=excluded.evidence,updated_at=excluded.updated_at', (key,kind,status,summary,raw,at))
            db.execute('INSERT INTO events(key,status,summary,evidence,at) VALUES (?,?,?,?,?)', (key,status,summary,raw,at))
        return {'changed':True,'key':key}

    def context(self, limit=12, offset=0):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('Лимит должен быть от 1 до 50')
        if type(offset) is not int or offset < 0:
            raise ValueError('Смещение должно быть неотрицательным')
        with self.connect() as db:
            sticky = db.execute("SELECT * FROM items WHERE status != 'done' AND (kind = 'decision' OR status = 'paused') ORDER BY updated_at DESC").fetchall()
            other_count = db.execute("SELECT count(*) FROM items WHERE status != 'done' AND kind != 'decision' AND status != 'paused'").fetchone()[0]
            other = db.execute("SELECT * FROM items WHERE status != 'done' AND kind != 'decision' AND status != 'paused' ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit,offset)).fetchall()
            pending = list(sticky) + list(other)
            recent = db.execute('SELECT * FROM items WHERE status = ? ORDER BY updated_at DESC LIMIT ?', ('done',limit)).fetchall()
        def decode(rows):
            return [dict(row) | {'evidence':json.loads(row['evidence'])} for row in rows]
        return {'notice':'Память хранит решения и ссылки на результаты. Актуальные ответы площадок нужно проверить заново; статусы откликов — в дашборде.', 'pending_and_decisions':decode(pending),'recent_completed':decode(recent), 'pending_total':len(sticky)+other_count,'pending_has_more':offset+limit<other_count,'pending_next_offset':offset+limit if offset+limit<other_count else None}


def main():
    parser=argparse.ArgumentParser(description='Постоянная память карьерного оркестратора')
    parser.add_argument('command',choices=['context','record'])
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--limit',type=int,default=12);parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--key');parser.add_argument('--kind');parser.add_argument('--status');parser.add_argument('--summary')
    parser.add_argument('--evidence',action='append',default=[])
    args=parser.parse_args()
    try:
        memory=Memory(args.root)
        value=memory.context(args.limit,args.offset) if args.command=='context' else memory.record(args.key,args.kind,args.status,args.summary,args.evidence)
        print(json.dumps(value,ensure_ascii=False,indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1,'Ошибка: '+str(exc)+'\n')


if __name__=='__main__':main()
