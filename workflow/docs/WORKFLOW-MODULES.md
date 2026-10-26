# Mermaid — Workflow v0.7 (Independent Modules)

## 1. Общая схема: три слоя

```mermaid
flowchart TB
  U["ТЫ: задача или ideas/idea.md"]
  O["MAIN ORCHESTRATOR · AI<br/>Понимание, маршрут, отчёты"]
  C["MODULE CONTROLLER · КОД<br/>Согласования, статусы, checkpoints"]
  B["CODEX BRIDGE · ТРАНСПОРТ<br/>Отдельные именованные чаты"]
  M["НЕЗАВИСИМЫЙ МОДУЛЬ · AI<br/>Только нужные для задания роли"]
  H["HANDOFF · ФАЙЛЫ<br/>Краткий результат, Mermaid, доказательства"]
  DB[("SQLite · утверждения и история")]
  U <--> O
  O <--> C
  C <--> DB
  C --> B --> M --> H --> C
```

## 2. Полная сложная задача: слева тип модуля, справа согласование

Это **пример максимальной последовательности**, а не обязательный конвейер. Маршрут подбирает Orchestrator заново для каждого задания. Детерминированный Controller выполняет один выбранный модуль после утверждения результата предыдущего.

```mermaid
flowchart TB
  START["ТЫ: сложная задача из ideas/"]
  UNDER["M0 · AI Orchestrator: уточнение требований"]
  ROUTE["M1 · AI Orchestrator: индивидуальный маршрут"]
  DISC["M2 · AI Researcher + UX: исследование и сценарии"]
  EVAL["M3 · AI Evals: критерии"]
  PLAN["M4 · AI Plan: план"]
  ARCH["M5 · AI Architect: System Design"]
  TASK["M6 · AI Task: декомпозиция"]
  DEV["M7 · AI Developer: реализация"]
  QUALITY["M8 · AI Reviewer + Tester + Security"]
  DOC["M9 · AI Documentation: Git + документы"]
  END["WORKFLOW ЗАВЕРШЁН"]
  START --> UNDER --> ROUTE --> DISC --> EVAL --> PLAN --> ARCH --> TASK --> DEV --> QUALITY --> DOC --> END
  UNDER -.-> U0["ТЫ: подтверждаешь точное понимание"]
  ROUTE -.-> U1["ТЫ: утверждаешь список модулей"]
  DISC -.-> U2["ТЫ: принимаешь исследование"]
  EVAL -.-> U3["ТЫ: утверждаешь критерии"]
  PLAN -.-> U4["ТЫ: утверждаешь план"]
  ARCH -.-> U5["ТЫ: утверждаешь System Design"]
  TASK -.-> U6["ТЫ: утверждаешь задачи"]
  DEV -.-> U7["ТЫ: принимаешь реализацию"]
  QUALITY -.-> U8["ТЫ: принимаешь проверки"]
  DOC -.-> U9["ТЫ: финальная приёмка"]
  classDef user fill:#fff1cd,stroke:#c28d28,color:#543e12
  class U0,U1,U2,U3,U4,U5,U6,U7,U8,U9 user
```

Сплошные переходы вниз разрешаются ТОЛЬКО после утверждения соответствующего пункта справа.

## 3. Динамический выбор модулей (не DAG)

```mermaid
flowchart TB
  APPROVED["Утверждённое понимание задания"]
  ANALYZE["Orchestrator · AI: какие результаты нужны?"]
  CHOOSE["Orchestrator · AI: выбери только необходимые модули"]
  ORDER["Orchestrator · AI: порядок и нужные входные handoff"]
  VALID["Controller · КОД: проверить роли, ID и порядок входов"]
  SHOW["Отчёт: список модулей + Mermaid + ограничения"]
  FIRST["Controller: начать только первый модуль"]
  APPROVED --> ANALYZE --> CHOOSE --> ORDER --> VALID --> SHOW
  VALID -->|"Неверно"| CHOOSE
  SHOW -.-> USER{"ТЫ: маршрут согласован?"}
  USER -->|"Изменить"| CHOOSE
  USER -->|"Да"| FIRST
  classDef user fill:#fff1cd,stroke:#c28d28,color:#543e12
  class USER user
```

## 4. Универсальный независимый модуль

```mermaid
flowchart TB
  IN["Утверждённые входные материалы"]
  CONTROLLER["Controller · КОД: один модуль"]
  CHAT["Codex Chat · AI: специализированные агенты"]
  RESULT["result.json + handoff.md + Mermaid"]
  CHECK["Controller · КОД: валидировать статус и обязательные поля"]
  REVIEW["Orchestrator · AI: сводка и решения"]
  SAVE["Controller · КОД: checkpoint + решения"]
  NEXT["Следующий утверждённый модуль"]
  FIX["Codex: доработать только выбранную роль"]
  IN --> CONTROLLER --> CHAT --> RESULT --> CHECK --> REVIEW
  REVIEW -.-> USER{"ТЫ: принимаешь результат?"}
  USER -->|"Нет"| FIX --> CHAT
  USER -->|"Да"| SAVE --> NEXT
  classDef user fill:#fff1cd,stroke:#c28d28,color:#543e12
  class USER user
```

## 5. Именованные чаты Codex: пример Architect → Task

```mermaid
flowchart TB
  C0["Controller: разрешён System Design"]
  A["Codex Bridge: чат WF-ID · architecture · architect"]
  OUT["Architect: system-design.md + handoff.md"]
  MAIN["Главный Orchestrator: отчёт + Mermaid"]
  SAVE["Controller: сохранить утверждение"]
  T["Codex Bridge: чат WF-ID · tasks · task"]
  TASKS["Task: утверждённый System Design → атомарные задачи"]
  C0 --> A --> OUT --> MAIN
  MAIN -.-> G{"ТЫ: System Design утверждён?"}
  G -->|"Доработать"| A
  G -->|"Да"| SAVE --> T --> TASKS
  classDef user fill:#fff1cd,stroke:#c28d28,color:#543e12
  class G user
```

## 6. Изменение после замечаний

```mermaid
flowchart TB
  REP["Отчёт модуля"]
  G{"ТЫ: принимаешь?"}
  FIX["Локально переделать только выбранного агента"]
  REPLAN["Предложить новый порядок/состав модулей"]
  IMPACT["КОД: определить сохраняемые и затронутые утверждения"]
  G2{"ТЫ: изменение маршрута согласовано?"}
  CONTINUE["Сохранить утверждённые результаты → следующий модуль"]
  REP -.-> G
  G -->|"Да"| CONTINUE
  G -->|"Локальная правка"| FIX --> REP
  G -->|"Изменить маршрут"| REPLAN --> IMPACT -.-> G2
  G2 -->|"Да"| CONTINUE
  G2 -->|"Нет"| REP
  classDef user fill:#fff1cd,stroke:#c28d28,color:#543e12
  class G,G2 user
```

## 7. Documentation всегда последний

```mermaid
flowchart TB
  READY["Все выбранные рабочие модули утверждены"]
  DOC["Documentation Chat · AI"]
  GIT["Контекст Git с момента начала задачи + handoffs"]
  AUDIT["Аудит README / docs / API / Mermaid"]
  EDIT["Только необходимые обновления или обоснование отсутствия изменений"]
  REVIEW["Отчёт + Mermaid + diff"]
  END["Финальный checkpoint: завершено"]
  READY --> DOC --> GIT --> AUDIT --> EDIT --> REVIEW
  REVIEW -.-> G{"ТЫ: документация принята?"}
  G -->|"Доработать"| DOC
  G -->|"Да"| END
  classDef user fill:#fff1cd,stroke:#c28d28,color:#543e12
  class G user
```
