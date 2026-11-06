"""Регрессии подбора. Только подставной клиент, без отправки и живой базы."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selection
from hh_patch import selection_guard


def vacancy(**changes):
    v = {'id': '1', 'name': 'Менеджер проектов автоматизации',
         'description': 'Управление проектами автоматизации бизнес-процессов, координация команды.',
         'work_format': [{'id': 'REMOTE'}],
         'salary': {'from': 100000, 'to': 150000, 'gross': False, 'currency': 'RUR'}}
    v.update(changes)
    return v


class SelectionTests(unittest.TestCase):
    def test_company_text_and_core_skips(self):
        wrong = vacancy(name='Менеджер по продажам', description='Наша организация занимается автоматизацией. Обязанности: продажи. Условия: удалённо.')
        self.assertNotEqual(selection.evaluate(wrong, 'automation')['decision'], 'eligible')
        for changes in ({'has_test': True}, {'response_url': 'https://example.com'}, {'professional_roles': [{'name': 'Программист'}]}):
            self.assertNotEqual(selection.evaluate(vacancy(**changes), 'automation')['decision'], 'eligible')
        self.assertEqual(selection.evaluate(vacancy(), 'automation', excluded_pattern='менеджер')['decision'], 'excluded')

    def test_read_only_blocks_token_refresh(self):
        from hh_patch import selection_read_only
        from hh_applicant_tool.api.client import BaseClient
        client = BaseClient(base_url='https://api.hh.ru/')
        original = BaseClient.request
        with selection_read_only():
            with self.assertRaises(RuntimeError):
                client.post('/token')
        self.assertIs(BaseClient.request, original)

    def test_matrix_30(self):
        base = vacancy()
        cases = [(base, 'automation', 'eligible')]
        for name in ('Фотограф', 'Видеограф', 'Ретушёр', 'Видеооператор', 'Начинающий фотограф'):
            cases.append((vacancy(name=name), 'automation', 'excluded'))
        cases += [(vacancy(name='Разработчик автоматизации'), 'automation', 'excluded'),
                  (vacancy(description='Разработка на Python'), 'automation', 'review'),
                  (vacancy(name='Руководитель медиапроектов', description='Управление командой выпуска контента'), 'media', 'eligible'),
                  (vacancy(description='Управление командой выпуска контента'), 'automation', 'review'),
                  (vacancy(name='Руководитель фотопроизводства', description='Управление командой фотографов и выпуском контента'), 'media', 'eligible'),
                  (vacancy(name='Фотограф', description='Корпоративная и репортажная съёмка', salary=None, work_format=[{'id': 'ON_SITE'}]), 'photography', 'eligible'),
                  (vacancy(name='Начинающий фотограф', description='Корпоративная съёмка'), 'photography', 'excluded'),
                  (vacancy(name='Фотограф', description='Предметная съёмка'), 'photography', 'review'),
                  (vacancy(work_format=[]), 'automation', 'review'),
                  (vacancy(work_format=[{'id': 'ON_SITE'}]), 'automation', 'excluded'),
                  (vacancy(work_format=[{'id': 'HYBRID'}]), 'automation', 'excluded'),
                  (vacancy(work_format=[{'id': 'REMOTE'}, {'id': 'ON_SITE'}]), 'automation', 'review'),
                  (vacancy(description=''), 'automation', 'review'),
                  (vacancy(archived=True), 'automation', 'excluded'),
                  (vacancy(relations=['applied']), 'automation', 'excluded')]
        for changes, expected in [({'from': None}, 'review'), ({'from': 80000, 'to': 200000}, 'excluded'),
                                  ({'gross': True}, 'review'), ({'gross': None}, 'review'),
                                  ({'currency': 'USD'}, 'review'), ({'period': 'DAY'}, 'review'),
                                  ({'from': 0}, 'review'), ({'from': 150000}, 'eligible')]:
            s = copy.deepcopy(base['salary']); s.update(changes)
            cases.append((vacancy(salary=s), 'automation', expected))
        cases += [(vacancy(salary=None), 'automation', 'review'), (base, 'unknown', 'excluded')]
        self.assertEqual(len(cases), 31)
        for v, policy, expected in cases:
            with self.subTest(name=v['name'], policy=policy, salary=v.get('salary')):
                result = selection.evaluate(v, policy)
                self.assertEqual(result['decision'], expected)
                self.assertTrue(result['reasons'])
        self.assertEqual(selection.evaluate(base, 'automation', {'1'})['decision'], 'excluded')

    def test_preview_only_get_and_preserves_files(self):
        calls = []
        class Client:
            def get(self, endpoint, params=None):
                calls.append((endpoint, params))
                return {'items': [{'id': '1'}]} if endpoint == '/vacancies' else vacancy()
            def __getattr__(self, name):
                raise AssertionError('Запись запрещена: ' + name)
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / 'selection-sent.json'; old.write_text('["old"]')
            history = selection.sent_history(d)
            rows = [row for _, row in selection.candidates(Client(), 'manager', 'automation', history)]
            selection.write_report(Path(d) / 'preview.html', rows)
            self.assertEqual(old.read_text(), '["old"]')
            self.assertEqual(rows[0]['resume_id'], 'manager')
            self.assertEqual(calls[0][1]['work_format'], ['REMOTE'])
            self.assertEqual(calls[0][1]['salary'], 100000)
            self.assertNotIn('salary', selection.search_params('photography'))
            self.assertTrue((Path(d) / 'preview.json').exists())
            reports = Path(d) / 'reports'; reports.mkdir()
            (reports / 'old.json').write_text('{"events":[{"kind":"applied_captcha","url":"https://hh.ru/vacancy/123"},{"kind":"would_apply","url":"https://hh.ru/vacancy/456"}]}')
            self.assertIn('123', selection.sent_history(d))
            self.assertNotIn('456', selection.sent_history(d))

    def test_send_guard_rechecks_policy_resume_and_history(self):
        class Client:
            def __init__(self): self.sent = []
            def request(self, method, endpoint, params=None, *a, **kw):
                if method == 'GET':
                    return vacancy() if endpoint != '/vacancies' else {'items': [{'id': '1'}]}
                self.sent.append((method, endpoint, params)); return {}
            def get(self, endpoint, params=None): return self.request('GET', endpoint, params)
        client = Client()
        from hh_applicant_tool.operations.apply_vacancies import Operation
        original = Operation._get_vacancies
        with tempfile.TemporaryDirectory() as d:
            rows = []
            with selection_guard(SimpleNamespace(api_client=client), {'manager': 'automation'}, d, rows):
                self.assertEqual(len(list(Operation()._get_vacancies('manager'))), 1)
                with self.assertRaises(RuntimeError):
                    client.request('POST', '/negotiations', {'resume_id': 'photo', 'vacancy_id': '1'})
                with self.assertRaises(RuntimeError): client.request('PUT', '/vacancies/blacklisted/1')
                client.request('POST', '/negotiations', {'resume_id': 'manager', 'vacancy_id': '1'})
                with self.assertRaises(RuntimeError):
                    client.request('POST', '/negotiations', {'resume_id': 'manager', 'vacancy_id': '1'})
                self.assertEqual(len(client.sent), 1)
            self.assertIs(Operation._get_vacancies, original)
            self.assertNotIn('request', client.__dict__)
            self.assertIn('1', selection.sent_history(d))

    def test_lost_post_response_stays_blocked(self):
        class Client:
            def __init__(self): self.posts = 0
            def get(self, endpoint): return vacancy()
            def request(self, method, endpoint, params=None):
                self.posts += 1
                raise TimeoutError('Ответ потерян после принятия запроса')
        client = Client()
        payload = {'resume_id': 'manager', 'vacancy_id': '1'}
        with tempfile.TemporaryDirectory() as d:
            with selection_guard(SimpleNamespace(api_client=client), {'manager': 'automation'}, d, []):
                with self.assertRaises(TimeoutError): client.request('POST', '/negotiations', payload)
            with selection_guard(SimpleNamespace(api_client=client), {'manager': 'automation'}, d, []):
                with self.assertRaises(RuntimeError): client.request('POST', '/negotiations', payload)
            self.assertEqual(client.posts, 1)


if __name__ == '__main__':
    unittest.main()
