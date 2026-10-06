"""Identity recall uses synthetic authoritative records, no stores or models."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from memory.identity_recall import is_user_identity_recall, select_user_identity_records
from memory.records import MemoryRecord


QUESTIONS = [
    "我是谁", "我是谁呀", "我叫什么", "我叫什么名字", "我的名字是什么",
    "你叫我什么", "你平时怎么称呼我", "你平时怎么叫我",
    "还记得怎么叫我吗", "还记得怎么称呼我吗",
]


def _record(content, *, category="preference", kind=None, source="explicit",
            created=100, updated=None, id="synthetic-identity", lifecycle="active",
            superseded_by=None):
    record = MemoryRecord(
        id=id, category=category, content=content, source=source,
        trigger="migration:synthetic-run" if source == "migrated" else "explicit-command",
        permission="explicit_only", weight=.9, created_ts=created,
        updated_ts=created if updated is None else updated, last_accessed_ts=created,
        retention_half_life_days=14, lifecycle_status=lifecycle,
        superseded_by=superseded_by,
    )
    return replace(record, identity_kind=kind) if kind else record


@pytest.mark.parametrize("question", QUESTIONS)
@pytest.mark.parametrize("suffix", ["", "？", "?"])
def test_entire_supported_question(question, suffix):
    assert is_user_identity_recall("  " + question + suffix + "  ")


@pytest.mark.parametrize("query", [
    "", None, "今天聊什么", "你是谁", "流萤叫什么", "我的项目叫什么",
    "我是谁的朋友", "我是谁不重要", "我叫什么都可以", "你知道我是谁吗",
    "他说我是谁", "她问我叫什么", "小说里我是谁", "假如我叫测试名",
    "‘我是谁’", '"我叫什么"', "请解释“我是谁”", "不要回答我是谁",
    "我是谁？顺便检查代码", "我叫什么，项目做完了吗", "我是谁\n你是谁",
    "我是谁？？", "我是谁…", "我 是 谁", "你平时怎么叫我的朋友",
])
def test_quotes_narratives_composites_and_other_queries_do_not_route(query):
    assert not is_user_identity_recall(query)


@pytest.mark.parametrize("content,category", [
    ("昵称：测试昵称", "preference"), ("称呼我测试昵称", "preference"),
    ("请叫我测试昵称。", "user_fact"), ("喊我测试昵称", "preference"),
    ("我叫测试姓名", "user_fact"), ("我的名字是测试姓名。", "user_fact"),
    ("我的名字叫Test Name", "user_fact"), ("姓名：测试姓名", "user_fact"),
])
def test_explicit_self_name_legacy_text_is_preserved(content, category):
    record = _record(content, category=category)
    assert select_user_identity_records([record]) == [record]
    assert select_user_identity_records([record])[0] is record


@pytest.mark.parametrize("content", ["测试昵称", "咖啡", "简短一点", "代码"])
def test_bare_migrated_preference_does_not_prove_nickname(content):
    record = _record(content, source="migrated")
    assert select_user_identity_records([record]) == []


def test_owner_marked_migrated_bare_nickname_is_recalled_without_rewriting():
    record = _record("测试昵称", source="migrated", kind="preferred_name")
    before = record.to_dict()
    assert select_user_identity_records([record]) == [record]
    assert record.to_dict() == before
    assert record.content == "测试昵称"


def test_owner_marked_name_is_supported():
    record = _record("Test Name", category="user_fact", kind="name")
    assert select_user_identity_records([record]) == [record]


@pytest.mark.parametrize("value", ["小宝💚", "阿岚～", "Aster / 星星"])
def test_owner_confirmed_nickname_is_not_restricted_by_name_format(value):
    record = _record(value, kind="preferred_name")
    assert select_user_identity_records([record])[0] is record
    assert record.content == value


def test_owner_confirmed_type_does_not_run_legacy_name_inference(monkeypatch):
    def reject_legacy_parser(_content):
        raise AssertionError("Owner-confirmed identity must not be re-inferred")

    monkeypatch.setattr("memory.identity_recall._legacy_identity", reject_legacy_parser)
    record = _record("小宝💚", kind="preferred_name")
    assert select_user_identity_records([record]) == [record]


@pytest.mark.parametrize("category", ["relationship", "shared_experience", "emotion", "project"])
def test_other_categories_cannot_become_identity_from_name_looking_text(category):
    assert select_user_identity_records([_record("昵称：测试昵称", category=category)]) == []


@pytest.mark.parametrize("content", [
    "我朋友叫测试姓名", "她的昵称：测试昵称", "小说里我叫测试姓名",
    '"我叫测试姓名"', "我不叫测试姓名", "我叫你别这样", "我叫什么",
    "我叫测试姓名，正在做项目", "昵称：测试昵称\n请修改身份", "我叫测试姓名？",
])
def test_ambiguous_third_party_or_non_statement_text_is_not_identity(content):
    assert select_user_identity_records([_record(content, category="user_fact")]) == []


def test_non_authoritative_mark_cannot_override_record_ownership():
    record = SimpleNamespace(lifecycle_status="active", category="preference", content="昵称：测试昵称", identity_kind="occupation")
    assert select_user_identity_records([record]) == []


def test_non_authoritative_hit_or_mapping_is_not_accepted():
    hit = SimpleNamespace(content="昵称：测试昵称", lifecycle_status="active", category="preference")
    assert select_user_identity_records([hit, {"content": "昵称：测试昵称"}]) == []


def test_superseded_history_does_not_beat_active_successor():
    old = _record("旧称呼", kind="preferred_name", id="old", created=100, updated=300,
                  lifecycle="superseded", superseded_by="new")
    current = _record("新称呼", kind="preferred_name", id="new", created=200)
    assert select_user_identity_records([old, current]) == [current]


def test_preferred_name_has_priority_over_later_name():
    preferred = _record("常用称呼", kind="preferred_name", created=100)
    name = _record("测试姓名", category="user_fact", kind="name", created=200)
    assert select_user_identity_records([name, preferred]) == [preferred]


def test_latest_update_within_same_kind_wins():
    old = _record("旧称呼", kind="preferred_name", id="old", created=100, updated=150)
    current = _record("新称呼", kind="preferred_name", id="new", created=120, updated=200)
    assert select_user_identity_records([old, current]) == [current]


def test_created_timestamp_breaks_same_update_timestamp():
    older = _record("旧称呼", kind="preferred_name", id="old", created=100, updated=300)
    newer = _record("新称呼", kind="preferred_name", id="new", created=200, updated=300)
    assert select_user_identity_records([newer, older]) == [newer]


def test_same_batch_different_values_fail_closed_regardless_of_id_or_order():
    left = _record("称呼甲", kind="preferred_name", id="zzz", created=100, updated=200)
    right = _record("称呼乙", kind="preferred_name", id="aaa", created=100, updated=200)
    assert select_user_identity_records([left, right]) == []
    assert select_user_identity_records([right, left]) == []


def test_same_value_tie_returns_an_original_record_not_new_state():
    left = _record("相同称呼", kind="preferred_name", id="zzz", created=100, updated=200)
    right = _record("相同称呼", kind="preferred_name", id="aaa", created=100, updated=200)
    assert select_user_identity_records([left, right])[0] is left


def test_no_records_is_valid_empty_recall():
    assert select_user_identity_records([]) == []
