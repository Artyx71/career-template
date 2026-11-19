import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from store import Store, local_day


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'source' / '2026-11-07'
        self.source.mkdir(parents=True)
        self.store = Store(root / 'data', root / 'source')

    def write(self, filename, rows):
        target = self.source / filename
        target.write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in rows) + '\n' if filename.endswith('jsonl') else json.dumps(rows, ensure_ascii=False))
        return target

    def test_повторные_события_не_увеличивают_число_откликов(self):
        self.write('hh-job-queue.json', [{'id': '1', 'title': 'Frontend', 'company': 'Команда'}])
        rows = [{'id': '1', 'status': 'needs_input', 'time': '2026-11-07T19:00:00Z'}, {'id': '1', 'status': 'sent', 'time': '2026-11-07T21:05:00Z'}, {'id': '1', 'status': 'sent', 'coverLetterSent': True, 'time': '2026-11-07T21:06:00Z'}]
        self.write('hh-applications.jsonl', rows)
        self.store.import_sources()
        self.store.import_sources(force=True)
        stats = self.store.stats()
        self.assertEqual(stats['new'], 1)
        self.assertEqual(stats['confirmed'], 1)
        row = self.store.detail('hh:1')
        self.assertEqual(row['title'], 'Frontend')
        self.assertEqual(row['sent_at'], '2026-11-07T21:05:00Z')
        self.assertEqual(len(row['events']), 3)
        self.assertEqual(local_day(row['sent_at']), '2026-11-08')

    def test_старые_отклики_и_неподтвержденные_отправки_считаются_отдельно(self):
        self.write('hh-applications.jsonl', [{'id': '1', 'status': 'already_sent', 'time': '2026-11-07T21:05:00Z'}])
        self.write('company-applications.jsonl', [{'company': 'Ozon', 'url': 'https://ozon.tech/vacancies/abc', 'status': 'submitted_unconfirmed'}])
        self.store.import_sources()
        stats = self.store.stats()
        self.assertEqual((stats['new'], stats['confirmed'], stats['previous']), (0, 1, 1))
        self.assertIsNone(self.store.detail('hh:1')['sent_at'])
        self.assertEqual(stats['counts']['unconfirmed'], 1)

    def test_подтвержденные_дубли_по_городам_не_увеличивают_счетчик(self):
        self.write('hh-applications.jsonl', [
            {'id': '1', 'status': 'sent', 'time': '2026-11-07T21:05:00Z'},
            {'id': '2', 'status': 'sent', 'time': '2026-11-07T21:06:00Z'},
            {'id': '2', 'status': 'duplicate', 'duplicateOf': 'hh:1', 'time': '2026-11-07T21:07:00Z'},
        ])
        self.store.import_sources()
        self.store.import_sources(force=True)
        self.assertEqual(self.store.stats()['confirmed'], 1)
        self.assertEqual(self.store.stats()['new'], 1)
        self.assertEqual(len(self.store.detail('hh:2')['events']), 2)
        self.assertEqual(self.store.detail('hh:2')['effective_stage'], 'skipped')

    def test_разные_площадки_с_одинаковым_id_не_объединяются(self):
        for platform in ['hh', 'habr']:
            self.write(platform + '-applications.jsonl', [{'id': '1', 'status': 'sent', 'time': '2026-11-07T20:00:00Z'}])
        self.store.import_sources()
        self.assertEqual(self.store.stats()['confirmed'], 2)

    def test_новые_id_hh_исключают_подтвержденную_отправку_через_компанию(self):
        self.write('hh-applications.jsonl', [{'id': '10', 'status': 'sent'}])
        self.write('company-applications.jsonl', [
            {'id': 'email-a', 'url': 'https://vladikavkaz.hh.ru/vacancy/11?query=Vue', 'status': 'sent'},
            {'id': 'email-b', 'url': 'https://hh.ru/vacancy/12', 'status': 'submitted_unconfirmed'},
            {'id': 'email-c', 'url': 'https://hh.ru.example.org/vacancy/13', 'status': 'sent'},
        ])
        self.store.import_sources()
        command = Path(__file__).resolve().parents[1] / 'manage.py'
        result = subprocess.check_output(
            [sys.executable, str(command), 'unseen', 'hh', '13', '11', '12', '10', '13'],
            env={**os.environ, 'CAREER_DATA_DIR': str(self.store.data_dir)},
        )
        self.assertEqual(json.loads(result), ['13', '12'])

    def test_новые_id_хабра_исключают_подтвержденную_отправку_через_компанию(self):
        self.write('company-applications.jsonl', [
            {'id': 'email-a', 'url': 'https://career.habr.com/vacancies/11/?from=search#response', 'status': 'sent'},
            {'id': 'email-b', 'url': 'https://career.habr.com/vacancies/12', 'status': 'submitted_unconfirmed'},
            {'id': 'email-c', 'url': 'https://career.habr.com.example.org/vacancies/13', 'status': 'sent'},
            {'id': 'email-d', 'url': 'https://hh.ru/vacancy/14', 'status': 'sent'},
        ])
        self.store.import_sources()
        command = Path(__file__).resolve().parents[1] / 'manage.py'
        result = subprocess.check_output(
            [sys.executable, str(command), 'unseen', 'habr', '13', '11', '12', '14', '13'],
            env={**os.environ, 'CAREER_DATA_DIR': str(self.store.data_dir)},
        )
        self.assertEqual(json.loads(result), ['13', '12', '14'])

    def test_новые_ссылки_смешивают_площадки_и_убирают_варианты_одного_url(self):
        geekjob_id = '6ad625cc3256e5f0bc008073'
        self.write('hh-applications.jsonl', [{'id': '10', 'status': 'sent'}])
        self.write('getmatch-applications.jsonl', [{'id': '13', 'status': 'sent'}])
        self.write('company-applications.jsonl', [
            {'id': 'direct', 'url': 'https://career.habr.com/vacancies/11?from=mail', 'status': 'sent'},
            {'id': 'geekjob', 'url': f'https://geekjob.ru/vacancy/{geekjob_id.upper()}', 'status': 'sent'},
        ])
        self.store.import_sources()
        command = Path(__file__).resolve().parents[1] / 'manage.py'
        env = {**os.environ, 'CAREER_DATA_DIR': str(self.store.data_dir)}
        args = [sys.executable, str(command), 'unseen', 'urls']
        links = [
            'https://spb.hh.ru/vacancy/10?query=Vue',
            'https://rabota.by/vacancy/10',
            'https://career.habr.com/vacancies/10',
            'https://career.habr.com/vacancies/11',
            'https://hh.ru/vacancy/12',
            'https://spb.hh.ru/vacancy/12/#response',
            'https://hh.kz/vacancy/12?from=search',
            'https://getmatch.ru/vacancies/13-frontend-developer?s=offers',
            f'https://geekjob.ru/vacancy/{geekjob_id}/?from=search',
            'https://getmatch.ru/vacancies/14-frontend-developer',
            'https://getmatch.ru/vacancies/14',
        ]
        self.assertEqual(json.loads(subprocess.check_output(args + links, env=env)), [links[2], links[4], links[9]])
        invalid = subprocess.run(args + ['file:///tmp/vacancy/12'], env=env,
                                 capture_output=True)
        self.assertEqual(invalid.returncode, 2)

    def test_новые_ссылки_проверяют_сайты_компаний_и_попытки_отправки(self):
        ashby = 'https://jobs.ashbyhq.com/acme/f3e18361-9b2d-4729-8be7-be5ad97a39f8'
        self.write('company-applications.jsonl', [
            {'id': 'career', 'url': 'https://acme.example/careers/frontend', 'status': 'sent'},
            {'id': 'ashby', 'url': ashby + '/', 'status': 'submitted_unconfirmed'},
        ])
        self.store.import_sources()
        command = Path(__file__).resolve().parents[1] / 'manage.py'
        env = {**os.environ, 'CAREER_DATA_DIR': str(self.store.data_dir)}
        links = [
            'https://acme.example/careers/frontend/',
            ashby + '/application',
            'https://acme.example/careers?job=frontend',
            'https://acme.example/careers?job=designer',
            'https://hh.ru/vacancy/99',
        ]
        result = subprocess.check_output([sys.executable, str(command), 'unseen', 'urls', *links], env=env)
        self.assertEqual(json.loads(result), links[2:])

    def test_синхронизация_сохраняет_заметки_и_не_выдает_приглашение_за_встречу(self):
        self.write('hh-applications.jsonl', [{'id': '1', 'status': 'sent', 'time': '2026-11-07T20:00:00Z'}])
        self.store.import_sources()
        self.store.update('hh:1', {'note': 'Нужна удалёнка', 'next_action': 'Ответить', 'manual_stage': 'reply'})
        self.store.apply_observations([{'id': '1', 'platform_status': 'Собеседование', 'response': 'Заполните анкету'}])
        self.store.import_sources(force=True)
        row = self.store.detail('hh:1')
        self.assertEqual(row['note'], 'Нужна удалёнка')
        self.assertEqual(row['effective_stage'], 'reply')
        self.store.update('hh:1', {'manual_stage': None})
        self.assertEqual(self.store.detail('hh:1')['effective_stage'], 'invited')
        self.assertEqual(self.store.stats()['interviews'], 0)

    def test_поврежденная_последняя_строка_не_теряет_остальные_данные(self):
        path = self.write('hh-applications.jsonl', [{'id': '1', 'status': 'sent'}])
        with path.open('a') as file:
            file.write('{"id":')
        result = self.store.import_sources()
        self.assertTrue(result['warnings'])
        self.assertEqual(self.store.stats()['confirmed'], 1)

    def test_фильтры_и_пагинация_выполняются_на_сервере(self):
        self.write('hh-applications.jsonl', [{'id': str(i), 'company': 'Команда A' if i % 2 else 'Команда Б', 'title': 'React', 'status': 'sent'} for i in range(7)])
        self.store.import_sources()
        result = self.store.jobs({'view': 'applications', 'q': 'Команда A', 'limit': '2', 'page': '2'})
        self.assertEqual(result['total'], 3)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['pages'], 2)

    def test_дневная_цель_не_меняет_фактический_счетчик(self):
        from store import now
        at = now()
        self.write('hh-applications.jsonl', [{'id': '1', 'status': 'sent', 'time': at}])
        self.store.import_sources()
        self.store.set_setting('daily_application_target', {'date': local_day(at), 'target': 550})
        stats = self.store.stats()
        self.assertEqual(stats['today'], 1)
        self.assertEqual(stats['dailyTarget'], 550)
        self.assertEqual(stats['remainingToday'], 549)
        self.store.set_setting('daily_application_target', {'date': '2000-01-01', 'target': 550})
        self.assertIsNone(self.store.stats()['dailyTarget'])

    def test_недоставленное_письмо_исключается_из_откликов_при_повторном_импорте(self):
        sent = {'id': 'mail', 'status': 'sent', 'time': '2026-11-07T21:05:00Z'}
        self.write('company-applications.jsonl', [sent, {'id': 'mail', 'status': 'delivery_failed', 'time': '2026-11-07T21:06:00Z'}])
        self.store.import_sources()
        self.store.import_sources(force=True)
        self.assertEqual(self.store.stats()['confirmed'], 0)
        self.assertEqual(self.store.stats()['new'], 0)
        self.assertEqual(self.store.detail('company:mail')['effective_stage'], 'blocked')
        self.assertEqual(len(self.store.detail('company:mail')['events']), 2)
        self.write('company-applications.jsonl', [sent])
        self.store.import_sources()
        self.assertEqual(self.store.stats()['confirmed'], 0)

    def test_успешная_повторная_отправка_после_ошибки_доставки_считается_один_раз(self):
        self.write('company-applications.jsonl', [
            {'id': 'mail', 'status': 'sent', 'time': '2026-11-07T21:05:00Z'},
            {'id': 'mail', 'status': 'delivery_failed', 'time': '2026-11-07T21:06:00Z'},
            {'id': 'mail', 'status': 'sent', 'time': '2026-11-08T08:00:00Z'},
        ])
        self.store.import_sources()
        self.store.import_sources(force=True)
        self.assertEqual(self.store.stats()['confirmed'], 1)
        self.assertEqual(self.store.detail('company:mail')['sent_at'], '2026-11-08T08:00:00Z')

    def test_обращение_рекрутеру_сохраняет_дату_без_увеличения_числа_откликов(self):
        from store import now
        at = now()
        self.write('company-applications.jsonl', [{'id': 'recruiter', 'status': 'recruiter_contacted', 'type': 'recruiter_outreach', 'time': at}])
        self.store.import_sources()
        self.store.import_sources(force=True)
        row = self.store.detail('company:recruiter')
        self.assertEqual(row['effective_stage'], 'sent')
        self.assertEqual(row['sent_at'], at)
        self.assertEqual((self.store.stats()['confirmed'], self.store.stats()['today']), (0, 0))

    def test_экспорт_экранирует_формулы_и_не_исполняет_текст(self):
        self.write('hh-applications.jsonl', [{'id': '1', 'company': '=SUM(A1)', 'status': 'sent'}])
        self.store.import_sources()
        text = self.store.export_csv({'view': 'applications'})
        self.assertIn("'=SUM(A1)", text)

    def test_вложенный_журнал_и_копия_старой_записи(self):
        row = {'id': '1', 'status': 'sent', 'time': '2026-11-07T20:00:00Z'}
        path = self.write('hh-applications.jsonl', [row])
        with self.store.connect() as db:
            db.execute("INSERT INTO jobs(key,platform,external_id,last_at,confirmed,stage) VALUES ('hh:1','hh','1',?,1,'sent')", (row['time'],))
            db.execute("INSERT INTO events VALUES ('legacy','hh:1','application',?,'sent',?,?)", (row['time'], str(path), json.dumps(row)))
        folder = self.source / 'wave' / 'results'
        folder.mkdir(parents=True)
        (folder / path.name).write_text(json.dumps(row) + '\n' + json.dumps(row | {'id': '2'}) + '\n')
        self.assertEqual(self.store.import_sources()['imported'], 1)
        self.assertEqual(self.store.stats()['confirmed'], 2)
        self.assertEqual(self.store.import_sources(force=True)['imported'], 0)
        self.assertEqual(len(self.store.detail('hh:1')['events']), 1)

    def test_hh_не_стирает_ручной_ответ_и_не_дублирует_снимок(self):
        row = {'id': '1', 'platform_status': 'Приглашение', 'response': 'Сообщение HH'}
        self.store.apply_observations([row])
        self.store.update('hh:1', {'response': 'Мой ответ', 'manual_stage': 'interview', 'follow_up': '2026-11-20'})
        self.store.apply_observations([row, row | {'platform_status': ''}])
        actual = self.store.detail('hh:1')
        self.assertEqual(actual['response'], 'Мой ответ')
        self.assertEqual(actual['effective_stage'], 'interview')
        self.assertEqual(actual['follow_up'], '2026-11-20')
        self.assertEqual(len(actual['events']), 2)

    def test_ручной_ответ_сохраняется_в_истории(self):
        self.write('hh-job-queue.json', [{'id': '1', 'title': 'Frontend'}])
        self.store.import_sources()
        self.store.update('hh:1', {'response': 'Ждём портфолио', 'manual_stage': 'reply'})
        row = self.store.detail('hh:1')
        self.assertEqual(row['effective_stage'], 'reply')
        self.assertEqual(row['events'][0]['kind'], 'manual_response')
        self.assertEqual(self.store.stats()['confirmed'], 0)


if __name__ == '__main__':
    unittest.main()
