# Workflow v0.7 — независимые модули и Codex App

**Workflow** — CLI/backend, управляющий специализированными агентами через отдельные именованные чаты Codex App. Собственный desktop-интерфейс не требуется.

В отличие от v0.6, здесь **нет DAG-планировщика, Stage Loop, Block Runner и Block Coordinator**. Главный AI Orchestrator согласует задачу, затем предлагает минимальный **упорядоченный список**. Детерминированный Module Controller исполняет **один независимый модуль за раз** и после каждого требует явное согласование. Между чатами передаются утверждённые артефакты, не полные диалоги.

## Установка (Python 3.11+)

```bash
python -m pip install -e .
workflow --project . doctor
workflow --project . codex-doctor
workflow --project . codex-install-skill --scope project
```

Для использования Codex-режима нужен установленный и авторизованный Codex CLI. В реальном Codex App проверь подключение самостоятельно. Skill установлен в `.agents/skills/workflow-ideas/SKILL.md`. При желании можно вызывать `$workflow-ideas` явно.

## Идея → Orchestrator

Создай файл `ideas/my-idea.md` обычным Markdown и напиши в Codex App:

> Передай оркестратору идею из `ideas/my-idea.md`. Сначала уточни и согласуй понимание. Потом предложи маршрут; ничего не выполняй до согласования.

Из терминала для проверки того же сценария:

```bash
workflow --project . new --provider codex --idea ideas/my-idea.md
workflow --project . next <RUN_ID>
workflow --project . codex-status <RUN_ID>
```

1. Если нужны уточнения — `answer <RUN_ID> --text "<ответ>"`, затем `next`.
2. Утверждение точного понимания — `approve <RUN_ID> --gate <TOKEN>`.
3. `next` формирует **динамический** список модулей; изменение до утверждения — `route-feedback <RUN_ID> --text "..."` и `next`.
4. Утверждение маршрута — ещё один `approve --gate <NEW_TOKEN>`.
5. `next` выполняет только **один модуль**, затем `review <RUN_ID>` и ещё одно согласование `approve --gate <LATEST_TOKEN>`. Следующий модуль не стартует автоматически.
6. Для адресной доработки `revise <RUN_ID> --agent ux-designer --feedback "Переработать только форму входа"`; после неё `next` и новое согласование.
7. Финал только после Documentation и финального согласования.

Для явного изменения маршрута после принятия части модулей:

```bash
workflow --project . replan <RUN_ID> --file examples/complex-system.json --reason "Нужен дополнительный дизайн"
workflow --project . status <RUN_ID>   # посмотри, что сохраняется и отменяется
workflow --project . approve <RUN_ID> --gate <NEW_ROUTE_CHANGE_TOKEN>
```

**Изменение маршрута не откатывает файлы в Git.** Проверяй последствия прежде чем соглашаться.

## Безопасные разрешения

```bash
workflow --project . next <RUN_ID>                 # только чтение
workflow --project . next <RUN_ID> --allow-writes  # после отдельного согласия на запись
workflow --project . next <RUN_ID> --allow-shell   # после отдельного согласия на команды
```

`--allow-shell` в Codex App в первую очередь является инструкцией, а не самостоятельным системным запретом: реальные sandbox-policy задаёт Codex Bridge. На OpenAI-провайдере shell работает через отдельные ограниченные инструменты. Непроверенный код не следует запускать без отдельной VM/контейнера.

При неизвестном статусе Codex-хода проверь `codex-chats <RUN_ID>` и Git diff; только затем используй `codex-clear-pending ... --confirm-inspected`. После прерывания локального процесса: `recover <RUN_ID> --confirm-inspected` после проверки владельца и файлов. Эти действия нельзя автоматизировать вслепую.

## Модули

Реестр контрактов расположен в `workflow_engine/modules/`. Модули по умолчанию доступны, но не обязаны запускаться все:

| Модуль | Агенты | Результат |
|---|---|---|
| `discovery` | Researcher + UX Designer | Исследование и UX |
| `evals` | Evals | Критерии приёмки |
| `planning` | Plan | План реализации |
| `system_design` | Architect | System Design `light/full` |
| `tasks` | Task | Декомпозиция |
| `development` | Developer | Код |
| `debugging` | Debugger | Локальное исправление |
| `quality` | Code Reviewer + Tester + Security | Проверки |
| `documentation` | Documentation | Обязательный финальный аудит |

Понимание и маршрутизация — отдельные изолированные действия Orchestrator, а не дополнительные постоянно работающие агенты.

Каждый модуль получает `inputs`: ID **только ранее утверждённых модулей**. Это ссылки на Markdown-handoff, не граф исполнения и не наследование чатов. Контракт отдельного модуля можно менять независимо от остальных. Каталог `agents/*.md` — один источник промптов; копии для wheel генерируются автоматически при сборке.

Артефакты: `.workflow/runs/<RUN_ID>/modules/<MODULE_ID>/handoff.md`, `result.json`, `diagram.mmd` и для System Design — `system-design.md`.

## Тесты

```bash
python -m unittest discover -s tests -v
```

`demo` используется только для проверки контрольных точек и помечает результаты как **не реальную работу**:

```bash
workflow --project . new --provider demo --task "Смоделируй разработку приложения"
```

Подробнее: [схемы](docs/WORKFLOW-MODULES.md), [Codex Bridge](docs/CODEX-INTEGRATION.md), [безопасность](docs/SECURITY.md), [миграция](docs/MIGRATION.md).

**Ограничения:** живое взаимодействие с твоей установкой Codex App, синхронизация внешне созданных чатов с UI и реальный OpenAI API пока не проверены. Системный sandbox из командного режима не является заменой полноценной VM для недоверенного кода.
