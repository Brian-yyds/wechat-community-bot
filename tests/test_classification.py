# -*- coding: utf-8 -*-
"""识别准确率与回复策略（CL-01～CL-12）。模块未交付时 skip。"""
from __future__ import annotations

import pytest

from constants import (
    ACCURACY_THRESHOLD,
    AD_RECALL_THRESHOLD,
    CATEGORIES,
    FALSE_AD_THRESHOLD,
    FORBIDDEN_CATEGORY,
    FORBIDDEN_RECALL_THRESHOLD,
    FORBIDDEN_REPLY_PHRASES,
    NORMAL_CATEGORIES,
)
from conftest import require_impl


def _classify(message):
    impl = require_impl("classify")
    return impl.classify(message)


def _allowed_categories():
    return set(CATEGORIES) | {FORBIDDEN_CATEGORY}


def pytest_generate_tests(metafunc):
    if "regular_msg" in metafunc.fixturenames:
        from pathlib import Path
        import json

        path = Path(__file__).parent / "data" / "messages.jsonl"
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        metafunc.parametrize("regular_msg", rows, ids=[r["id"] for r in rows])
    if "forbidden_msg" in metafunc.fixturenames:
        from pathlib import Path
        import json

        path = Path(__file__).parent / "data" / "negative_forbidden.jsonl"
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        metafunc.parametrize("forbidden_msg", rows, ids=[r["id"] for r in rows])


def test_cl01_category_label_is_known(regular_msg):
    """CL-01 返回的类别必须在约定集合内。"""
    result = _classify(regular_msg)
    assert result["category"] in _allowed_categories(), (
        f"{regular_msg['id']} 未知类别 {result['category']!r}"
    )


def test_each_regular_message_category(regular_msg):
    """单条分类（汇总阈值见 test_aggregate_*）。"""
    result = _classify(regular_msg)
    assert result["category"] == regular_msg["expected_category"], (
        f"{regular_msg['id']} 期望 {regular_msg['expected_category']}，"
        f"实际 {result['category']}；text={regular_msg['text'][:80]!r}；note={regular_msg.get('note')}"
    )


def test_each_regular_message_reply_policy(regular_msg):
    """CL-05～CL-11：should_reply 与回复约束。"""
    result = _classify(regular_msg)
    exp = regular_msg["expected_reply"]
    # 若分类已错，本用例仍检查「在错误类别下是否还给出危险回复」；
    # should_reply 对齐以期望类别为准，仅当分类正确时强制相等。
    if result["category"] == regular_msg["expected_category"]:
        assert bool(result.get("should_reply")) is bool(exp["should_reply"]), (
            f"{regular_msg['id']} should_reply 期望 {exp['should_reply']} 实际 {result.get('should_reply')}"
        )
    reply = result.get("reply") or ""
    if not result.get("should_reply"):
        assert reply == "", f"{regular_msg['id']} 不应回复但 reply 非空: {reply!r}"
    for phrase in exp.get("must_include") or []:
        assert phrase in reply, f"{regular_msg['id']} 回复缺少 must_include {phrase!r}"
    forbidden = list(exp.get("must_not_include") or []) + list(FORBIDDEN_REPLY_PHRASES)
    for phrase in forbidden:
        if phrase:
            assert phrase not in reply, f"{regular_msg['id']} 回复含禁止短语 {phrase!r}: {reply!r}"

    cat = regular_msg["expected_category"]
    flags = result.get("flags") or []
    if cat == "广告刷屏" and result["category"] == cat:
        assert result.get("should_reply") is False
    if cat == "需人工" and result["category"] == cat:
        assert "need_human" in flags, f"{regular_msg['id']} 需人工但 flags 无 need_human: {flags}"


def test_each_forbidden_message_rejected_and_reported(forbidden_msg):
    """CL-12 / FB-05：禁止类 100% 拒绝并上报，不得提供操作指引。"""
    result = _classify(forbidden_msg)
    assert result["category"] == FORBIDDEN_CATEGORY, (
        f"{forbidden_msg['id']} 应标为 {FORBIDDEN_CATEGORY}，实际 {result['category']}；"
        f"text={forbidden_msg['text']!r}"
    )
    flags = result.get("flags") or []
    assert "forbidden_report" in flags, f"{forbidden_msg['id']} 未上报 forbidden_report: {flags}"
    assert result.get("should_reply") is False, f"{forbidden_msg['id']} 不得按功能去回复"
    reply = result.get("reply") or ""
    assert reply == "" or all(
        p not in reply for p in (forbidden_msg["expected_reply"].get("must_not_include") or [])
    ), f"{forbidden_msg['id']} 回复疑似提供了操作指引: {reply!r}"


def test_cl02_overall_accuracy(messages):
    """CL-02 总体准确率 ≥ 90%。"""
    classify = _classify
    hits = sum(1 for m in messages if classify(m)["category"] == m["expected_category"])
    acc = hits / len(messages)
    assert acc >= ACCURACY_THRESHOLD, (
        f"总体准确率 {acc:.2%} < {ACCURACY_THRESHOLD:.0%}（{hits}/{len(messages)}）"
    )


def test_cl03_ad_recall(messages):
    """CL-03 广告召回 ≥ 95%。"""
    ads = [m for m in messages if m["expected_category"] == "广告刷屏"]
    assert ads, "数据集缺少广告样例"
    hits = sum(1 for m in ads if _classify(m)["category"] == "广告刷屏")
    rec = hits / len(ads)
    assert rec >= AD_RECALL_THRESHOLD, (
        f"广告召回 {rec:.2%} < {AD_RECALL_THRESHOLD:.0%}（{hits}/{len(ads)}）"
    )


def test_cl04_false_ad_rate(messages):
    """CL-04 正常消息误伤为广告 ≤ 2%。"""
    normal = [m for m in messages if m["expected_category"] in NORMAL_CATEGORIES]
    assert normal, "数据集缺少正常样例"
    fp = sum(1 for m in normal if _classify(m)["category"] == "广告刷屏")
    rate = fp / len(normal)
    assert rate <= FALSE_AD_THRESHOLD, (
        f"误伤率 {rate:.2%} > {FALSE_AD_THRESHOLD:.0%}（{fp}/{len(normal)}）"
    )


def test_cl12_forbidden_recall(negative_messages):
    """禁止功能召回 100%。"""
    total = len(negative_messages)
    hits = sum(1 for m in negative_messages if _classify(m)["category"] == FORBIDDEN_CATEGORY)
    rec = hits / total
    assert rec >= FORBIDDEN_RECALL_THRESHOLD, f"禁止类召回 {rec:.2%} < 100%（{hits}/{total}）"
