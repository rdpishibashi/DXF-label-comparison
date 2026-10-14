"""このテストが守るもの: 結果プレビューの「機器符号候補」絞り込み（表示専用）の純関数。

- 選択肢は列に実際にある値だけを DEF → CAN → （空欄）の順に返す
- 全選択なら元の表と同じ行、0選択なら0行、index は保持（行の色付けが元の df を index で引くため）
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from app import candidate_filter_options, filter_by_candidate  # noqa: E402

BLANK = '（空欄）'


def _df():
    return pd.DataFrame({
        '機器符号候補': ['CAN', None, 'DEF', '', 'CAN', float('nan')],
        'ラベル': list('abcdef'),
    }, index=[10, 11, 12, 13, 14, 15])


def test_options_order_and_blank_merge():
    assert candidate_filter_options(_df()['機器符号候補']) == ['DEF', 'CAN', BLANK]


def test_options_only_present_values():
    assert candidate_filter_options(pd.Series(['CAN', 'CAN'])) == ['CAN']
    assert candidate_filter_options(pd.Series([None, ''])) == [BLANK]


def test_select_all_returns_same_rows():
    df = _df()
    out = filter_by_candidate(df, ['DEF', 'CAN', BLANK])
    assert list(out.index) == list(df.index)


def test_select_def_can_drops_blank_rows_keeps_index():
    out = filter_by_candidate(_df(), ['DEF', 'CAN'])
    assert list(out['ラベル']) == ['a', 'c', 'e']
    assert list(out.index) == [10, 12, 14]


def test_select_blank_only():
    assert list(filter_by_candidate(_df(), [BLANK])['ラベル']) == ['b', 'd', 'f']


def test_select_none_returns_empty():
    assert filter_by_candidate(_df(), []).empty
