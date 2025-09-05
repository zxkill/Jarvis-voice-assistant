"""Тесты адаптации политики проактивности на основе отзывов."""

import pathlib
import sys

# Добавляем корневой каталог проекта в PYTHONPATH, чтобы импортировать пакет
sys.path.append(str(pathlib.Path(__file__).resolve().parents[2]))

import proactive.policy as policy


def test_policy_becomes_less_active_on_rejections():
    """Если подсказки часто отклоняются, интервал увеличивается, а лимит падает."""

    cfg = policy.PolicyConfig(suggestion_min_interval_min=1, daily_limit=5)
    pol = policy.Policy(cfg)
    pol.adapt_from_feedback({"accepted": 0.2, "rejected": 0.8})
    assert pol.config.suggestion_min_interval_min == 2
    assert pol.config.daily_limit == 4


def test_policy_becomes_more_active_on_acceptance():
    """При высокой доле принятий интервал уменьшается и лимит растёт."""

    cfg = policy.PolicyConfig(suggestion_min_interval_min=2, daily_limit=3)
    pol = policy.Policy(cfg)
    pol.adapt_from_feedback({"accepted": 0.9, "rejected": 0.1})
    assert pol.config.suggestion_min_interval_min == 1
    assert pol.config.daily_limit == 4


def test_policy_adaptation_skips_without_feedback():
    """Отсутствие отзывов не должно менять настройки политики."""

    cfg = policy.PolicyConfig(suggestion_min_interval_min=1, daily_limit=2)
    pol = policy.Policy(cfg)
    pol.adapt_from_feedback({"accepted": 0.0, "rejected": 0.0})
    assert pol.config.suggestion_min_interval_min == 1
    assert pol.config.daily_limit == 2

