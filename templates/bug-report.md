> << disclaimer >>
>
<% if verdict %>
> **Вердикт доверия (авто): << verdict.level >> — << verdict.score >>/100**
>
<% endif %>
> Родительская задача: **<< parent_key >>** | Дата анализа: << date >> | Модели: << models >> | Ревизия конфигурации: << dump_rev >>

## 1. Резюме

<< r.summary >>

**Оценка сложности: << 'Простая' if r.complexity == 'simple' else 'Сложная' >>** — << r.complexity_reason >>

## 2. Воспроизведение

<< r.reproduction or '_Не установлено._' >>

## 3. Вероятные причины

<% if r.hypotheses %><% for h in r.hypotheses %>- **<< h.cause >>** (уверенность: << h.confidence >>)<% if h.basis %> — << h.basis >><% endif %>
<% endfor %><% else %>_Гипотезы не сформированы._
<% endif %>

## 4. Затронутые объекты конфигурации

<% if r.affected_objects %>| Объект метаданных | Модуль | Процедура / функция | Роль в проблеме |
|---|---|---|---|
<% for o in r.affected_objects %>| << o.object >> | << o.module >> | << o.procedure >> | << o.role >> |
<% endfor %><% else %>_Не установлены._
<% endif %>

## 5. << 'Драфт решения' if r.complexity == 'simple' else 'Рекомендуемые шаги диагностики' >>

<< r.draft_solution or '_Не сформирован._' >>

## 6. Недостающая информация / вопросы к постановщику

<% if r.missing_info %><% for q in r.missing_info %>- [ ] << q >>
<% endfor %><% else %>_Информации достаточно._
<% endif %>

## 7. Источники анализа

- **Скриншоты:** << sources.images_note >>
- **Вики:** << sources.wiki_note >>
- **Связанные задачи:** << sources.nav_note >>
<% if r.code_refs %>- **Код:**
<% for c in r.code_refs %>  - << c >>
<% endfor %><% else %>- **Код:** << sources.code_note >>
<% endif %>
<% if r.notes %>
## Примечания

<< r.notes >>
<% endif %>
<% if verdict and verdict.reasons %>
## Оценка доверия (как считалась)

**<< verdict.level >> — << verdict.score >>/100.** Факторы:
<% for reason in verdict.reasons %>- << reason >>
<% endfor %>
<% endif %>
<% if stats %>
## Метрики анализа (авто)

- **Токены:** analyst << stats.analyst_in >>→<< stats.analyst_out >> (из них кеш << stats.analyst_cached >>; обращений: << stats.analyst_calls >>); vision << stats.vision_in >>→<< stats.vision_out >> (кеш << stats.vision_cached >>; << stats.vision_calls >>); **ИТОГО** << stats.total_in >>→<< stats.total_out >> (кеш << stats.total_cached >>)
<% if stats.total_cost is not none %>- **Стоимость (₽, вкл. НДС; с учётом кеша и токенов инструментов):** analyst ≈ << stats.analyst_cost if stats.analyst_cost is not none else 'н/д' >>; vision ≈ << stats.vision_cost if stats.vision_cost is not none else 'н/д' >>; **ИТОГО ≈ << stats.total_cost >> ₽**
<% endif %>- **Обращений к инструментам (код/навигация):** << stats.tool_steps >>
- **Время анализа:** << stats.duration_s >> с
<% endif %>
