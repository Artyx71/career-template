# Вход ATS-проверки

`vacancy.json` содержит `company`, `title`, `url`, `text` (исходная публикация), `requirements` (массив). Исследователь задаёт обязательность явно и сохраняет буквальную цитату, присутствующую в text; скрипт проверяет её наличие. Цитата не доказывает сама по себе корректность интерпретации — reviewer сверяет смысл.

Виды требований:
- `skill`: `id`, `skill` (каноническое имя из ALIASES), `basis` (`practice` или `commercial`), `required` (bool), `source_quote`.
- `experience`: те же поля плюс `min_months` (целое число). Не превращать общий стаж frontend в стаж конкретной технологии.
- `condition`: `id`, `field` (`remote_from_russia`, `relocation`, `mandatory_office_visits`), `value` (bool), `required`, `source_quote`.

Суммы и договор изучаются координатором отдельно: gross/net и налоги нельзя угадывать из числа. Не делать обязательным преимущество из раздела «плюсом».

```sh
python3 career/scripts/career_ats.py audit --vacancy /absolute/vacancy.json --resume /absolute/resume.pdf --output /absolute/audit
python3 career/scripts/career_ats.py prepare --vacancy /absolute/vacancy.json --output /absolute/prepared
```

`prepare` создаёт черновик из подтверждённых фактов. До внешнего использования reviewer проверяет смысловую адаптацию, извлечённый текст и визуальный PDF. `required_gaps` — требующие разбора несовпадения, не автоматический отказ работодателя. `safe_keyword_additions` — только подтверждённые навыки, отсутствующие в документе. `resume_terms_without_verified_evidence` требует ручной проверки: название технологии может встречаться в отрицании. Нулевое число предупреждений не доказывает прохождение ATS.

## Локальные данные

Заполните `career/profile.json` и `career/evidence.json` по примерам из README. В `skills` ключ соответствует `ALIASES` в скрипте. Для каждого навыка укажите `name`, `commercial` (bool), подтверждённые `commercial_months` и `practice_months` (целое число или null) и список идентификаторов `evidence`. Неизвестные условия в `preferences` оставьте null.

Каждое подтверждение в `items` содержит `id`, `company`, `kind` (`personal` для личного проекта), `skills` (список ключей), `text`, `status` (`user_confirmed` или `code_checked`) и непустой список `sources`. Для подготовки резюме запишите в `experience` компании, роли и даты как объекты с полями `company`, `role`, `dates`.

Для PDF из готового черновика можно добавить `--pdf` к `prepare`. Понадобятся Node.js, локальный Playwright в корне проекта (`npm ci`), Chromium из Playwright (`npx playwright install chromium`) или путь к системному Chromium в `CAREER_CHROMIUM`, а также `pdftotext` для проверки текста.
