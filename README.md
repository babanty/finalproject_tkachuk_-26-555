# ValutaTrade Hub

Учебное консольное приложение для виртуального валютного кошелька и отслеживания курсов. Реализация ведётся последовательно; сейчас доступна пакетная точка входа и справка CLI.

## Требования

Windows 11, Git Bash и `uv`. Проект фиксирует Python 3.12 в `.python-version`; `uv` может установить его автоматически. Глобальная установка Python и активация `.venv` вручную не нужны.

Команды из корня `finalproject_tkachuk_М26-555` в Git Bash:

```bash
uv sync --locked
uv run valutatrade --help
uv run python -m valutatrade_hub --help
uv run python main.py --help
```

## Проверки

```bash
bash scripts/check.sh
uv build
```

Перед каждым локальным Git-коммитом выполняются Ruff и полный набор тестов. Реальные JSON-данные приложения, логи и `.venv` не отслеживаются Git. Инструкции по кошельку, настройкам курсов и Parser Service будут дополнены на соответствующих этапах.
