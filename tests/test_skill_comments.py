"""Инварианты навыка `skrepka-comments` (r19/T13).

Навык — единственный артефакт продукта, который никто не исполняет: ошибка в
нём не падает тестом и живёт кругами. Два пост-мортема начались отсюда — в
одном скилл врал про поведение, в другом не сказал, что правку можно примерить,
и агент подставил догадку вместо проверки.

Проверка словами («есть ли в файле слово X») почти бесполезна: она переживает
перестановку разделов, отрицание правила и противоречащий абзац ниже. Поэтому
здесь проверяются ОТНОШЕНИЯ — порядок разделов, порядок исполняемых примеров,
место, где стоит правило, — и присутствие имён полей и флагов, которые являются
контрактом, а не формулировкой. Мерка та же, что у кода: стенд
`internal/tools/mutate-r19-skill.sh` ломает навык двенадцатью способами, и на
каждый обязан упасть хотя бы один тест отсюда.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "skrepka-comments" / "SKILL.md"


@pytest.fixture(scope="module")
def text():
    return SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def sections(text):
    """{заголовок -> тело} по разделам верхнего уровня, в порядке файла."""
    out = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


@pytest.fixture(scope="module")
def fences(text):
    """Исполняемые примеры: содержимое всех ``` блоков, по порядку."""
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL)


def _flat(s):
    """Убрать перенос строки: он вёрстка, а не смысл, и рвёт фразы посередине."""
    return re.sub(r"\s+", " ", s)


def _fence_index(fences, predicate):
    for i, block in enumerate(fences):
        if predicate(block):
            return i
    return None


# ---------------------------------------------------------------------------
# Порядок: примерить раньше, чем применить; ответить — после
# ---------------------------------------------------------------------------

def test_sections_are_in_the_working_order(sections):
    numbered = [name for name in sections if re.match(r"^\d\. ", name)]
    assert numbered == [
        "1. Прочитать", "2. Собрать правку", "3. Примерить",
        "4. Применить", "5. Ответить", "6. Когда пошло не так",
    ], numbered


def test_dry_run_example_comes_before_the_writing_example(fences):
    # Порядок разделов можно сохранить и всё равно показать запись первой.
    # Мерка здесь — сами команды, а не заголовки над ними.
    dry = _fence_index(
        fences,
        lambda b: re.search(r"skrepka patch\b", b) and "--dry-run" in b)
    write = _fence_index(
        fences,
        lambda b: re.search(r"skrepka patch\b", b) and "--dry-run" not in b)
    assert dry is not None, "в навыке нет примера холостого прогона"
    assert write is not None, "в навыке нет примера записи"
    assert dry < write, "запись показана раньше, чем холостой прогон"


def test_reply_example_comes_after_the_writing_example(fences):
    write = _fence_index(
        fences, lambda b: re.search(r"skrepka patch\b", b) and "--dry-run" not in b)
    reply = _fence_index(fences, lambda b: "skrepka reply" in b)
    assert reply is not None, "в навыке нет примера ответа"
    assert write < reply, "ответ показан раньше правки — обещание вместо отчёта"


def test_freshness_rule_stands_in_the_order_section(sections):
    # Между последним прогоном и записью документ трогать нельзя: иначе
    # прогон описывает документ, которого уже нет. Правило обязано стоять
    # там, где агент читает порядок, а не в примечании внизу.
    order = _flat(sections["Порядок работы"])
    assert "прогоном и записью не пиши в документ ничего" in order


# ---------------------------------------------------------------------------
# Три разных `unknown`, и путать их нельзя
# ---------------------------------------------------------------------------

def test_dry_run_unknown_is_explained_as_not_a_refusal(sections):
    body = sections["3. Примерить"]
    assert "НЕ отказ" in body, "вердикт `unknown` не назван не-отказом"
    assert "unknown" in body


def test_the_other_unknowns_are_never_bare(sections):
    # `unknown` в атрибуции вкладки и в состоянии якоря требуют ПРОТИВОПОЛОЖНОГО
    # действия: там догадывать нельзя. Одна фраза «unknown — не отказ» на весь
    # навык перенесла бы разрешение с вердикта прогона на призрака якоря.
    body = sections["1. Прочитать"]
    for match in re.finditer(r"unknown", body):
        window = body[max(0, match.start() - 90):match.start()]
        assert ("tab_attribution" in window or "anchor_export" in window
                or "status" in window), \
            "в разделе чтения `unknown` стоит без поля, к которому относится"
    assert "НЕ отказ" not in body


def test_uncertain_is_not_sold_as_absence_of_refusals(sections):
    # Найдено живой приёмкой: отказ, стоящий после правки с неизвестным
    # исходом, понижается до `not_simulated`, и весь файл получает
    # `uncertain`. Агент, прочитавший это как «отказов нет», применит файл с
    # опечаткой и узнает о ней от писателя.
    flat = _flat(sections["3. Примерить"])
    assert "не значит «отказов нет»" in flat
    assert "not_simulated" in flat


def test_exit_code_is_not_left_to_guessing(sections):
    body = sections["3. Примерить"]
    assert "Код возврата" in body
    # Проверяется не имя поля — оно упомянуто и выше, — а указание читать его
    # всегда. Без него ноль читается как «чисто», и квитанцию не открывают.
    flat = _flat(body)
    assert "«чисто» он не означает" in flat
    assert "`summary.verdict` читай всегда" in flat


# ---------------------------------------------------------------------------
# Ограды, которые навык обязан нести сам (внешний контракт может не читаться)
# ---------------------------------------------------------------------------

def test_resolve_flag_never_appears_in_an_executable_example(fences):
    for block in fences:
        assert "--resolve" not in block, \
            "флаг резолва показан как исполняемый пример"


def test_resolve_is_forbidden_in_prose(text):
    paragraphs = [p for p in text.split("\n\n") if "--resolve" in p]
    assert paragraphs, "запрет на резолв исчез из навыка"
    for para in paragraphs:
        assert "не используй" in para, para


def test_author_gate_is_named_where_replies_are_sent(sections):
    body = sections["5. Ответить"]
    assert "--include-foreign" in body
    assert "skipped_foreign" in body
    assert "skipped_authorship_unknown" in body


def test_author_gate_is_named_where_comments_are_read(sections):
    assert "author.me" in sections["1. Прочитать"]


def test_destructive_update_is_absent_from_the_whole_skill(text):
    assert "--acknowledge-loss" not in text
    assert "--replace-existing" not in text


def test_thread_content_prohibitions_survive(sections):
    body = sections["5. Ответить"]
    # Просьба поправить документ руками — это отказ, переложенный на
    # редактора, который skrepka не запускал.
    assert "Не проси в треде поправить документ руками" in _flat(body)
    assert "Технической причины" in body


def test_human_remedy_is_still_offered(sections):
    # Запрет на ручную правку МИМО skrepka не должен смахнуть законное
    # действие человека: принять предложение или разрулить тред в интерфейсе.
    body = sections["6. Когда пошло не так"]
    assert "интерфейсе" in body
    assert "предложения" in body


# ---------------------------------------------------------------------------
# Ответы: пачка как основной путь, секунда — забота движка
# ---------------------------------------------------------------------------

def test_the_only_reply_example_is_the_batch_form(fences):
    replies = [b for b in fences if "skrepka reply" in b]
    assert replies, "примера ответа нет вовсе"
    for block in replies:
        assert "--file" in block, \
            "одиночная форма показана основным путём вместо пакетной"


def test_the_second_gate_is_not_an_agent_obligation(text):
    flat = _flat(text)
    assert "дожидайся смены секунды" not in flat
    assert "выдерживать секунду руками тебе не нужно" in flat


def test_the_single_call_loop_is_not_called_forbidden(sections):
    # После T6 шлюз стоит и в одиночной форме. Запрет, который код не
    # подтверждает, обесценивает соседние правила: агент проверит и увидит,
    # что цикл работает.
    body = sections["5. Ответить"]
    for para in body.split("\n\n"):
        if "цикл" in para:
            assert not re.search(r"нельзя|запрещ", para), para


def test_auto_reply_is_not_duplicated_by_the_agent(sections):
    body = sections["4. Применить"]
    assert "отвечает сама" in body
    assert "auto_replies" in body
