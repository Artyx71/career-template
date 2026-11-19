#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
import zipfile
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
ALIASES = {
    'react': ['React', 'React.js', 'ReactJS'], 'nextjs': ['Next.js', 'NextJS'],
    'typescript': ['TypeScript'], 'javascript': ['JavaScript'], 'vue': ['Vue', 'Vue.js'],
    'nuxt': ['Nuxt', 'Nuxt.js'], 'electron': ['Electron'],
    'tanstack-query': ['TanStack Query', 'React Query'], 'tailwind': ['Tailwind', 'Tailwind CSS'],
    'rest': ['REST', 'REST API', 'BFF'], 'websocket': ['WebSocket', 'WebSockets'],
    'livekit': ['LiveKit'], 'git': ['Git', 'Pull Request'], 'nodejs': ['Node.js', 'NodeJS'],
    'vitest': ['Vitest'], 'playwright': ['Playwright'], 'react-hook-form': ['React Hook Form'],
    'zod': ['Zod'], 'html': ['HTML', 'HTML5'], 'css': ['CSS', 'SCSS'],
    'go': ['Golang', 'Go'], 'python': ['Python'], 'postgresql': ['PostgreSQL', 'Postgres'],
    'kafka': ['Kafka'], 'rabbitmq': ['RabbitMQ'], 'kubernetes': ['Kubernetes', 'K8s'],
    'express': ['Express', 'Express.js'], 'nestjs': ['NestJS', 'Nest.js'],
}


def normalized(value):
    return re.sub(r'\s+', ' ', value.casefold().replace('ё', 'е')).strip()


def contains(text, term):
    return bool(re.search(r'(?<!\w)' + re.escape(normalized(term)) + r'(?!\w)', normalized(text)))


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.skip += 1
        if tag in ('p', 'li', 'h1', 'h2', 'h3', 'br', 'div'): self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.skip = max(0, self.skip - 1)
        if tag in ('p', 'li', 'h1', 'h2', 'h3', 'div'): self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip: self.parts.append(data)


def extract(path):
    path = Path(path)
    if path.suffix.lower() == '.pdf':
        return subprocess.run(['pdftotext', str(path), '-'], check=True, capture_output=True, text=True).stdout
    if path.suffix.lower() == '.docx':
        with zipfile.ZipFile(path) as archive:
            tree = ElementTree.fromstring(archive.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return '\n'.join(''.join(node.itertext()) for node in tree.findall('.//w:p', ns))
    value = path.read_text()
    if path.suffix.lower() in ('.html', '.htm'):
        parser = TextHTML(); parser.feed(value); return ''.join(parser.parts)
    return value


def load_data(profile_path=None, evidence_path=None):
    profile = json.loads(Path(profile_path or ROOT / 'profile.json').read_text())
    evidence = json.loads(Path(evidence_path or ROOT / 'evidence.json').read_text())['items']
    return profile, evidence


def verified_skills(profile, evidence):
    allowed = {item['id'] for item in evidence if item.get('status') in ('user_confirmed', 'code_checked') and item.get('sources')}
    return {key: value for key, value in profile['skills'].items() if value.get('evidence') and set(value['evidence']) <= allowed}


def validate_vacancy(vacancy):
    if not isinstance(vacancy.get('text'), str) or not vacancy['text'].strip():
        raise ValueError('Нужен сохранённый текст вакансии в поле text')
    requirements = vacancy.get('requirements')
    if not isinstance(requirements, list) or not requirements:
        raise ValueError('Нужны явные requirements с цитатами; инструмент не угадывает обязательность по ключевым словам')
    ids = set()
    for req in requirements:
        if not isinstance(req.get('id'), str) or not req['id'] or req['id'] in ids:
            raise ValueError('У требований должны быть разные непустые id')
        ids.add(req['id'])
        quote = req.get('source_quote')
        if not isinstance(quote, str) or not quote.strip() or normalized(quote) not in normalized(vacancy['text']):
            raise ValueError('Цитата требования отсутствует в исходной вакансии: ' + req['id'])
        if type(req.get('required')) is not bool:
            raise ValueError('Обязательность должна быть указана явно: ' + req['id'])
        if req.get('kind') not in ('skill', 'experience', 'condition'):
            raise ValueError('Неизвестный вид требования')
        if req['kind'] in ('skill', 'experience'):
            if req.get('skill') not in ALIASES or req.get('basis', 'practice') not in ('practice', 'commercial'):
                raise ValueError('Неизвестный навык или основание опыта')
        if req['kind'] == 'experience' and (type(req.get('min_months')) is not int or req['min_months'] < 0):
            raise ValueError('Нужен неотрицательный min_months')
        if req['kind'] == 'condition' and req.get('field') not in ('remote_from_russia', 'relocation', 'mandatory_office_visits'):
            raise ValueError('Неизвестное условие сотрудничества')
        if req['kind'] == 'condition' and type(req.get('value')) is not bool:
            raise ValueError('Условие сотрудничества должно иметь логическое значение')


def audit(vacancy, resume_text, profile, evidence):
    validate_vacancy(vacancy)
    skills = verified_skills(profile, evidence)
    checks = []
    for req in vacancy['requirements']:
        status, ids = 'unknown', []
        if req['kind'] in ('skill', 'experience'):
            skill = skills.get(req['skill'])
            if skill:
                ids = skill['evidence']
                if req['kind'] == 'experience':
                    if req.get('basis', 'practice') == 'commercial':
                        months = skill.get('commercial_months')
                        status = 'unknown' if months is None else 'supported' if months >= req['min_months'] else 'gap'
                    else:
                        months = skill.get('practice_months')
                        commercial = skill.get('commercial_months')
                        if commercial is None: commercial = 0
                        status = ('supported' if commercial >= req['min_months'] else 'unknown') if months is None else 'supported' if max(months, commercial) >= req['min_months'] else 'gap'
                elif req.get('basis', 'practice') == 'commercial' and not skill.get('commercial'):
                    status = 'gap'
                else:
                    status = 'supported'
            else:
                status = 'not_verified'
            mentioned = any(contains(resume_text, term) for term in ALIASES[req['skill']])
        else:
            actual = profile['preferences'].get(req['field'])
            status = 'unknown' if actual is None else 'supported' if actual == req['value'] else 'gap'
            mentioned = None
        checks.append({**req, 'status': status, 'mentioned_in_resume': mentioned, 'evidence_ids': ids})
    warnings = []
    if len(resume_text.strip()) < 150: warnings.append('Из файла извлечено слишком мало текста: проверить, не является ли он изображением.')
    if '\ufffd' in resume_text: warnings.append('Есть символы замены: проверить кодировку и шрифты.')
    for field in ('email', 'name'):
        if profile[field] and normalized(profile[field]) not in normalized(resume_text): warnings.append('В извлечённом тексте не найдено поле: ' + field)
    additions = [{'skill': req['skill'], 'name': skills[req['skill']]['name'], 'evidence_ids': req['evidence_ids']}
                 for req in checks if req['kind'] == 'skill' and req['status'] == 'supported' and not req['mentioned_in_resume']]
    unsupported = [key for key in ALIASES if key not in skills and any(contains(resume_text, term) for term in ALIASES[key])]
    return {'notice': 'Это проверка сопоставления и извлечения текста, не результат работодателя и не вероятность прохождения ATS.',
            'company': vacancy.get('company'), 'title': vacancy.get('title'), 'source_url': vacancy.get('url'), 'snapshot_date': vacancy.get('snapshot_date'),
            'conditions_to_review': vacancy.get('conditions_to_review', []),
            'checks': checks, 'required_gaps': [q['id'] for q in checks if q['required'] and q['status'] != 'supported'],
            'safe_keyword_additions': additions, 'resume_terms_without_verified_evidence': unsupported,
            'parser_warnings': warnings, 'extracted_characters': len(resume_text),
            'manual_review_required': True, 'submission_authorized_by_report': False}


def prepare(vacancy, profile, evidence):
    validate_vacancy(vacancy)
    skills = verified_skills(profile, evidence)
    targets = list(dict.fromkeys(q['skill'] for q in vacancy['requirements'] if q['kind'] in ('skill', 'experience') and q['skill'] in skills))
    ranked = sorted(enumerate(evidence), key=lambda pair: (-sum(skill in pair[1]['skills'] for skill in targets), pair[0]))
    selected = [item for _, item in ranked if item.get('status') in ('user_confirmed', 'code_checked') and item.get('sources')]
    selected_skills = targets + [key for key in ('react','typescript','nextjs','vue','nuxt','electron','rest','tanstack-query','websocket','git') if key in skills and key not in targets]
    sections = [f'<h1>{escape(profile["name"])}</h1>', f'<p class="headline">{escape(profile["headline"])}</p>',
                '<p>' + escape(' · '.join(value for value in (profile['email'], profile['phone'], profile['telegram']) if value)) + '</p>',
                '<p>'+escape(profile['github'])+'</p>', '<p>'+escape(profile['location'])+' · Полностью удалённо, без переезда</p>',
                '<h2>Профиль</h2><p>'+escape(profile['summary'])+'</p>',
                '<h2>Навыки</h2><p>'+escape(', '.join(skills[key]['name'] for key in selected_skills))+'</p>', '<h2>Опыт работы</h2>']
    for exp in profile['experience']:
        sections.append('<h3>'+escape(exp['company']+' — '+exp['role'])+'</h3><p>'+escape(exp['dates'])+'</p><ul>')
        for item in [x for x in selected if x['company'] == exp['company']][:4]:
            sections.append('<li>'+escape(item['text'])+'</li>')
        sections.append('</ul>')
    sections.append('<h2>Личные проекты</h2><ul>')
    for item in [x for x in selected if x['kind'] == 'personal'][:3]: sections.append('<li>'+escape(item['text'])+'</li>')
    sections.extend(['</ul><h2>Образование и языки</h2><p>'+escape(profile['education'])+'</p>', '<p>'+escape(profile['languages'])+'.</p>'])
    style='@page{size:A4;margin:15mm}body{font:10pt/1.4 Arial,sans-serif;color:#111}h1{font-size:23pt;margin:0 0 5pt}h2{font-size:13pt;border-bottom:1px solid #aaa;padding-bottom:3pt;margin:14pt 0 6pt}h3{font-size:11pt;margin:10pt 0 3pt}p{margin:3pt 0}li{margin:5pt 0}ul{padding-left:17pt}h2,h3{break-after:avoid}li{break-inside:avoid}.headline{font-size:12pt}'
    html='<!doctype html><html lang="ru"><meta charset="utf-8"><title>Резюме — '+escape(profile['name'])+'</title><style>'+style+'</style>'+''.join(sections)+'</html>'
    parser=TextHTML();parser.feed(html)
    return html, ''.join(parser.parts), [x['id'] for x in selected]


def main():
    parser=argparse.ArgumentParser(description='Проверка резюме и требований по подтверждённым фактам; без имитации ATS работодателя')
    parser.add_argument('command',choices=['audit','prepare'])
    parser.add_argument('--vacancy',type=Path,required=True)
    parser.add_argument('--resume',type=Path)
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--evidence',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--pdf',action='store_true')
    args=parser.parse_args()
    try:
        profile,evidence=load_data(args.profile,args.evidence)
        vacancy=json.loads(args.vacancy.read_text())
        args.output.mkdir(parents=True,exist_ok=True)
        if args.command=='prepare':
            html,text,ids=prepare(vacancy,profile,evidence)
            (args.output/'resume.html').write_text(html)
            (args.output/'resume.txt').write_text(text)
            (args.output/'evidence-used.json').write_text(json.dumps(ids,ensure_ascii=False,indent=2))
            if args.pdf:
                subprocess.run(['node',str(ROOT/'scripts/render-resume.cjs'),str(args.output/'resume.html'),str(args.output/'resume.pdf')],check=True,capture_output=True,text=True)
                text=extract(args.output/'resume.pdf')
        else:
            if not args.resume:raise ValueError('Для audit нужен --resume')
            text=extract(args.resume)
        report=audit(vacancy,text,profile,evidence)
        (args.output/'ats-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'output':str(args.output),'required_gaps':report['required_gaps'],'parser_warnings':report['parser_warnings'],'automatic_ats_score':None},ensure_ascii=False))
    except (OSError,ValueError,KeyError,subprocess.SubprocessError,zipfile.BadZipFile) as exc:
        parser.exit(1,'Ошибка: '+str(exc)+'\n')


if __name__=='__main__':main()
