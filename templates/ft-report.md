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

## 2. Оценка полноты ФТ

<% if r.ft_completeness %>| Аспект | Статус | Комментарий |
|---|---|---|
<% for a in r.ft_completeness %>| << a.aspect >> | << a.status >> | << a.comment >> |
<% endfor %><% else %>_Оценка не сформирована._
<% endif %>

## 3. Маппинг на конфигурацию

<% if r.mapping %>| Требование | Объекты конфигурации | Примечания |
|---|---|---|
<% for m in r.mapping %>| << m.requirement >> | << m.objects >> | << m.notes >> |
<% endfor %><% else %>_Маппинг не сформирован._
<% endif %>

## 4. Драфт плана реализации

<% if r.implementation_plan %><% for step in r.implementation_plan %>1. << step >>
<% endfor %><% else %>_План не сформирован._
<% endif %>

## 5. Риски и влияние

<% if r.risks %><% for risk in r.risks %>- << risk >>
<% endfor %><% else %>_Риски не выявлены._
<% endif %>

## 6. Вопросы к постановщику

<% if r.missing_info %><% for q in r.missing_info %>- [ ] << q >>
<% endfor %><% else %>_Вопросов нет._
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
