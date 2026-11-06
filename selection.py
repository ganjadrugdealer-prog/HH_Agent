"""Проверка соответствия вакансий и просмотр только через GET."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from pathlib import Path

POLICIES = {
    'automation': ('Автоматизация', '"менеджер проектов" OR "внедрение" OR "автоматизация процессов"'),
    'media': ('Медиапроекты', '"руководитель медиапроектов" OR "продюсер" OR "руководитель производства контента"'),
    'photography': ('Фотография', '"фотограф" OR "фотокорреспондент" OR "фотосопровождение"'),
}


def search_params(policy):
    params = {'text': POLICIES[policy][1], 'per_page': 30, 'page': 0}
    if policy != 'photography':
        params.update(work_format=['REMOTE'], salary=100000, currency='RUR')
    return params


def evaluate(v, policy, history=(), excluded_pattern=None):
    reasons, review = [], []
    title = str(v.get('name') or '')
    description = re.sub('<[^>]*>', ' ', html.unescape(str(v.get('description') or '')))
    text = (title + ' ' + description).lower()
    duties = description.lower()
    section = re.search(r'(?:обязанности|задачи|вам предстоит|чем предстоит заниматься)\s*:?\s*(.*?)(?:требования|условия|мы предлагаем|мы ожидаем|$)', duties, re.S)
    if section:
        duties = section.group(1)
    has = lambda pattern, value=text: bool(re.search(pattern, value))
    salary = v.get('salary') or v.get('salary_range') or {}
    formats = [x.get('id') for x in (v.get('work_format') or []) if isinstance(x, dict)]
    if v.get('relations') or str(v.get('id')) in history:
        reasons.append('Уже есть отклик, отношение или незавершённая попытка; повтор запрещён')
    if v.get('archived'):
        reasons.append('Вакансия в архиве')
    if v.get('has_test'):
        reasons.append('Вакансия требует теста: автоматическая отправка отключена')
    if v.get('response_url'):
        reasons.append('Отклик через внешний сайт')
    if excluded_pattern and re.search(excluded_pattern, title, re.I):
        reasons.append('Совпадение с настроенным фильтром названия')
    management = has(r'(?:управлен\w*|руковод\w*|координац\w*|организац\w*)\s+(?:проект|команд|подрядчик|процесс|производств|выпуск)', duties)
    photo_title = has(r'фотограф|фотокорреспондент|видеограф|ретуш|видеооператор', title.lower())
    lead_title = has(r'руковод|продюсер|директор|менеджер проектов', title.lower())
    if policy not in POLICIES:
        reasons.append('Направление резюме не задано')
    elif policy == 'photography':
        if has(r'начинающ|без опыта|стаж[её]р|junior', title.lower()):
            reasons.append('Начальная позиция не соответствует опыту')
        if not has(r'фотограф|фотокорреспондент|фотосопровожд', title.lower()):
            review.append('Название не подтверждает фотографическую роль')
        if not has(r'репортаж|корпоратив|мероприяти|фотосопровожд|фотокорреспондент', duties):
            review.append('Не подтверждены репортажные или корпоративные обязанности')
    else:
        title_domain = r'проект|автоматизац|внедрен|бизнес.процесс|project' if policy == 'automation' else r'медиа|контент|производств|продюсер|producer|видео'
        if not has(title_domain, title.lower()):
            review.append('Название не подтверждает специализацию выбранного направления')
        if not has(r'менеджер\s+(?:проект|по\s+(?:автоматизац|внедрен))|руковод|продюсер|директор|координатор|project|producer', title.lower()):
            review.append('Название не подтверждает управленческую профессию')
        roles = ' '.join(str(r.get('name') or '') for r in v.get('professional_roles', [])).lower()
        if has(r'программист|фотограф|продавец', roles):
            review.append('Профессиональная категория противоречит менеджерскому направлению')
        if has(r'разработчик|программист|инженер', title.lower()) and not lead_title:
            reasons.append('Самостоятельная инженерная роль не подтверждена опытом')
        if photo_title and not lead_title:
            reasons.append('Исполнительская фотографическая/видеороль не соответствует менеджерскому резюме')
        if not management:
            review.append('Не подтверждены управление, координация или внедрение в обязанностях')
        domain = r'автоматизац|внедрен\w*.{0,60}(ии|ai|инструмент|систем)|бизнес.процесс' if policy == 'automation' else r'контент|медиа|продакш|съ[её]м|производств\w*.{0,35}(видео|материал|выпуск)'
        if not has(domain, duties):
            review.append('Обязанности не подтверждают выбранное направление')
        if 'REMOTE' not in formats:
            (reasons if formats else review).append('Удалённый формат не подтверждён')
        elif len(formats) > 1:
            review.append('Несколько форматов: требуется подтверждение постоянной удалёнки')
        lower = salary.get('from')
        if not isinstance(lower, (int, float)) or isinstance(lower, bool) or lower <= 0:
            review.append('Неизвестна гарантированная нижняя граница оплаты')
        elif lower < 100000:
            reasons.append('Нижняя граница оплаты меньше 100000 ₽')
        if salary.get('gross') is not False:
            review.append('Не подтверждена оплата на руки')
        if salary.get('currency') != 'RUR':
            review.append('Не подтверждена оплата в рублях')
        period = (v.get('salary_range') or {}).get('mode') or salary.get('mode') or salary.get('period')
        if period and (period.get('id') if isinstance(period, dict) else period) not in ('MONTH', 'MONTHLY', 'month'):
            review.append('Не подтверждена месячная оплата')
    if not description.strip():
        review.append('Нет полного описания обязанностей')
    decision = 'excluded' if reasons else 'review' if review else 'eligible'
    return {'vacancy_id': str(v.get('id') or ''), 'name': title,
            'url': v.get('alternate_url') or '', 'policy': policy,
            'decision': decision, 'reasons': reasons + review or ['Обязанности и условия соответствуют направлению'],
            'work_format': v.get('work_format'), 'salary': salary,
            'duties': description, 'professional_roles': v.get('professional_roles')}


def sent_history(directory):
    """Читаем старую базу; не открываем соединение с правом записи."""
    root = Path(directory)
    result = set(json.loads((root / 'selection-sent.json').read_text(encoding='utf-8'))) if (root / 'selection-sent.json').exists() else set()
    if (root / 'selection-pending.json').exists():
        result.update(json.loads((root / 'selection-pending.json').read_text(encoding='utf-8')))
    for path in (root / 'reports').glob('*.json'):
        report = json.loads(path.read_text(encoding='utf-8'))
        for event in report.get('events', []):
            if str(event.get('kind') or '').startswith('applied'):
                match = re.search(r'/vacancy/(\d+)', str(event.get('url') or ''))
                if match:
                    result.add(match.group(1))
    db = root / 'data'
    if db.is_file():
        with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            columns = {r[1] for r in conn.execute('PRAGMA table_info(negotiations)')}
            if 'vacancy_id' in columns:
                result.update(str(r[0]) for r in conn.execute('SELECT vacancy_id FROM negotiations'))
    return result


def candidates(client, resume_id, policy, history, limit=30, options=None, excluded_pattern=None):
    params = search_params(policy)
    params.update({k: v for k, v in (options or {}).items() if k in ('area', 'experience', 'period', 'order_by') and v})
    params['per_page'] = min(100, limit, (options or {}).get('per_page') or 100)
    count = 0
    for page in range((options or {}).get('total_pages') or 1):
        params['page'] = page
        data = client.get('/vacancies', params)
        for item in data.get('items', []):
            full = client.get('/vacancies/' + str(item['id']))
            row = evaluate(full, policy, history, excluded_pattern)
            row.update(resume_id=resume_id, source='/vacancies', query=dict(params))
            yield full, row
            count += 1
            if count >= limit:
                return
        if not data.get('items') or page + 1 >= data.get('pages', 1):
            return


def write_report(path, rows):
    """Локальный отчёт с экранированием всех внешних полей."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix('.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    headers = ['Вакансия', 'Резюме / направление', 'Решение и причины', 'Формат', 'Оплата', 'Обязанности / профессия']
    body = ''
    for r in rows:
        values = [r['name'], r['resume_id'] + ' / ' + r['policy'], r['decision'] + ': ' + '; '.join(r['reasons']),
                  json.dumps(r['work_format'], ensure_ascii=False), json.dumps(r['salary'], ensure_ascii=False),
                  r['duties'] + '\n' + json.dumps(r['professional_roles'], ensure_ascii=False)]
        body += '<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in values) + '</tr>'
    path.write_text('<!doctype html><meta charset="utf-8"><title>Проверка подбора</title><style>body{font:15px system-ui}td,th{padding:12px;border:1px solid #ccc;vertical-align:top}table{border-collapse:collapse}</style><h1>Подбор без отправки</h1><p>eligible — подходит; excluded — исключено; review — ручное рассмотрение.</p><table><tr>' + ''.join('<th>' + h + '</th>' for h in headers) + '</tr>' + body + '</table>', encoding='utf-8')
