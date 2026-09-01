"""Пакетный ответ и секундный шлюз (T6, M27/M31).

Ключ учёта якорей — `(author.displayName, секунда)`. Тред, у которого не
осталось ни одного уникального такого ключа, перестаёт опознаваться в выгрузке,
и тогда отказывают ВСЕ замены в документе, включая абзацы без комментариев.
Замерено M31 напрямую.

Отсюда роль шлюза: он не расшивает запертые документы, а не даёт скрепке
отнять у треда последнего свидетеля самой. Это единственный способ, которым она
может запереть чужой документ, — и он же нормальный режим работы навыка:
«ответь на треды по делу» агент выполняет циклом.
"""
import json
import os

import pytest

CREATED = "2026-09-01T10:00:00.000Z"


class Clock:
    """Поддельные часы: пауза не спит, а двигает время. Тест обязан быть
    быстрым, но проверять он должен настоящую арифметику ожидания."""

    def __init__(self, start=1000.0):
        self.t = start
        self.slept = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.t += seconds


class Drive:
    """Дублёр Drive для `reply`: перепись, ответы и `about`."""

    def __init__(self, comments=(), me="Я", permission_id="pid-1",
                 stamps=None, errors=None, about_fails=False,
                 visible_after=0):
        self.comments_payload = [dict(c) for c in comments]
        self.me, self.permission_id = me, permission_id
        self.stamps = list(stamps or [])
        self.errors = dict(errors or {})
        self.about_fails = about_fails
        # Свежий `comments.list` может ещё не показывать только что созданный
        # ответ. `visible_after` откладывает его появление на N переписей.
        self.visible_after = visible_after
        self.pending_visible = []
        self.created = []
        self.list_calls = 0
        # часы стенда: во сколько случилась каждая попытка записи. Считать
        # паузы по отдельным `sleep` нельзя — ожидание шлюза складывается из
        # уже прошедшего времени и остатка (поймано стендом мутаций).
        self.clock = None
        self.attempt_times = []
        # чужая рука: колбэк вызывается на каждой переписи и может изменить
        # документ — так выглядит человек, отвечающий в UI между нашими
        # записями
        self.on_list = None

    # --- маршрутизация
    def comments(self):
        return self

    def replies(self):
        return self

    def about(self):
        return self

    class _Req:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            if isinstance(self.payload, Exception):
                raise self.payload
            return self.payload

    def list(self, **kw):
        self.list_calls += 1
        if self.on_list:
            self.on_list(self, self.list_calls)
        for item in list(self.pending_visible):
            item["left"] -= 1
            if item["left"] <= 0:
                item["into"].append(item["reply"])
                self.pending_visible.remove(item)
        return self._Req([dict(c) for c in self.comments_payload]
                         and {"comments": [dict(c) for c in
                                           self.comments_payload]}
                         or {"comments": []})

    def get(self, **kw):
        if self.about_fails:
            return self._Req(RuntimeError("about недоступен"))
        return self._Req({"user": {"displayName": self.me,
                                   "permissionId": self.permission_id}})

    def create(self, **kw):
        cid = kw.get("commentId")
        if self.clock is not None:
            self.attempt_times.append(self.clock.t)
        if cid in self.errors:
            return self._Req(self.errors[cid])
        rid = f"r{len(self.created) + 1}"
        stamp = (self.stamps.pop(0) if self.stamps
                 else f"2026-09-01T10:00:{len(self.created):02d}.000Z")
        self.created.append((cid, kw["body"]["content"]))
        # ответ немедленно виден в переписи: пост-проверка её и спрашивает
        for c in self.comments_payload:
            if c.get("id") == cid:
                entry = {"id": rid, "createdTime": stamp,
                         "author": {"displayName": self.me, "me": True}}
                bucket = c.setdefault("replies", [])
                if self.visible_after:
                    self.pending_visible.append(
                        {"left": self.visible_after, "into": bucket,
                         "reply": entry})
                else:
                    bucket.append(entry)
        return self._Req({"id": rid, "createdTime": stamp}
                         if stamp is not None else {"id": rid})


def comment(cid, *, me=True, author="Я", created=CREATED, resolved=False,
            deleted=False, replies=(), no_author=False, no_me=False):
    c = {"id": cid, "createdTime": created, "resolved": resolved,
         "deleted": deleted, "replies": [dict(r) for r in replies],
         "quotedFileContent": {"value": "x"}}
    if not no_author:
        c["author"] = {"displayName": author}
        if not no_me:
            c["author"]["me"] = me
    return c


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Конфиг в поддельном HOME: блокировка берётся НАСТОЯЩАЯ, но на
    одноразовом каталоге."""
    os.chmod(tmp_path, 0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKREPKA_CONFIG_DIR", str(tmp_path / "cfg"))
    import skrepka.config as config
    config.ensure_config_dir()
    return config


@pytest.fixture
def clock(engine, monkeypatch):
    c = Clock()
    monkeypatch.setattr(engine.time, "monotonic", c.monotonic)
    monkeypatch.setattr(engine.time, "sleep", c.sleep)
    return c


def _wire(engine, monkeypatch, drive):
    monkeypatch.setattr(engine, "get_creds", lambda: object())
    monkeypatch.setattr(engine, "get_drive_service", lambda c: drive)


def _file(tmp_path, pairs, name="replies.json"):
    path = tmp_path / name
    path.write_text(json.dumps(
        {"replies": [{"comment_id": c, "text": t} for c, t in pairs]}),
        encoding="utf-8")
    return str(path)


def _run(engine, tmp_path, capsys, path, **kw):
    code = 0
    try:
        engine.batch_reply("doc1", path, **kw)
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)


def _journal(path):
    with open(path + ".skrepka-reply-journal.json", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ схема --

@pytest.mark.parametrize("payload, need", [
    ({"replies": []}, "непустой список"),
    ({"replies": [{"comment_id": "c1"}]}, "пустым 'text'"),
    ({"replies": [{"comment_id": "c1", "text": " "}]}, "пустым 'text'"),
    ({"replies": [{"comment_id": "c1", "text": "a", "resolve": True}]},
     "не резолвит треды"),
    ({"replies": [{"comment_id": "c1", "text": "a"},
                  {"comment_id": "c1", "text": "b"}]}, "повторяется"),
    ({"replies": [{"comment_id": "c1", "text": "a"}], "extra": 1},
     "единственным полем"),
])
def test_the_input_is_read_strictly(engine, tmp_path, capsys, payload, need):
    """Файл описывает записи в ЧУЖОЙ документ: молча понятое «примерно то»
    здесь дороже отказа."""
    path = tmp_path / "r.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit):
        engine._reply_plan_from_file(str(path))
    assert need in json.loads(capsys.readouterr().out)["error"]


# ------------------------------------------------------------ круг авторов --

def test_only_my_own_threads_get_a_reply_by_default(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    drive = Drive([comment("c1", me=True), comment("c2", me=False)])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "мой"), ("c2", "чужой")]))
    assert [c[0] for c in drive.created] == ["c1"]
    assert [s["comment_id"] for s in out["skipped_foreign"]] == ["c2"]
    assert code == 3          # что-то пропущено — исход частичный


def test_include_foreign_opens_the_other_threads(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    drive = Drive([comment("c1", me=True), comment("c2", me=False)])
    _wire(engine, monkeypatch, drive)
    code, _out = _run(engine, tmp_path, capsys,
                      _file(tmp_path, [("c1", "мой"), ("c2", "чужой")]),
                      include_foreign=True)
    assert [c[0] for c in drive.created] == ["c1", "c2"]
    assert code == 0


def test_the_ROOT_decides_the_thread_not_the_last_reply(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Единица — тред, а не реплика: ветка владельца остаётся своей, даже
    если в ней отвечал клиент."""
    drive = Drive([comment("c1", me=True, replies=[
        {"id": "r0", "createdTime": CREATED,
         "author": {"displayName": "Клиент", "me": False}}])])
    _wire(engine, monkeypatch, drive)
    _code, _out = _run(engine, tmp_path, capsys,
                       _file(tmp_path, [("c1", "мой")]))
    assert [c[0] for c in drive.created] == ["c1"]


@pytest.mark.parametrize("kw", [{"no_me": True}, {"no_author": True}])
def test_unknown_authorship_is_not_the_same_as_foreign(
        engine, monkeypatch, tmp_path, capsys, store, clock, kw):
    """Google опускает `author.me`, когда ему угодно. Смешать такой тред с
    чужим — соврать: это не «чужой», а «Google не сказал». И писать в него по
    догадке нельзя даже с `--include-foreign`."""
    drive = Drive([comment("c1", **kw)])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "текст")]),
                     include_foreign=True)
    assert drive.created == []
    assert [s["comment_id"] for s in out["skipped_authorship_unknown"]] == ["c1"]
    assert out["skipped_foreign"] == []
    assert code == 3


def test_missing_and_resolved_threads_are_listed_apart(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    drive = Drive([comment("c1", resolved=True), comment("c2", deleted=True)])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b"), ("c3", "c")]))
    assert [s["comment_id"] for s in out["resolved"]] == ["c1"]
    assert sorted(s["comment_id"] for s in out["missing"]) == ["c2", "c3"]
    assert drive.created == [] and code == 3


# ------------------------------------------------------------------ шлюз ---

def test_replies_are_spread_across_whole_seconds(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Между записями — не меньше 1,1 с. Фиксированный сон меньше секунды
    запрещён: он не разводит записи по СЕКУНДАМ."""
    drive = Drive([comment("c1"), comment("c2"), comment("c3")])
    _wire(engine, monkeypatch, drive)
    _run(engine, tmp_path, capsys,
         _file(tmp_path, [("c1", "a"), ("c2", "b"), ("c3", "c")]))
    assert len(drive.created) == 3
    assert all(s >= engine._REPLY_GATE_SECONDS for s in clock.slept)
    assert len([s for s in clock.slept if s >= 1.0]) >= 2


def test_the_FIRST_reply_waits_too_when_the_document_already_has_my_entry(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Пакет, запущенный сразу после ответа из UI или прошлого процесса,
    столкнётся уже ПЕРВЫМ запросом. Памяти между процессами нет, поэтому
    предыдущая запись ищется в свежей переписи."""
    drive = Drive([comment("c1"), comment("c2", author="Я")])
    _wire(engine, monkeypatch, drive)
    _run(engine, tmp_path, capsys, _file(tmp_path, [("c1", "a")]))
    assert clock.slept and clock.slept[0] >= engine._REPLY_GATE_SECONDS


def test_nothing_to_collide_with_means_no_wait_at_all(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Ждать без причины — тоже цена. В документе нет ни одной моей записи в
    другом треде, значит столкнуться не с чем."""
    drive = Drive([comment("c1", author="Я")])
    _wire(engine, monkeypatch, drive)
    _run(engine, tmp_path, capsys, _file(tmp_path, [("c1", "a")]))
    assert clock.slept == []


def test_an_entry_in_the_SAME_thread_is_harmless(engine, store):
    """Ключ по-прежнему принадлежит одному треду и остаётся его свидетелем."""
    raw = [comment("c1", replies=[{"id": "r0", "createdTime": CREATED,
                                   "author": {"displayName": "Я"}}])]
    assert engine._entries_by_author_elsewhere(raw, "Я", "c1") == []


def test_the_gate_looks_at_the_SAME_universe_as_the_accounting(engine):
    """Не «мой последний ответ». Корень, закрытый тред и УДАЛЁННЫЙ ответ
    владеют ключом наравне с обычным ответом — учёт строит ключи по всем."""
    raw = [comment("c9", author="Я", resolved=True,
                   replies=[{"id": "rx", "deleted": True,
                             "createdTime": CREATED,
                             "author": {"displayName": "Я"}}])]
    got = engine._entries_by_author_elsewhere(raw, "Я", "c1")
    assert len(got) == 2          # и корень закрытого треда, и удалённый ответ


def test_the_gate_keys_on_the_display_name_not_on_me(engine):
    """Коллизия в выгрузке определяется по `displayName`; `me` к ключу
    отношения не имеет, и другой аккаунт с тем же именем даёт тот же ключ."""
    raw = [comment("c9", author="Я", me=False)]
    assert engine._entries_by_author_elsewhere(raw, "Я", "c1")
    assert engine._second_key({"author": {"displayName": "Я", "me": False},
                               "createdTime": CREATED}) == ("Я", CREATED[:19] + "Z")


def test_the_universe_and_the_gate_share_one_key_builder(engine):
    """Учёт сравнивает СТРОКУ, усечённую `_trunc_seconds`. Считать по-разному
    значит однажды разойтись."""
    raw = [comment("c1", author="Я", created="2026-09-01T10:00:00.999Z")]
    key = engine._second_key(raw[0])
    assert key in engine._key_owners_universe(raw)
    assert key[1] == "2026-09-01T10:00:00Z"


def test_a_batch_without_an_account_id_refuses_before_writing(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Журнал опознаёт аккаунт по `permissionId`. Записать туда пустоту значит
    сломать возобновление всем следующим запускам: первый прогон положил бы
    `null`, а следующий удачный `about()` дал бы настоящий id и получил бы
    отказ по несовпадению журнала."""
    drive = Drive([comment("c1", me=False)], about_fails=True)
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys, _file(tmp_path, [("c1", "a")]),
                     include_foreign=True)
    assert drive.created == []
    assert out["reason"] == "reply_journal_mismatch" and code == 1


def test_the_identity_is_completed_from_the_census(engine, monkeypatch):
    """`about()` вернул имя без `permissionId` — раньше на этом и выходили.
    Недостающее добирается из переписи, а не бросается."""
    class Partial(Drive):
        def get(self, **kw):
            return self._Req({"user": {"displayName": "Я"}})

    drive = Partial([comment("c1", me=True)])
    drive.comments_payload[0]["author"]["permissionId"] = "pid-9"
    assert engine._my_identity(drive, drive.comments_payload) == ("Я", "pid-9")


# ------------------------------------------------------------ холостой ход --

def test_dry_run_never_reaches_the_writer(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Не проверка флага, а отсутствие вызова: проверку можно снять правкой,
    несделанный вызов — нельзя."""
    drive = Drive([comment("c1"), comment("c2", me=False)])
    _wire(engine, monkeypatch, drive)
    path = _file(tmp_path, [("c1", "мой"), ("c2", "чужой")])
    code, out = _run(engine, tmp_path, capsys, path, dry_run=True)
    assert drive.created == [] and code == 0
    assert out["action"] == "dry-run"
    assert [w["comment_id"] for w in out["would_reply"]] == ["c1"]
    assert [w["text"] for w in out["would_reply"]] == ["мой"]
    assert not os.path.exists(path + ".skrepka-reply-journal.json")


def test_dry_run_says_whether_it_would_wait_on_THIS_snapshot(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Обещать за будущий запуск нельзя: между прогонами документ живёт своей
    жизнью."""
    drive = Drive([comment("c1"), comment("c2", author="Я")])
    _wire(engine, monkeypatch, drive)
    _code, out = _run(engine, tmp_path, capsys,
                      _file(tmp_path, [("c1", "a")]), dry_run=True)
    assert out["would_wait_before_first_on_this_snapshot"] is True
    assert clock.slept == []      # холостой прогон не ждёт


# ---------------------------------------------------------------- журнал ---

def test_the_journal_marks_inflight_BEFORE_the_request(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Классическое окно: процесс умирает после `create`, но до записи
    `applied`, и возобновление отправляет второй раз."""
    seen = []
    drive = Drive([comment("c1")])

    real_write = engine._reply_journal_write

    def spy(path, header, records, stopped):
        seen.append({k: v.get("state") for k, v in records.items()})
        return real_write(path, header, records, stopped)

    monkeypatch.setattr(engine, "_reply_journal_write", spy)
    _wire(engine, monkeypatch, drive)
    _run(engine, tmp_path, capsys, _file(tmp_path, [("c1", "a")]))
    assert seen[0]["c1"] == "inflight"
    assert "applied" in [s["c1"] for s in seen]


def test_an_interrupted_inflight_becomes_unknown_and_is_never_repeated(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Запрос ушёл, чем кончился — неизвестно. Повтор породил бы дубль в
    чужом документе."""
    path = _file(tmp_path, [("c1", "a")])
    drive = Drive([comment("c1")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a")])},
            "replies": [{"comment_id": "c1", "text": "a",
                         "state": "inflight"}]}, fh)
    code, out = _run(engine, tmp_path, capsys, path)
    assert drive.created == []
    assert [f["state"] for f in out["failed"]] == ["unknown"]
    assert code == 3


def test_a_pending_check_is_verified_before_anything_else_is_written(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Пока прошлая пост-проверка не сделана, следующая запись не разрешена:
    иначе созданная в прошлый раз коллизия осталась бы незамеченной, а мы
    добавили бы к ней ещё одну. `create` при этом не зовётся вовсе."""
    path = _file(tmp_path, [("c1", "a"), ("c2", "b")])
    drive = Drive([comment("c1", replies=[{"id": "r1", "createdTime": CREATED,
                                           "author": {"displayName": "Я"}}]),
                   comment("c2")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a"), ("c2", "b")])},
            "replies": [{"comment_id": "c1", "text": "a", "state": "applied",
                         "reply_id": "r1", "collision_check": "pending"}]}, fh)
    _code, _out = _run(engine, tmp_path, capsys, path)
    assert [c[0] for c in drive.created] == ["c2"]      # c1 повторно НЕ ушёл
    assert _journal(path)["replies"][0]["collision_check"] == "clean"


def test_a_changed_account_forbids_resuming(engine, monkeypatch, tmp_path,
                                            capsys, store, clock):
    """Журналу аккаунты надо РАЗЛИЧАТЬ — семантика обратная шлюзу, и потому
    ключ здесь `permissionId`, а не отображаемое имя."""
    path = _file(tmp_path, [("c1", "a")])
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "ЧУЖОЙ", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a")])},
            "replies": [{"comment_id": "c1", "text": "a",
                         "state": "applied"}]}, fh)
    drive = Drive([comment("c1")])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys, path)
    # НЕ «начинаем заново»: молча отправить всё повторно — это второй ответ в
    # чужом документе и затёртое свидетельство прошлого прогона
    assert drive.created == []
    assert out["reason"] == "reply_journal_mismatch"
    assert code == 1


def test_narrowing_the_mode_forbids_resuming_but_widening_does_not(
        engine, tmp_path, store):
    """Разрешён ровно переход `false → true`: человек посмотрел на пропущенных
    чужих и решил ответить и им. Обратный переход возобновление запрещает."""
    path = str(tmp_path / "r.json")
    h = engine._reply_input_hash([("c1", "a")])
    with open(engine._reply_journal_path(path), "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": True,
            "input_sha256": h}, "replies": []}, fh)
    jp = engine._reply_journal_path(path)
    assert engine._read_reply_journal(jp, "doc1", "pid-1", h, True)[1] == "valid"
    # сужение режима — не «журнала нет», а именно расхождение: разница в том,
    # что первое молча начало бы всё заново
    got, status = engine._read_reply_journal(jp, "doc1", "pid-1", h, False)
    assert got is None and status not in ("missing", "valid")
    assert engine._read_reply_journal(
        jp + ".нет", "doc1", "pid-1", h, True) == (None, "missing")


# ------------------------------------------------------------- пост-проверка

def test_a_reply_that_takes_the_last_witness_stops_the_batch(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Условие блокировки — тред без единого уникального ключа (замерено M31).
    Наш ответ отнимает последнего свидетеля у СОСЕДА: он ложится в ту же
    секунду, в которой стоит единственный ключ соседнего треда. Остановка
    немедленная — каждая следующая запись запирает ещё один тред."""
    other = "2026-09-01T11:11:11.000Z"
    drive = Drive([comment("c1", created="2026-09-01T09:00:00.000Z"),
                   comment("c9", created=other),
                   comment("c2")],
                  stamps=[other])       # ответ попадёт в секунду соседа c9
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert out["stopped_because"] == "identity_collision_created"
    assert [c[0] for c in drive.created] == ["c1"]        # c2 уже не ушёл
    assert out["applied"][0]["collision_check"] == "collision"
    assert [n["comment_id"] for n in out["not_attempted"]] == ["c2"]
    assert code == 3


def test_a_clean_check_lets_the_batch_continue(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    drive = Drive([comment("c1"), comment("c2")])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert code == 0 and out["stopped_because"] is None
    assert all(a["collision_check"] == "clean" for a in out["applied"])


def test_a_reply_without_a_readable_second_stops_but_is_not_a_failure(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Ответ применён, а следующий отправить нечем: без читаемой секунды шлюз
    не на чем строить. Называть это `failed` — врать о том, что произошло."""
    drive = Drive([comment("c1"), comment("c2")], stamps=[None])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert out["stopped_because"] == "reply_second_unknown"
    assert [a["comment_id"] for a in out["applied"]] == ["c1"]
    assert [c[0] for c in drive.created] == ["c1"]
    assert code == 3


# ------------------------------------------------------------------ ошибки --

def _http(status, retry_after=None):
    from googleapiclient.errors import HttpError

    class Resp(dict):
        def __init__(self, s):
            super().__init__()
            self.status, self.reason = s, "boom"
            if retry_after is not None:
                self["retry-after"] = retry_after
    return HttpError(Resp(status), b"{}")


def test_a_local_4xx_does_not_stop_the_rest(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """404 на конкретный тред — местная беда: остальные ответы того же файла
    пройдут."""
    drive = Drive([comment("c1"), comment("c2")], errors={"c1": _http(404)})
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert [c[0] for c in drive.created] == ["c2"]
    assert out["stopped_because"] is None and code == 3


def test_auth_failure_stops_the_batch(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """401 и 403 глобальные: следующие записи тоже не пройдут, и перебирать
    файл впустую незачем."""
    drive = Drive([comment("c1"), comment("c2")], errors={"c1": _http(403)})
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert drive.created == []
    assert out["stopped_because"] == "auth_failed" and code == 3


def test_a_5xx_after_sending_is_unknown_and_never_retried(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Автоповтора нет: повтор породил бы дубль в чужом документе."""
    drive = Drive([comment("c1"), comment("c2")], errors={"c1": _http(503)})
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert out["stopped_because"] == "unknown_write_outcome"
    assert [f["state"] for f in out["failed"]] == ["unknown"]
    # c2 не провалилась — до неё просто не дошли, и путать одно с другим
    # значит соврать о том, сколько работы осталось
    assert [n["comment_id"] for n in out["not_attempted"]] == ["c2"]
    assert code == 3


# --------------------------------------------------------------- блокировка -

def test_the_lock_is_per_DOCUMENT_and_nothing_else(engine, store):
    """Два процесса могут держать разные `replies.json` на один документ —
    блокировка по пути журнала была бы мнимой защитой.

    Аккаунта в имени нет намеренно: он ничего не добавляет к защите, ресурс
    здесь документ. Зато создавал бы дыру — процесс, у которого `about()` не
    ответил, взял бы «блокировку неизвестного аккаунта», а внутри неё узнал бы
    настоящий id, и правильную блокировку в этот момент держал бы другой.
    """
    assert (engine._document_lock_name("doc1")
            == engine._document_lock_name("doc1"))
    assert (engine._document_lock_name("doc1")
            != engine._document_lock_name("doc2"))


def test_a_symlinked_lock_is_refused(engine, tmp_path, store):
    """`O_NOFOLLOW`: подсунутая ссылка не должна увести запись в чужой файл."""
    name = engine._document_lock_name("doc1")
    path = store._named_lock_path(name)
    os.symlink(str(tmp_path / "elsewhere"), path)
    with pytest.raises(store.ConfigError):
        with store.lock(name):
            pass


def test_a_lock_name_cannot_escape_its_directory(store):
    """Имя строится хешем, а не из пользовательского текста: оно становится
    именем файла."""
    for bad in ("../evil", "a/b", "", "Ы"):
        with pytest.raises(store.ConfigError):
            store._named_lock_path(bad)


# ------------------------------------------------------- одиночная форма ---

def test_the_single_form_keeps_its_contract_and_gains_the_gate(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Круг авторов на одиночную форму НЕ распространяется: человек назвал
    тред явно, и это само по себе разрешение. Меняется скорость, не контракт."""
    drive = Drive([comment("c1", me=False), comment("c9", author="Я")])
    _wire(engine, monkeypatch, drive)
    engine.reply_comment("doc1", "c1", "ответ в чужой тред")
    out = json.loads(capsys.readouterr().out)
    assert out["comment_id"] == "c1" and drive.created == [("c1", "ответ в чужой тред")]
    assert clock.slept and clock.slept[0] >= engine._REPLY_GATE_SECONDS


# ------------------------------------------- находки стенда мутаций --------
# Четыре мутации пережили первый прогон. Каждая назвала поведение, которое
# никто не проверял; самая ценная — четвёртая: она отличает безвредную
# коллизию от той, что отнимает у треда последнего свидетеля (M31).

def test_an_unknown_outcome_is_never_retried_either(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """`unknown` — это «запрос ушёл, чем кончился, мы не знаем». Повтор
    породил бы дубль в чужом документе так же верно, как повтор `inflight`."""
    path = _file(tmp_path, [("c1", "a")])
    drive = Drive([comment("c1")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a")])},
            "replies": [{"comment_id": "c1", "text": "a",
                         "state": "unknown"}]}, fh)
    _code, out = _run(engine, tmp_path, capsys, path)
    assert drive.created == []
    assert [f["state"] for f in out["failed"]] == ["unknown"]


def test_a_changed_input_file_forbids_resuming(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Журнал возобновляется только на ТОМ ЖЕ входе. Иначе состояния прошлого
    прогона относились бы к другим ответам, и «уже отправлено» означало бы
    не то, что написано."""
    path = _file(tmp_path, [("c1", "новый текст")])
    drive = Drive([comment("c1")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "СТАРЫЙ текст")])},
            "replies": [{"comment_id": "c1", "text": "СТАРЫЙ текст",
                         "state": "applied"}]}, fh)
    code, out = _run(engine, tmp_path, capsys, path)
    assert drive.created == []
    assert out["reason"] == "reply_journal_mismatch"
    assert code == 1


def test_the_check_waits_for_the_reply_to_show_up_in_the_census(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Свежий `comments.list` может ещё не показывать только что созданный
    ответ. Сказать в этот момент «коллизии нет» — соврать: мы смотрели на
    состояние ДО своей записи."""
    drive = Drive([comment("c1")], visible_after=2)
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys, _file(tmp_path, [("c1", "a")]))
    assert out["applied"][0]["collision_check"] == "clean"
    assert code == 0
    assert drive.list_calls >= 3      # перепись спрашивали, пока не увидели


def test_a_collision_that_leaves_a_witness_is_harmless(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Главное уточнение M31: документ запирает не коллизия сама по себе, а
    тред, у которого не осталось НИ ОДНОГО уникального ключа.

    Здесь ответ попадает в секунду соседнего треда, но у того есть свой корень
    в отдельной секунде — он остаётся опознаваемым, и останавливаться не за
    чем. Правило «любая коллизия — стоп» отказало бы по живому документу.
    """
    shared = "2026-09-01T11:11:11.000Z"
    drive = Drive([comment("c1", created="2026-09-01T09:00:00.000Z"),
                   # у соседа ЕСТЬ свой уникальный ключ корня, и вдобавок
                   # ответ в ту же секунду, куда попадём мы
                   comment("c9", created="2026-09-01T09:00:01.000Z",
                           replies=[{"id": "r0", "createdTime": shared,
                                     "author": {"displayName": "Я"}}]),
                   comment("c2", created="2026-09-01T09:00:02.000Z")],
                  stamps=[shared, "2026-09-01T11:11:13.000Z"])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert [c[0] for c in drive.created] == ["c1", "c2"]
    assert out["stopped_because"] is None and code == 0


# ---------------------------------------------------- находки ревью кода ---

def test_a_terminal_outcome_from_the_PAST_run_stops_this_one(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """«Повторно не отправляем» касалось одной записи, а остальные ответы
    уходили поверх неразобранного исхода. Терминальный исход прошлого прогона
    обязан останавливать и этот."""
    path = _file(tmp_path, [("c1", "a"), ("c2", "b")])
    drive = Drive([comment("c1"), comment("c2")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a"), ("c2", "b")])},
            "replies": [{"comment_id": "c1", "text": "a",
                         "state": "unknown"}]}, fh)
    code, out = _run(engine, tmp_path, capsys, path)
    assert drive.created == []                    # c2 тоже не ушла
    assert out["stopped_because"] == "unknown_write_outcome" and code == 3


def test_the_resumed_check_compares_against_the_SAVED_baseline(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Возобновление считает «до» по свежей переписи — а в ней наш прошлый
    ответ уже виден, и повреждённый им тред уже попал бы в «до». Разница
    вышла бы нулевой, и проверка сказала бы «чисто» о том самом вреде, который
    ищет. Сравнивать надо с множеством, сохранённым ПЕРЕД записью."""
    path = _file(tmp_path, [("c1", "a"), ("c2", "b")])
    shared = "2026-09-01T11:11:11.000Z"
    # c9 остался без свидетеля из-за нашего прошлого ответа r1
    drive = Drive([comment("c1", created="2026-09-01T09:00:00.000Z",
                           replies=[{"id": "r1", "createdTime": shared,
                                     "author": {"displayName": "Я"}}]),
                   comment("c9", created=shared),
                   comment("c2", created="2026-09-01T09:00:02.000Z")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a"), ("c2", "b")])},
            "replies": [{"comment_id": "c1", "text": "a", "state": "applied",
                         "reply_id": "r1", "collision_check": "pending",
                         # снимок «до», сохранённый вместе с inflight
                         "naked_baseline": []}]}, fh)
    code, out = _run(engine, tmp_path, capsys, path)
    assert out["stopped_because"] == "identity_collision_created"
    assert drive.created == [] and code == 3


def test_an_applied_reply_without_an_id_stops_instead_of_writing_on(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Проверить нечего и никогда не будет: без id ответа его не отличить от
    чужого. Продолжать записи поверх непроверенного нельзя."""
    path = _file(tmp_path, [("c1", "a"), ("c2", "b")])
    drive = Drive([comment("c1"), comment("c2")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a"), ("c2", "b")])},
            "replies": [{"comment_id": "c1", "text": "a", "state": "applied",
                         "collision_check": "pending"}]}, fh)
    code, out = _run(engine, tmp_path, capsys, path)
    assert drive.created == []
    assert out["stopped_because"] == "collision_check_unknown" and code == 3


def test_an_unverifiable_check_leaves_pending_and_stops(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Ответ так и не показался в переписи. Сказать «чисто» нельзя — мы
    смотрели на состояние ДО своей записи; сказать «коллизия» тоже нельзя.
    Неопределённость остаётся неопределённостью, и батч встаёт."""
    drive = Drive([comment("c1"), comment("c2")], visible_after=99)
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    assert out["stopped_because"] == "collision_check_unknown"
    assert [c[0] for c in drive.created] == ["c1"]
    assert out["applied"][0]["collision_check"] == "pending" and code == 3


def test_a_429_is_retried_after_its_own_delay_and_through_the_gate_again(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """429 — единственный случай, где повтор уместен: запись не состоялась,
    значит дубля не будет. Но ожидание Google не заменяет нашего: после паузы
    снова через шлюз."""
    class Once(Drive):
        def __init__(self, *a, retry_after="2", **kw):
            super().__init__(*a, **kw)
            self.first, self.retry_after = True, retry_after

        def create(self, **kw):
            if self.first:
                self.first = False
                if self.clock is not None:
                    self.attempt_times.append(self.clock.t)
                return self._Req(_http(429, retry_after=self.retry_after))
            return super().create(**kw)

    # пауза Google берётся заведомо КОРОТКОЙ: иначе она сама сойдёт за
    # паузу шлюза, и тест перестанет различать «повторили через шлюз» и
    # «повторили сразу» (стенд мутаций поймал ровно это)
    drive = Once([comment("c1"), comment("c9", author="Я")],
                 retry_after="0.1")
    drive.clock = clock
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys, _file(tmp_path, [("c1", "a")]))
    assert [c[0] for c in drive.created] == ["c1"]
    assert code == 0 and out["stopped_because"] is None
    assert 0.1 in clock.slept                     # пауза, названная Google
    # между отказом и повтором прошла ПОЛНАЯ пауза шлюза, а не только та,
    # которую назвал Google
    first, second = drive.attempt_times
    assert second - first >= engine._REPLY_GATE_SECONDS


def test_the_single_form_takes_the_document_lock_too(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Без блокировки два одиночных процесса на одной машине проходят шлюз
    одновременно и пишут в одну секунду — ровно то, от чего он заведён."""
    taken = []
    real = store.lock
    monkeypatch.setattr(engine.config, "lock",
                        lambda name=None: (taken.append(name), real(name))[1])
    drive = Drive([comment("c1")])
    _wire(engine, monkeypatch, drive)
    engine.reply_comment("doc1", "c1", "текст")
    capsys.readouterr()
    assert taken == [engine._document_lock_name("doc1")]


def test_the_single_form_says_when_the_guarantee_degraded(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Перепись недоступна — пауза выдержана вслепую, а коллизию проверить
    было нечем. Молчание тут читалось бы как обещание."""
    class Blind(Drive):
        def list(self, **kw):
            raise RuntimeError("перепись недоступна")

    drive = Blind([comment("c1")], about_fails=True)
    _wire(engine, monkeypatch, drive)
    engine.reply_comment("doc1", "c1", "текст")
    out = json.loads(capsys.readouterr().out)
    assert "reply_gate_unavailable" in out
    assert clock.slept and clock.slept[0] >= engine._REPLY_GATE_SECONDS


def test_the_journal_saves_the_witness_snapshot_taken_BEFORE_the_write(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Снимок «какие треды были без свидетеля до записи» обязан лечь в журнал
    вместе с `inflight`. Без него возобновление сравнит с состоянием ПОСЛЕ
    своей же записи и не увидит вреда, который само и нанесло."""
    path = _file(tmp_path, [("c1", "a")])
    # в документе уже есть пара тредов без свидетеля — снимок непустой
    drive = Drive([comment("c1", created="2026-09-01T09:00:00.000Z"),
                   comment("c8", created="2026-09-01T08:00:00.000Z"),
                   comment("c9", created="2026-09-01T08:00:00.000Z")])
    _wire(engine, monkeypatch, drive)
    _run(engine, tmp_path, capsys, path)
    rec = _journal(path)["replies"][0]
    assert rec["naked_baseline"] == ["c8", "c9"]


def test_the_baseline_is_refreshed_between_replies(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Снимок «кто без свидетеля» обязан обновляться после каждой проверки.

    Сдвинуть этот набор может только чужая рука — человек, отвечающий в UI
    между нашими записями. Здесь он даёт запертому треду свидетеля, а наша
    вторая запись снова его отнимает. Со СТАРЫМ снимком тред числился бы
    запертым с самого начала, и потеря не заметилась бы вовсе.
    """
    S = "2026-09-01T08:00:00.000Z"     # общий ключ корней c8 и c9
    U = "2026-09-01T12:34:56.000Z"     # секунда, в которую ответит человек
    drive = Drive([comment("c1", created="2026-09-01T09:00:00.000Z"),
                   comment("c2", created="2026-09-01T09:00:01.000Z"),
                   comment("c8", created=S),
                   comment("c9", created=S)],
                  stamps=["2026-09-01T11:00:00.000Z", U])

    def human_answers_in_the_ui(d, calls):
        # перепись 1 — плановая, перепись 2 — проверка первого ответа.
        # Человек успевает ответить в c8 ровно к ней: у треда появляется
        # собственный уникальный ключ, и он перестаёт быть запертым
        if calls == 2:
            for c in d.comments_payload:
                if c["id"] == "c8":
                    c.setdefault("replies", []).append(
                        {"id": "human", "createdTime": U,
                         "author": {"displayName": "Я"}})
    drive.on_list = human_answers_in_the_ui
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys,
                     _file(tmp_path, [("c1", "a"), ("c2", "b")]))
    # наша вторая запись попала в ту же секунду и снова заперла c8
    assert out["stopped_because"] == "identity_collision_created"
    assert [c[0] for c in drive.created] == ["c1", "c2"]
    assert code == 3


def test_a_429_records_pending_BEFORE_it_sleeps(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Сервер уже доказал, что запрос не применён. Умереть во время паузы с
    `inflight` в журнале значит навсегда отказаться от повтора там, где он
    безопасен."""
    order = []

    class Once(Drive):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.first = True

        def create(self, **kw):
            if self.first:
                self.first = False
                return self._Req(_http(429, retry_after="0.1"))
            return super().create(**kw)

    drive = Once([comment("c1")])
    real_write = engine._reply_journal_write
    monkeypatch.setattr(engine, "_reply_journal_write",
                        lambda p, h, r, st: (order.append(
                            ("journal", r["c1"].get("state"))),
                            real_write(p, h, r, st))[1])
    real_sleep = clock.sleep
    monkeypatch.setattr(engine.time, "sleep",
                        lambda s: (order.append(("sleep", s)), real_sleep(s))[1])
    _wire(engine, monkeypatch, drive)
    _run(engine, tmp_path, capsys, _file(tmp_path, [("c1", "a")]))
    i = order.index(("sleep", 0.1))
    assert ("journal", "pending") in order[:i]


def test_a_terminal_reply_second_unknown_survives_resuming(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Исход, который прошлый прогон объявил окончательным, окончателен и
    здесь: без читаемой секунды прошлого ответа шлюз не на чем строить."""
    path = _file(tmp_path, [("c1", "a"), ("c2", "b")])
    drive = Drive([comment("c1"), comment("c2")])
    _wire(engine, monkeypatch, drive)
    with open(path + ".skrepka-reply-journal.json", "w", encoding="utf-8") as fh:
        json.dump({"header": {
            "schema": engine._REPLY_JOURNAL_SCHEMA, "doc_id": "doc1",
            "permission_id": "pid-1", "include_foreign": False,
            "input_sha256": engine._reply_input_hash([("c1", "a"), ("c2", "b")])},
            "stopped_because": "reply_second_unknown",
            "replies": [{"comment_id": "c1", "text": "a", "state": "applied",
                         "reply_id": "r1", "collision_check": "clean"}]}, fh)
    code, out = _run(engine, tmp_path, capsys, path)
    assert drive.created == []
    assert out["stopped_because"] == "reply_second_unknown" and code == 3


def test_the_single_form_honours_output(engine, monkeypatch, tmp_path, capsys,
                                        store, clock):
    """Флаг общий для обеих форм. Принимать его и молча игнорировать хуже,
    чем не иметь вовсе."""
    drive = Drive([comment("c1")])
    _wire(engine, monkeypatch, drive)
    dest = str(tmp_path / "out.json")
    engine.reply_comment("doc1", "c1", "текст", output=dest)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["written"] == dest
    with open(dest, encoding="utf-8") as fh:
        assert json.load(fh)["comment_id"] == "c1"


def test_an_unwritable_journal_refuses_instead_of_crashing(
        engine, monkeypatch, tmp_path, capsys, store, clock):
    """Найдено живым прогоном: `/tmp` на macOS — символическая ссылка, а
    `safeio` их намеренно не следует. Падало сырым трейсбеком.

    Отказ здесь правильный — журнал и есть защита от второго ответа, — но он
    обязан быть человеческим. Первая запись журнала идёт ДО первой отправки,
    поэтому отказ никого не оставляет с половиной работы.
    """
    link = tmp_path / "через-ссылку"
    (tmp_path / "настоящий").mkdir()
    link.symlink_to(tmp_path / "настоящий")
    path = _file(tmp_path, [("c1", "a")])
    moved = str(link / "replies.json")
    os.replace(path, moved)
    drive = Drive([comment("c1")])
    _wire(engine, monkeypatch, drive)
    code, out = _run(engine, tmp_path, capsys, moved)
    assert drive.created == []          # ни одного ответа не ушло
    assert out["reason"] == "reply_journal_mismatch" and code == 1
    assert "символическую ссылку" in out["error"]
