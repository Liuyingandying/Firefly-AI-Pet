# -*- coding: utf-8 -*-
"""M2.7 TJULLM Function Calling Adapter 验收测试。

覆盖：tool schema JSON 合法 / tool call 解析 / 参数校验 / 成功调用
（真实 Learning API，无 TJULLM 网络调用）/ 非法工具拒绝 / JSON 序列化,
另加安全契约（白名单/文件允许根/无动态执行）。
隔离：import_textbook 用 tmp 教材 + 注入允许根; run_experiment 用真实
本地 runner（~2s）; 不接真实 TJULLM、不修改任何 learning 模块。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.learning.tools import (
    ERR_INVALID_ARGUMENTS,
    ERR_INVALID_CALL,
    ERR_INVALID_PARAM,
    ERR_MISSING_PARAM,
    ERR_NOT_FOUND,
    ERR_PATH_DENIED,
    ERR_UNKNOWN_TOOL,
    TOOL_GENERATE_LEARNING_PLAN,
    TOOL_IMPORT_TEXTBOOK,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
    build_default_registry,
    all_tool_schemas,
    configure_allowed_roots,
    execute_tool_call,
    parse_tool_call,
    tool_schema,
)

TEXTBOOK_MD = """# 电磁场导论

引言。

## 均匀平面波

平面波正文，含公式 $k = 2\\pi/\\lambda$。

# 天线基础

天线正文。
"""


@pytest.fixture(scope="module")
def registry():
    return build_default_registry()


@pytest.fixture(scope="module")
def textbook_dir(tmp_path_factory):
    base = tmp_path_factory.mktemp("m27_textbooks")
    (base / "textbook.md").write_text(TEXTBOOK_MD, encoding="utf-8")
    return base


@pytest.fixture(scope="module")
def allowed(textbook_dir):
    """把允许根收紧到 tmp 教材目录（安全边界注入）。"""
    configure_allowed_roots([textbook_dir])
    yield textbook_dir
    configure_allowed_roots([Path.cwd().resolve()])   # 还原默认


# ---------------------------------------------------------------------------
# 1. tool schema JSON 合法（OpenAI function 格式）
# ---------------------------------------------------------------------------

def test_tool_schemas_are_valid_json_and_openai_shape():
    schemas = all_tool_schemas()
    assert len(schemas) == 4
    names = set()
    for schema in schemas:
        text = json.dumps(schema, ensure_ascii=False)          # 全 JSON 原生类型
        parsed = json.loads(text)
        assert parsed["type"] == "function"
        fn = parsed["function"]
        assert fn["name"] and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])
        names.add(fn["name"])
    assert names == {
        TOOL_IMPORT_TEXTBOOK, TOOL_QUERY_CONCEPT,
        TOOL_RUN_EXPERIMENT, TOOL_GENERATE_LEARNING_PLAN,
    }


def test_single_schema_lookup():
    schema = tool_schema(TOOL_IMPORT_TEXTBOOK)
    assert schema["function"]["parameters"]["required"] == ["path", "course_id"]
    assert tool_schema("nope") is None


# ---------------------------------------------------------------------------
# 2. tool call 解析
# ---------------------------------------------------------------------------

def test_parse_tool_call_valid():
    entry = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": TOOL_QUERY_CONCEPT,
            "arguments": json.dumps({"concept_id": "em-te-polarization"}),
        },
    }
    parsed, error = parse_tool_call(entry)
    assert error == ""
    assert parsed.call_id == "call_1"
    assert parsed.name == TOOL_QUERY_CONCEPT
    assert parsed.arguments == {"concept_id": "em-te-polarization"}


def test_parse_tool_call_failures():
    _, err = parse_tool_call(None)
    assert err.startswith("invalid_tool_call")
    _, err = parse_tool_call({"id": "x", "type": "other", "function": {"name": "f"}})
    assert err.startswith(ERR_INVALID_CALL)
    _, err = parse_tool_call({"id": "x", "type": "function", "function": {"name": ""}})
    assert err.startswith(ERR_INVALID_CALL)
    _, err = parse_tool_call({
        "id": "x", "type": "function",
        "function": {"name": TOOL_QUERY_CONCEPT, "arguments": "{not json"},
    })
    assert err.startswith(ERR_INVALID_ARGUMENTS)


def test_parse_tolerates_dict_arguments():
    entry = {"id": "x", "type": "function",
             "function": {"name": TOOL_QUERY_CONCEPT, "arguments": {"concept_id": "c"}}}
    parsed, error = parse_tool_call(entry)
    assert error == "" and parsed.arguments == {"concept_id": "c"}


# ---------------------------------------------------------------------------
# 3. 参数校验
# ---------------------------------------------------------------------------

def test_missing_required_parameter(registry):
    result = registry.execute(TOOL_IMPORT_TEXTBOOK, {"path": "a.md"})
    assert result.success is False
    assert result.error.startswith(ERR_MISSING_PARAM)
    assert "course_id" in result.error


def test_wrong_parameter_type(registry):
    result = registry.execute(TOOL_QUERY_CONCEPT, {"concept_id": 123})
    assert result.success is False
    assert result.error.startswith(ERR_INVALID_PARAM)


def test_unknown_parameter_rejected(registry):
    result = registry.execute(TOOL_QUERY_CONCEPT, {"concept_id": "c", "evil": True})
    assert result.success is False
    assert result.error.startswith(ERR_INVALID_PARAM)


# ---------------------------------------------------------------------------
# 4. 成功调用（真实 Learning API）
# ---------------------------------------------------------------------------

def test_import_textbook_success(allowed):
    result = execute_tool_call({
        "id": "call_imp", "type": "function",
        "function": {
            "name": TOOL_IMPORT_TEXTBOOK,
            "arguments": json.dumps({
                "path": str(allowed / "textbook.md"),
                "course_id": "course-tjullm",
            }),
        },
    })
    assert result.success is True
    assert result.tool == TOOL_IMPORT_TEXTBOOK
    assert result.result["document_title"] == "电磁场导论"
    assert result.result["course_id"] == "course-tjullm"
    assert "1" in result.result["lessons"] and "2" in result.result["lessons"]
    assert result.result["draft"]["is_confirmed"] is False      # 不自动激活


def test_query_concept_success(registry):
    result = registry.execute(TOOL_QUERY_CONCEPT, {"concept_id": "em-te-polarization"})
    assert result.success is True
    assert result.result["concept"]["name"] == "TE 极化"
    assert result.result["learning_path"] == ["em-uniform-plane-wave", "em-te-polarization"]
    assert "uniform-plane-wave" in result.result["related_experiments"]


def test_query_concept_not_found(registry):
    result = registry.execute(TOOL_QUERY_CONCEPT, {"concept_id": "ghost"})
    assert result.success is False
    assert result.error.startswith(ERR_NOT_FOUND)


def test_run_experiment_success(registry):
    result = registry.execute(TOOL_RUN_EXPERIMENT, {
        "experiment_id": "uniform-plane-wave",
        "params": {"frequency": 2.4, "medium": "vacuum"},
    })
    assert result.success is True
    assert result.result["status"] == "completed"
    assert result.result["params"]["frequency_ghz"] == 2.4
    assert Path(result.result["artifacts"]["png"]).is_file()


def test_generate_learning_plan_success(registry):
    result = registry.execute(TOOL_GENERATE_LEARNING_PLAN,
                              {"concept_id": "em-te-polarization"})
    assert result.success is True
    assert result.result["learning_path"] == ["em-uniform-plane-wave", "em-te-polarization"]
    steps = result.result["steps"]
    assert steps[0]["concept"] == "em-uniform-plane-wave"
    assert steps[-1]["concept"] == "em-te-polarization"
    assert all("action" in s for s in steps)


# ---------------------------------------------------------------------------
# 5. 非法工具拒绝 + 安全契约
# ---------------------------------------------------------------------------

def test_unknown_tool_rejected(registry):
    result = execute_tool_call({
        "id": "x", "type": "function",
        "function": {"name": "run_python", "arguments": "{\"code\": \"import os\"}"},
    })
    assert result.success is False
    assert result.error.startswith(ERR_UNKNOWN_TOOL)        # 白名单外结构上不可调用


def test_path_outside_allowed_roots_denied(allowed):
    result = execute_tool_call({
        "id": "x", "type": "function",
        "function": {
            "name": TOOL_IMPORT_TEXTBOOK,
            "arguments": json.dumps({"path": "C:/Windows/win.ini", "course_id": "c"}),
        },
    })
    assert result.success is False
    assert result.error.startswith(ERR_PATH_DENIED)


def test_static_no_dynamic_execution():
    """静态契约: tools 包三个模块无 eval/exec/__import__。"""
    import core.learning.tools.adapters as adapters_mod
    import core.learning.tools.schema as schema_mod
    import core.learning.tools.tool_registry as registry_mod
    import inspect

    for module in (adapters_mod, schema_mod, registry_mod):
        source = inspect.getsource(module)
        assert "eval(" not in source, module.__name__
        assert "exec(" not in source, module.__name__
        assert "__import__" not in source, module.__name__


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_tool_result_json_roundtrip(registry):
    ok = registry.execute(TOOL_QUERY_CONCEPT, {"concept_id": "em-uniform-plane-wave"})
    parsed = json.loads(ok.to_json())
    assert parsed["success"] is True
    assert parsed["tool"] == TOOL_QUERY_CONCEPT
    assert parsed["result"]["concept"]["concept_id"] == "em-uniform-plane-wave"

    failed = registry.execute("ghost_tool", {})
    parsed_fail = json.loads(failed.to_json())
    assert parsed_fail["success"] is False
    assert parsed_fail["error"].startswith(ERR_UNKNOWN_TOOL)
