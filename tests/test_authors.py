"""Фильтр авторов тега-триггера: резолв email->uid, мультиаккаунт, кеш, сверка автора."""

from types import SimpleNamespace

from analyzer import pipeline
from analyzer.users import UserMap, active_email_uids


# ---------- users.active_email_uids ----------

def test_active_email_uids_multi_account_and_dismissed():
    users = [
        {"email": "a@x.ru", "uid": "1", "dismissed": False},
        {"email": "A@x.ru", "uid": "2", "dismissed": False},   # тот же email (регистр) — второй аккаунт
        {"email": "a@x.ru", "uid": "3", "dismissed": True},    # уволен — игнор
        {"email": "b@x.ru", "trackerUid": "9", "dismissed": False},  # uid берётся из trackerUid
        {"email": "", "uid": "4", "dismissed": False},          # без email — игнор
    ]
    idx = active_email_uids(users)
    assert idx["a@x.ru"] == ["1", "2"]
    assert idx["b@x.ru"] == ["9"]
    assert "" not in idx


# ---------- users.UserMap (файловая таблица соответствия) ----------

def test_usermap_cache_roundtrip_and_lazy_fetch(tmp_path):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return [{"email": "boss@x.ru", "uid": "842", "dismissed": False},
                {"email": "boss@x.ru", "uid": "999", "dismissed": False}]

    cache = tmp_path / "user_map.json"
    res = UserMap(cache).resolve(["boss@x.ru"], fake_fetch)
    assert res == {"boss@x.ru": ["842", "999"]}
    assert calls["n"] == 1
    assert cache.exists()
    # второй инстанс читает кеш и не дёргает трекер
    res2 = UserMap(cache).resolve(["boss@x.ru"], fake_fetch)
    assert res2 == {"boss@x.ru": ["842", "999"]}
    assert calls["n"] == 1


def test_usermap_unresolved_email(tmp_path):
    res = UserMap(tmp_path / "m.json").resolve(
        ["ghost@x.ru"], lambda: [{"email": "real@x.ru", "uid": "1", "dismissed": False}])
    assert res == {}


# ---------- pipeline._tag_ids ----------

def test_tag_ids_handles_str_and_dict():
    assert pipeline._tag_ids(["t1", {"id": "t2"}, {"display": "t3"}]) == {"t1", "t2", "t3"}
    assert pipeline._tag_ids(None) == set()


# ---------- pipeline._trigger_set_by_allowed ----------

def _ctx(tmp_path, changelog, users):
    tracker = SimpleNamespace(
        get_changelog=lambda key, field=None: changelog,
        get_users=lambda: users,
    )
    return SimpleNamespace(tracker=tracker, project_root=tmp_path,
                           acfg=SimpleNamespace(paths=SimpleNamespace(work_dir="w")))


def _changelog(*add_events):
    """add_events: (uid, from_tags, to_tags) — по одному изменению тегов на запись."""
    return [{"updatedBy": {"id": uid, "display": f"user{uid}"},
             "fields": [{"field": {"id": "tags"}, "from": frm, "to": to}]}
            for uid, frm, to in add_events]


def test_empty_allowlist_passes_anyone(tmp_path):
    assert pipeline._trigger_set_by_allowed(_ctx(tmp_path, [], []), "ONE-1", "TT", []) is True


def test_allowed_by_email(tmp_path):
    users = [{"email": "boss@x.ru", "uid": "842", "dismissed": False},
             {"email": "boss@x.ru", "uid": "999", "dismissed": False}]
    ctx = _ctx(tmp_path, _changelog(("842", [], ["TT"])), users)
    assert pipeline._trigger_set_by_allowed(ctx, "ONE-1", "TT", ["boss@x.ru"]) is True


def test_matches_second_account_of_same_person(tmp_path):
    users = [{"email": "boss@x.ru", "uid": "842", "dismissed": False},
             {"email": "boss@x.ru", "uid": "999", "dismissed": False}]
    ctx = _ctx(tmp_path, _changelog(("999", [], ["TT"])), users)  # тегнул из второго аккаунта
    assert pipeline._trigger_set_by_allowed(ctx, "ONE-1", "TT", ["boss@x.ru"]) is True


def test_rejected_when_other_author(tmp_path):
    users = [{"email": "boss@x.ru", "uid": "842", "dismissed": False}]
    ctx = _ctx(tmp_path, _changelog(("777", [], ["TT"])), users)
    assert pipeline._trigger_set_by_allowed(ctx, "ONE-1", "TT", ["boss@x.ru"]) is False


def test_added_at_creation_is_skipped(tmp_path):
    # тег есть, но события добавления в истории нет -> автора не определить -> пропуск
    ctx = _ctx(tmp_path, [], [{"email": "boss@x.ru", "uid": "842", "dismissed": False}])
    assert pipeline._trigger_set_by_allowed(ctx, "ONE-1", "TT", ["boss@x.ru"]) is False


def test_allowed_by_uid_literal(tmp_path):
    ctx = _ctx(tmp_path, _changelog(("842", [], ["TT"])), [])
    assert pipeline._trigger_set_by_allowed(ctx, "ONE-1", "TT", ["842"]) is True


def test_last_add_author_wins(tmp_path):
    # TT: чужой добавил -> снял -> свой добавил. Берём АВТОРА ПОСЛЕДНЕГО добавления (свой).
    cl = _changelog(("777", [], ["TT"]), ("777", ["TT"], []), ("842", [], ["TT"]))
    ctx = _ctx(tmp_path, cl, [{"email": "boss@x.ru", "uid": "842", "dismissed": False}])
    assert pipeline._trigger_set_by_allowed(ctx, "ONE-1", "TT", ["boss@x.ru"]) is True
