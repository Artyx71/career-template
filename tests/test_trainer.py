import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trainer import Trainer


class TrainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        bank_path = self.source / 'interview-prep' / 'question-bank.json'
        bank_path.parent.mkdir(parents=True)
        questions = [{'id': ident, 'category': category, 'priority': 'P0',
                      'question': 'Вопрос', 'answer_outline': 'Разбор', 'follow_up': 'Уточнение', 'sources': ['test']}
                     for ident, category in [('JS01', 'JS'), ('RE01', 'React'), ('JS02', 'JS')]]
        bank_path.write_text(json.dumps({'questions': questions, 'first_30': ['RE01', 'JS02'],
                                         'sources': {'test': ['Источник', 'https://react.dev/']}}))
        self.time = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        self.trainer = Trainer(self.root / 'data', self.source, lambda: self.time)

    def save(self, ident='RE01', answer='Мой ответ', revision=0, score=2):
        return self.trainer.save(dict(id=ident, answer=answer, revision=revision, score=score), grade=True)

    def test_без_личной_базы_загружаются_стартовые_вопросы(self):
        trainer = Trainer(self.root / 'starter-data', self.root / 'empty-source')
        state = trainer.state()
        self.assertEqual(state['stats']['total'], 3)
        self.assertEqual(state['question']['id'], 'DEMO-REACT-STATE')
        self.assertEqual(state['stats']['reviewed'], 0)

    def test_сначала_приоритетные_вопросы_а_не_алфавит(self):
        self.assertEqual(self.trainer.state()['question']['id'], 'RE01')
        self.assertEqual(self.trainer.state()['stats']['total'], 3)

    def test_черновик_сохраняется_после_перезапуска_без_ложной_оценки(self):
        self.trainer.save(dict(id='RE01', answer='Незавершённый ответ', revision=0))
        other = Trainer(self.root / 'data', self.source, lambda: self.time)
        state = other.state()
        self.assertEqual(state['question']['answer'], 'Незавершённый ответ')
        self.assertEqual(state['stats']['reviewed'], 0)

    def test_слабый_вопрос_возвращается_через_десять_минут_перед_новыми(self):
        self.save(score=0)
        self.assertEqual(self.trainer.state()['question']['id'], 'JS02')
        self.assertIsNone(self.trainer.state(mode='review')['question'])
        self.time += timedelta(minutes=10)
        self.assertEqual(self.trainer.state()['question']['id'], 'RE01')
        self.assertTrue(self.trainer.state()['question']['isReview'])

    def test_повторная_оценка_той_же_версии_не_увеличивает_прогресс(self):
        self.save()
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(self.trainer.state()['stats']['reviewed'], 1)
        self.assertEqual(self.trainer.state()['stats']['confident'], 1)

    def test_старый_черновик_не_затирает_уже_оценённый_ответ(self):
        self.save(answer='Окончательный ответ')
        with self.assertRaises(ValueError):
            self.trainer.save(dict(id='RE01', answer='Устаревший черновик', revision=0))
        self.time += timedelta(days=3)
        self.assertEqual(self.trainer.state()['question']['answer'], 'Окончательный ответ')

    def test_фильтр_темы_и_режим_повторения_выполняются_сервером(self):
        self.save(score=1)
        self.time += timedelta(days=1)
        self.assertEqual(self.trainer.state(category='JS')['stats']['reviewed'], 0)
        self.assertIsNone(self.trainer.state(category='JS', mode='review')['question'])
        self.assertEqual(self.trainer.state(category='React', mode='review')['question']['id'], 'RE01')

    def test_пустая_очередь_показывает_дату_следующего_повторения(self):
        self.save(score=3)
        state = self.trainer.state(category='React')
        self.assertIsNone(state['question'])
        self.assertEqual(state['nextReview'], (self.time + timedelta(days=7)).isoformat())

    def test_некорректные_оценки_и_идентификаторы_не_записываются(self):
        for score in [-1, 4, True, '2', None]:
            with self.subTest(score=score), self.assertRaises(ValueError):
                self.save(score=score)
        with self.assertRaises(ValueError):
            self.save(ident='нет-в-базе')
        with self.assertRaises(ValueError):
            self.save(answer='а' * 15001)
        self.assertEqual(self.trainer.state()['stats']['reviewed'], 0)

    def test_неизвестный_фильтр_не_подменяется_молча(self):
        with self.assertRaises(ValueError):
            self.trainer.state(category='Неизвестная тема')
        with self.assertRaises(ValueError):
            self.trainer.state(mode='ошибка')


if __name__ == '__main__':
    unittest.main()
