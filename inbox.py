import hashlib
import json
import re
from datetime import datetime

KINDS = {
    'invitation': 'Приглашение', 'question': 'Вопрос', 'rejection': 'Отказ',
    'offer': 'Оффер', 'test': 'Тестовое задание', 'automatic': 'Автоответ',
    'review': 'Проверить',
}
ACTIONS = {
    'invitation': 'Ответить и согласовать время',
    'question': 'Ответить на вопрос',
    'offer': 'Проверить условия',
    'test': 'Уточнить срок и объём задания',
    'review': 'Проверить диалог',
}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, thread TEXT NOT NULL,
    job_key TEXT REFERENCES jobs(key), at TEXT NOT NULL, sender TEXT NOT NULL,
    recipient TEXT NOT NULL, subject TEXT NOT NULL, text TEXT NOT NULL,
    url TEXT NOT NULL, kind TEXT NOT NULL, urgent INTEGER NOT NULL,
    next_action TEXT NOT NULL, due TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0, notify INTEGER NOT NULL DEFAULT 0, reminded_due TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS inbox_thread ON inbox(source,thread,at,id);
'''


def classify(subject, body, headers, sender):
    # Цитата из исходного отклика не описывает новое письмо.
    body = re.split(r'(?im)^\s*(?:>|On .+ wrote:|[- ]*Original Message|.{0,180}писал[аи]?:\s*$)', body, maxsplit=1)[0]
    text = (subject + '\n' + body[:4000]).casefold()
    automatic = headers.get('auto-submitted', '').lower()
    if re.search(r'\b(otp|one.time (?:passcode|code)|verification code|код (?:входа|подтверждения|безопасности)|подтвердите вход|сброс(?:ить)? пароль|reset your password|magic link)\b', text):
        return None, False
    if automatic.startswith('auto-replied'):
        return 'automatic', False
    urgent = bool(re.search(r'\b(сегодня|завтра|срочно|today|tomorrow|urgent|deadline|дедлайн)\b', text))
    rules = (
        ('rejection',
         r'решили не продолж|не готовы (?:пригласить|продолж)|не сможем (?:пригласить|продолж)|выбрали другого'
         r'|к сожалению.{0,100}(?:отказ|не подх|не готов|не смож)'
         r'|не рассматриваем (?:варианты )?удал[её]нн'
         r'|will not be moving forward|won.t be moving forward|other candidates'
         r'|not currently hiring (?:for|to fill) (?:this|the) (?:position|role)'),
        ('offer', r'\bjob offer\b|(?:наш|направляем|высылаем|прилагаем) (?:вам )?оффер|offer (?:letter|of employment)'),
        ('test', r'тестов(?:ое|ого|ым|ому) задани|take.home (?:test|assignment)|coding (?:test|challenge)'),
        ('invitation', r'приглашаем|(?:доступн|удобн).{0,70}(?:созвон|интервью|собеседован)|(?:invite|availability|schedule).{0,80}(?:interview|call)|(?:interview|call).{0,60}(?:availability|schedule)'),
        ('question', r'\?|(?:уточните|расскажите|подскажите|пришлите|заполните)|please (?:share|provide|confirm|send)'),
    )
    acknowledgement = re.search(
        r'out of office|automatic reply|автоответ|отклик (?:получен|принят)|(?:received|thank you for) your application|получили (?:ваш|ваше) (?:отклик|резюме)', text)
    for kind, pattern in rules:
        if kind != 'rejection' and acknowledgement:
            return 'automatic', False
        if re.search(pattern, text):
            return kind, urgent or kind == 'offer'
    if (automatic not in ('', 'no')
            or re.match(r'(?:no[-_]?reply|mailer-daemon)@', sender, re.I)
            or re.search(r'\bобращение зарегистрировано\s+и передано в отдел подбора\b', text)):
        return 'automatic', False
    return 'review', urgent


def insert(db, message):
    keys = ('id', 'source', 'thread', 'job_key', 'at', 'sender', 'recipient', 'subject',
            'text', 'url', 'kind', 'urgent', 'next_action', 'notify')
    return db.execute(
        'INSERT OR IGNORE INTO inbox (' + ','.join(keys) + ') VALUES (' + ','.join('?' for _ in keys) + ')',
        [message[key] for key in keys],
    ).rowcount


def record_hh(db, key, current, row, status, at):
    chat = row.get('chat_url', '')
    status_kinds = {'invited': 'invitation', 'rejected': 'rejection'}
    kind = status_kinds.get(status)
    messages = row.get('messages') or []
    current = current or {'payload': '{}', 'chat_url': '', 'platform_status': ''}
    previous = json.loads(current['payload']).get('messages') or []
    notify = False
    outgoing = row.get('last_message_outgoing') is True
    if messages and outgoing:
        # Закрываем только совпавший старый фрагмент чата без ручного черновика или срока.
        signature = json.dumps(messages[-2:], ensure_ascii=False)
        message_id = hashlib.sha256((key + ':' + signature).encode()).hexdigest()
        db.execute("""UPDATE inbox SET done=1, notify=0 WHERE id=? AND kind='review'
                      AND urgent=0 AND next_action=? AND draft='' AND due=''""",
                   ('hh:' + message_id, ACTIONS['review']))
    if messages and messages != previous and not outgoing:
        body = messages[-1]
        kind = 'review'
        signature = json.dumps(messages[-2:], ensure_ascii=False)
        notify = bool(previous)
    elif kind and row.get('platform_status') and row['platform_status'] != current['platform_status']:
        body = row['platform_status']
        signature = body
        notify = kind == 'invitation' and bool(current['platform_status'])
    else:
        return
    message_id = hashlib.sha256((key + ':' + signature).encode()).hexdigest()
    insert(db, dict(id='hh:' + message_id, source='hh', thread=key, job_key=key, at=at,
                    sender='', recipient='', subject=row.get('company') or row.get('title') or 'HH',
                    text=body, url=chat or current['chat_url'] or row.get('url', ''),
                    kind=kind, urgent=False, next_action=ACTIONS.get(kind, ''), notify=notify))


def update(db, ident, values):
    allowed = {'kind', 'urgent', 'next_action', 'due', 'draft', 'done'}
    if not values or values.keys() - allowed:
        raise ValueError('Неизвестные поля входящего')
    for key, value in values.items():
        if key in {'done', 'urgent'}:
            if not isinstance(value, bool):
                raise ValueError('Ожидалось логическое значение')
        elif not isinstance(value, str) or len(value) > 20000:
            raise ValueError('Некорректный текст')
    if 'kind' in values and values['kind'] not in KINDS:
        raise ValueError('Неизвестный тип сообщения')
    if values.get('due'):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', values['due']):
            raise ValueError('Укажите дату и время по Москве')
        datetime.fromisoformat(values['due'])
    query = ','.join(key + '=?' for key in values)
    if not db.execute('UPDATE inbox SET ' + query + ' WHERE id=?', [*values.values(), ident]).rowcount:
        raise KeyError(ident)
    if values.get('done'):
        db.execute('UPDATE inbox SET notify=0 WHERE id=?', (ident,))
    return {'saved': True}


LATEST = """SELECT inbox.*,row_number() OVER (PARTITION BY source,thread ORDER BY at DESC,id DESC) AS position
            FROM inbox"""


def listing(db, page=1, pending=False):
    page = max(1, min(10000, page))
    condition = "(position=1 OR (done=0 AND (due!='' OR draft!='')))"
    if pending:
        condition += " AND done=0 AND (urgent=1 OR next_action!='' OR due!='')"
    query = ' FROM (' + LATEST + ') WHERE ' + condition
    total = db.execute('SELECT count(*)' + query).fetchone()[0]
    rows = db.execute(
        'SELECT id,source,job_key,at,sender,subject,kind,urgent,next_action,due,done,draft' + query
        + ' ORDER BY done ASC,urgent DESC,at DESC,id DESC LIMIT 20 OFFSET ?', ((page - 1) * 20,),
    )
    return {'items': [dict(row) for row in rows], 'pages': max(1, (total + 19) // 20)}
