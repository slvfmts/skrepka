"""Авторство треда: чтение и отправка обязаны судить ОДНОЙ логикой (r19/T14).

Дефект, который здесь закрыт, нашёл живой прогон навыка чужим исполнителем.
Он спланировал ответы по файлу `comments`, где у чужого треда и у треда с
неподтверждённым авторством стоял одинаковый `author.me: false`, и узнал
разницу только на отправке — в `skipped_authorship_unknown`, куда
`--include-foreign` не пускает. Код знал разницу и выбрасывал её.

Отображение слоёв, которое проверяется ниже:

    _reply_author_state   →  публичное `authorship`  →  отправка
    mine                     mine                       разрешена
    foreign                  foreign                    только --include-foreign
    authorship_unknown       unspecified                всегда пропуск

`author.me` остаётся совместимым представлением и решения больше не несёт.
"""

import json

import pytest


def _drive(engine, monkeypatch, comments):
    class _Req:
        def execute(self):
            return {"comments": comments}

    class Drive:
        def comments(self):
            return self

        def list(self, **kw):
            return _Req()

    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda c: Drive())
    return Drive()


def _listing(engine, monkeypatch, capsys, comments):
    _drive(engine, monkeypatch, comments)
    engine.list_comments("doc1")
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Классификация на сырых данных, до всякой нормализации
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("author,expected", [
    ({"displayName": "Слава", "me": True}, "mine"),
    ({"displayName": "Заказчик", "me": False}, "foreign"),
    ({"displayName": "Заказчик"}, "unspecified"),          # ключа нет вовсе
    ({"displayName": "Заказчик", "me": None}, "unspecified"),  # явный null
    (None, "unspecified"),                                  # автора нет
    ("Заказчик", "unspecified"),                            # автор не объект
])
def test_raw_author_shapes_classify(engine, monkeypatch, capsys, author,
                                    expected):
    out = _listing(engine, monkeypatch, capsys,
                   [{"id": "c1", "content": "x", "author": author}])
    assert out[0]["authorship"] == expected


def test_unknown_survives_the_compatibility_normalisation(engine, monkeypatch,
                                                          capsys):
    """Главный случай: наружу всё ещё уходит `author.me: false`.

    Он и ломает мутацию «`comments` смотрит на `author.me`»: нормализованное
    `false` неотличимо от чужого треда, поэтому вердикт обязан быть снят
    раньше и другой функцией.
    """
    out = _listing(engine, monkeypatch, capsys,
                   [{"id": "c1", "content": "x",
                     "author": {"displayName": "Заказчик"}}])
    assert out[0]["authorship"] == "unspecified"
    assert out[0]["author"]["me"] is False


@pytest.mark.parametrize("author", [
    {"displayName": "З"},              # ключа нет вовсе
    {"displayName": "З", "me": None},  # явный null
])
def test_compatibility_view_is_always_a_boolean(engine, monkeypatch, capsys,
                                                author):
    """`author.me` наружу — всегда bool.

    Обещание «нормализовано, чтобы ничего не падало» держится только если оно
    покрывает и явный `null`, а не один отсутствующий ключ: потребитель,
    который читает поле, различия между ними не делает.
    """
    out = _listing(engine, monkeypatch, capsys,
                   [{"id": "c1", "content": "x", "author": dict(author)}])
    assert out[0]["author"]["me"] is False
    assert out[0]["authorship"] == "unspecified"


def test_replies_do_not_move_the_thread(engine, monkeypatch, capsys):
    """Единица решения — тред. Ветка владельца остаётся своей, даже если в
    ней отвечал клиент, и наоборот."""
    out = _listing(engine, monkeypatch, capsys, [
        {"id": "c1", "content": "x",
         "author": {"displayName": "Слава", "me": True},
         "replies": [{"id": "r1", "author": {"displayName": "Заказчик",
                                             "me": False}},
                     {"id": "r2", "author": {"displayName": "Кто-то"}}]},
    ])
    assert out[0]["authorship"] == "mine"


def test_summary_counts_threads_and_adds_up(engine, monkeypatch, capsys):
    _drive(engine, monkeypatch, [
        {"id": "c1", "content": "x", "author": {"displayName": "Ð", "me": True}},
        {"id": "c2", "content": "y", "author": {"displayName": "З", "me": False}},
        {"id": "c3", "content": "z", "author": {"displayName": "?"}},
    ])
    engine.list_comments("doc1", output=None)
    out = json.loads(capsys.readouterr().out)
    kinds = [c["authorship"] for c in out]
    assert kinds == ["mine", "foreign", "unspecified"]


# ---------------------------------------------------------------------------
# Один источник на оба слоя
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("author,state,public", [
    ({"me": True}, "mine", "mine"),
    ({"me": False}, "foreign", "foreign"),
    ({"displayName": "З"}, "authorship_unknown", "unspecified"),
    (None, "authorship_unknown", "unspecified"),
])
def test_the_gate_and_the_listing_agree_on_every_shape(engine, monkeypatch,
                                                       capsys, author, state,
                                                       public):
    """Шлюз и выдача обязаны сойтись на КАЖДОЙ форме автора.

    Тест сравнивает не строки, а семантику: состояние шлюза, публичное имя
    того же состояния и запрет отправки — это три проекции одного решения.
    """
    raw = {"id": "c1", "content": "x", "author": author}
    assert engine._reply_author_state(dict(raw)) == state
    out = _listing(engine, monkeypatch, capsys, [dict(raw)])
    assert out[0]["authorship"] == public
    # и именно `unspecified` — то состояние, которое отправка не пускает
    # никогда, даже с явным разрешением на чужие треды
    assert (public == "unspecified") == (state == "authorship_unknown")
