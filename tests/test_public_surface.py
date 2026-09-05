"""Публичная поверхность: README и QUICKSTART обещают то, что есть (r19/T14).

Проверять документ словами почти бесполезно: `assert "--dry-run" in text`
переживает удаление холостого прогона из QUICKSTART, потому что флаг остаётся
в README. Поэтому здесь разбираются исполняемые блоки и проверяется ПОРЯДОК
команд внутри демонстрации, а не наличие подстрок в файле.

Мерка та же, что у кода: стенд `internal/tools/mutate-r19-surface-docs.sh`
ломает эти документы, и на каждую поломку обязан упасть тест отсюда.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _sections(path):
    """{заголовок -> тело} по разделам верхнего уровня."""
    out, current = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _commands(body):
    """Команды `skrepka` из исполняемых блоков, по порядку.

    Комментарии в конце строки отбрасываются: они объясняют шаг, а не входят
    в него. Обычный текст вокруг блоков не читается вовсе — обещание живёт в
    команде, которую человек может выполнить.
    """
    out = []
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", body, re.DOTALL):
        for line in block.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith("skrepka "):
                out.append(line)
    return out


def _verbs(commands):
    return [c.split()[1] for c in commands]


@pytest.fixture(scope="module")
def readme():
    return _sections(ROOT / "README.md")


@pytest.fixture(scope="module")
def quickstart():
    return _sections(ROOT / "docs" / "QUICKSTART.md")


# ---------------------------------------------------------------------------
# Цикл работы: показан целиком и в правильном порядке
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("doc,section", [
    ("README.md", "Как это выглядит"),
    ("QUICKSTART.md", "Шаг 7. Первый прогон"),
])
def test_the_demonstration_shows_the_whole_cycle_in_order(doc, section,
                                                          readme, quickstart):
    body = (readme if doc == "README.md" else quickstart)[section]
    commands = _commands(body)
    assert _verbs(commands) == ["comments", "patch", "patch", "reply"], commands
    # Холостой прогон — у ПЕРВОГО `patch`, и это не придирка к порядку слов:
    # демонстрация, где примеряют после записи, учит обратному.
    dry, write = [c for c in commands if c.startswith("skrepka patch")]
    assert "--dry-run" in dry
    assert "--dry-run" not in write
    # Один и тот же документ на всех четырёх шагах: цикл, а не четыре
    # несвязанных примера.
    docs = {c.split()[2] for c in commands}
    assert len(docs) == 1, docs


@pytest.mark.parametrize("doc,section", [
    ("README.md", "Как это выглядит"),
    ("QUICKSTART.md", "Шаг 7. Первый прогон"),
])
def test_the_main_route_has_no_destructive_commands(doc, section, readme,
                                                    quickstart):
    """`sync` экспериментальна, `update` уничтожает треды. Ни одной из них не
    место в первом, что человек увидит и повторит."""
    body = (readme if doc == "README.md" else quickstart)[section]
    verbs = _verbs(_commands(body))
    assert "update" not in verbs and "sync" not in verbs


def test_quickstart_says_which_step_writes(quickstart):
    body = quickstart["Шаг 7. Первый прогон"]
    assert "ничего не записывает" in body
    assert "единственный, который меняет документ" in body


def test_quickstart_does_not_sell_unknown_as_a_refusal(quickstart):
    """Тот же капкан, что и в навыке: `unknown` на комментированном документе
    получает почти любая правка, и прочитанный как запрет он останавливает
    работу."""
    flat = re.sub(r"\s+", " ", quickstart["Шаг 7. Первый прогон"])
    assert "не повод не пробовать" in flat


# ---------------------------------------------------------------------------
# Обещания, которые нельзя потерять
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["README.md", "README.en.md"])
def test_readme_names_the_unix_only_limit(name):
    """Установка на Windows кончится непонятной ошибкой, и узнать об этом
    человек должен до неё, а не после."""
    text = re.sub(r"\s+", " ", (ROOT / name).read_text(encoding="utf-8"))
    if name.endswith(".en.md"):
        assert "macOS or Linux" in text and "Windows is not supported" in text
    else:
        assert "macOS или Linux" in text and "Windows не поддержан" in text


@pytest.mark.parametrize("name", ["README.md", "README.en.md"])
def test_both_readmes_show_the_cycle(name):
    """Английский README — публичный вход через ссылку «In English», и
    отставать ему нельзя."""
    section = "Как это выглядит" if name == "README.md" else "What it looks like"
    body = _sections(ROOT / name)[section]
    assert _verbs(_commands(body)) == ["comments", "patch", "patch", "reply"]


def test_human_limitations_do_not_read_like_the_technical_reference():
    """Разделение имеет смысл, только если человеческий текст остался
    человеческим: слов из чужой кухни в нём быть не должно."""
    human = (ROOT / "docs" / "LIMITATIONS.md").read_text(encoding="utf-8")
    for jargon in ("replaceAllText", "deleteContentRange", "batchUpdate",
                   "якорь", "призрак"):
        assert jargon not in human, jargon
    # …и ссылка на подробности из него ведёт, иначе технический читатель
    # решит, что материал отменён
    assert "LIMITATIONS-TECHNICAL.md" in human


def test_technical_reference_points_back_and_is_current():
    tech = (ROOT / "docs" / "LIMITATIONS-TECHNICAL.md").read_text(
        encoding="utf-8")
    assert "LIMITATIONS.md" in tech
    assert "0.18" in tech.splitlines()[2]


def test_update_is_described_by_what_it_requires_and_costs():
    """`update` без режима не делает НИЧЕГО, а живая замена уничтожает треды.

    Оба утверждения обязаны стоять там, где человек про команду читает:
    описание, где названа только цена, учит, что потеря тредов — обычный
    порядок вещей, а описание без цены — что команда безобидна.
    """
    from skrepka import cli
    overview = re.sub(r"\s+", " ", cli._TOP_HELP)
    assert "Режим обязателен" in overview
    assert "уничтожает все треды" in overview

    human = re.sub(r"\s+", " ",
                   (ROOT / "docs" / "LIMITATIONS.md").read_text(
                       encoding="utf-8"))
    assert "режим называется явно" in human
    assert "уничтожает все треды" in human
    # Архив — не резервная копия, и обещать восстановление нельзя.
    assert "архив, а не резервная копия" in human
