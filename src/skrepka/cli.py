"""Console entry point.

Routes `init` / `doctor` to the setup module (repo-only; the reviewed engine
is unchanged) and everything else to the engine. The engine imports the Google
client libraries at module load, so we defer that import and wrap it: a missing
dependency becomes a friendly message instead of a raw traceback (plan r1 #16).
For `doctor --json` the safe-output boundary is established BEFORE the engine
import so a dependency/import failure still yields one sanitized JSON (r3 #4).
"""

import json
import sys

_INSTALL_HINT = (
    "skrepka is not fully installed (missing dependencies) — reinstall with "
    "`pipx install skrepka` or `uv tool install skrepka`")

# Curated top-level help. The engine and the setup/privacy modules each own
# their own argparse, so no single parser lists every command; without this,
# `skrepka --help` would hide `init` — the first command a new user needs.
_TOP_HELP = """skrepka — бережная совместная правка Google-документов.

Настройка:
  init         Мастер доступа к Google (начните с него)
  doctor       Проверить доступы, токен и связь с API

Комментарии и правки:
  comments     Показать комментарии документа
  reply        Ответить в тред (--file — пачкой, каждый ответ
               в свою секунду)
  resolve      Закрыть тред — это решение человека, не агента
  comment      Оставить комментарий ко всему документу
  patch        Внести правки, не убивая комментарии. --dry-run
               примеряет их, ничего не записывая
  mark         Пометить фрагмент именованным диапазоном
  suggestions  Показать предложенные правки (принять их можно
               только руками в интерфейсе)

Документы:
  upload       Создать документ из .md
  download     Выгрузить документ в markdown
  update       Положить содержимое новым документом или заменить
               существующий целиком. Режим обязателен; замена
               уничтожает все треды
  upload-file  Залить файлы как есть, без превращения в документ
  sync         Слить правки из локального .md обратно в документ,
               сохраняя ОТКРЫТЫЕ треды (экспериментальная; отказывает,
               если правка переписывает прокомментированное)

Данные и приватность:
  logout       Удалить локальный токен, клиент OAuth останется
  revoke       Отозвать токен у Google и удалить его локально
  forget       Удалить локальные токен, ключи и журналы (а с
               --sidecars PATH — и сайдкар документа)

Ещё:
  --version    Установленная версия

Подробности по команде: skrepka <команда> --help
"""


def _bootstrap_error(msg):
    sys.stdout.write(json.dumps({"error": msg}) + "\n")
    sys.exit(1)


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else None

    # Bare `skrepka`, or a lone help request, prints the curated overview and
    # exits 0. `help <cmd>` / `-h <extra>` are NOT swallowed here: they fall
    # through so an unknown top-level token still fails loudly.
    if not argv or argv in (["-h"], ["--help"], ["help"]):
        sys.stdout.write(_TOP_HELP)
        sys.exit(0)

    # Bug reports need a version to quote (#5). Compared as the whole argv, so
    # `skrepka --version extra` still fails loudly instead of exiting 0; the
    # version lives in skrepka/__init__.py, which imports nothing heavy, so a
    # broken dependency install can still answer this.
    if argv == ["--version"]:
        from skrepka import __version__
        sys.stdout.write(f"skrepka {__version__}\n")
        sys.exit(0)

    if cmd == "init":
        try:
            from skrepka import setup
        except ImportError:
            _bootstrap_error(_INSTALL_HINT)
        sys.exit(setup.init_main(argv[1:]))

    if cmd == "doctor":
        json_mode = "--json" in argv[1:]
        try:
            from skrepka import setup
        except ImportError:
            if json_mode:
                sys.stdout.write(json.dumps(
                    {"action": "doctor", "ok": False,
                     "error": "not_installed"}) + "\n")
                sys.exit(2)
            _bootstrap_error(_INSTALL_HINT)
        sys.exit(setup.doctor_main(argv[1:]))

    if cmd in ("logout", "revoke", "forget"):
        try:
            from skrepka import privacy
        except ImportError:
            _bootstrap_error(_INSTALL_HINT)
        entry = {"logout": privacy.logout_main,
                 "revoke": privacy.revoke_main,
                 "forget": privacy.forget_main}[cmd]
        sys.exit(entry(argv[1:]))

    try:
        from skrepka._engine import main as engine_main
    except ImportError:
        _bootstrap_error(_INSTALL_HINT)
    engine_main()


if __name__ == "__main__":
    main()
