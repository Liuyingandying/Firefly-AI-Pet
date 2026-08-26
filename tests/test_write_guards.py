"""Red-line filter tests: hard-block credential, identity, and payment content."""

from __future__ import annotations

import pytest

from memory.records import WritePolicy
from memory.service import MemoryService
from memory.write_guards import (
    RedLineCategory,
    RedLineViolationError,
    authorize_explicit_write,
    check_red_line,
    detect_red_line,
)


def _valid_id_card() -> str:
    prefix = "11010519491231002"
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check = "10X98765432"
    total = sum(int(d) * w for d, w in zip(prefix, weights))
    return prefix + check[total % 11]


class CountingRepository:
    def __init__(self):
        self.add_count = 0
        self._records = {}

    def add(self, record):
        self.add_count += 1
        self._records[record.id] = record
        return record.id

    def get(self, record_id):
        return self._records.get(record_id)

    def list(self, **kwargs):
        return list(self._records.values())

    def update(self, record_id, patch):
        return self._records[record_id]

    def delete(self, record_id):
        return False

    def clear(self):
        return 0

    def all_ids(self):
        return set(self._records)

    def export(self):
        return {}

    def import_data(self, data):
        return 0


class CountingAdapter:
    def __init__(self):
        self.add_count = 0

    def add(self, text, metadata=None):
        self.add_count += 1
        return "vec-1"

    def search(self, query, *, limit=5):
        return []

    def delete(self, vector_id):
        return True


# --- allowed content -------------------------------------------------------


def test_ordinary_project_memory_is_allowed():
    content = "我在做 Firefly 项目，用 PySide6 和记忆模块"
    assert check_red_line(content) == content


def test_ordinary_preference_is_allowed():
    assert detect_red_line("我喜欢简洁的代码风格") is None


def test_authorize_explicit_write_allows_ordinary_memory():
    content = authorize_explicit_write("记住我在做 Firefly 项目", WritePolicy.EXPLICIT_ONLY)
    assert content is not None


# --- blocked categories ----------------------------------------------------


def test_api_key_is_rejected():
    violation = detect_red_line("我的 API key 是 sk-abcdef1234567890abcdef")
    assert violation is not None
    assert violation.category is RedLineCategory.API_KEY


def test_access_token_jwt_is_rejected():
    violation = detect_red_line(
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sigvalue123456"
    )
    assert violation is not None
    assert violation.category is RedLineCategory.ACCESS_TOKEN


def test_bearer_credential_is_rejected():
    violation = detect_red_line("Bearer abcdef1234567890abcdef123456")
    assert violation is not None
    assert violation.category is RedLineCategory.ACCESS_TOKEN


def test_password_assignment_is_rejected():
    violation = detect_red_line("password=hunter2secret")
    assert violation is not None
    assert violation.category is RedLineCategory.PASSWORD_SECRET


def test_secret_assignment_is_rejected():
    violation = detect_red_line("secret: s3cret-value")
    assert violation is not None
    assert violation.category is RedLineCategory.PASSWORD_SECRET


def test_private_key_is_rejected():
    violation = detect_red_line(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA..."
    )
    assert violation is not None
    assert violation.category is RedLineCategory.PRIVATE_KEY


def test_identity_number_is_rejected():
    violation = detect_red_line(f"我的身份证号是 {_valid_id_card()}")
    assert violation is not None
    assert violation.category is RedLineCategory.IDENTITY


def test_payment_card_is_rejected():
    violation = detect_red_line("银行卡号是 4242424242424242")
    assert violation is not None
    assert violation.category is RedLineCategory.PAYMENT


# --- placeholders / discussion are NOT blocked -----------------------------


def test_placeholder_api_key_is_not_blocked():
    assert detect_red_line("api_key=YOUR_API_KEY_HERE") is None


def test_placeholder_token_is_not_blocked():
    assert detect_red_line("token=<token>") is None


def test_api_key_discussion_is_not_blocked():
    assert detect_red_line("API key 是用于鉴权的字符串") is None


def test_password_discussion_without_value_is_not_blocked():
    assert detect_red_line("我的密码很重要，要妥善保管") is None


# --- the write gate blocks all persistence ---------------------------------


def test_authorize_explicit_write_raises_on_red_line():
    with pytest.raises(RedLineViolationError):
        authorize_explicit_write(
            "记住 password=hunter2secret", WritePolicy.EXPLICIT_ONLY
        )


def test_red_line_blocks_repository_and_mem0_writes():
    repo = CountingRepository()
    adapter = CountingAdapter()
    service = MemoryService(repo, adapter)

    with pytest.raises(RedLineViolationError):
        service.remember("sk-abcdef1234567890abcdef", asserted_explicit=True)

    assert repo.add_count == 0
    assert adapter.add_count == 0


def test_clean_content_still_writes():
    repo = CountingRepository()
    adapter = CountingAdapter()
    service = MemoryService(repo, adapter)

    record = service.remember("我喜欢猫", asserted_explicit=True)

    assert record is not None
    assert repo.add_count == 1
    assert adapter.add_count == 1
