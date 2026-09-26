import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from model.compare_labels import (
    DIFF_COLUMNS, REGION_DIFF_COLUMNS, compare_key, aggregate_labels, aggregate_region_labels,
    compare_labels, summarize, summarize_metrics, summary_header,
    compare_labels_by_region, build_region_summary_rows, blank_repeated_column,
    row_style, ROW_STYLE_A_ONLY, ROW_STYLE_B_ONLY, ROW_STYLE_MATCH, ROW_STYLE_MISMATCH,
)


def _row(df, label):
    return df[df['ラベル'] == label].iloc[0]


def test_basic_kubun_assignment():
    """A のみ／B のみ／両方の区分と、無い側の個数が pd.NA になることを守る。"""
    df = compare_labels({'CB1': 2, 'X': 1}, {'CB1': 2, 'Y': 3})
    assert list(df.columns) == DIFF_COLUMNS
    assert list(df['ラベル']) == ['CB1', 'X', 'Y']
    assert _row(df, 'CB1')['区分'] == '両方'
    assert _row(df, 'X')['区分'] == 'A のみ' and pd.isna(_row(df, 'X')['B個数'])
    assert _row(df, 'Y')['区分'] == 'B のみ' and pd.isna(_row(df, 'Y')['A個数'])


def test_candidate_column_uses_ref_designator_judgment():
    """機器符号候補列は DXF-extract-labels と同じ判定（Y／None）を付ける。"""
    df = compare_labels({'CB001': 1, '電源': 1}, {})
    assert _row(df, 'CB001')['機器符号候補'] == 'Y'
    assert _row(df, '電源')['機器符号候補'] is None


@pytest.mark.parametrize('label, expected', [
    ('R10(2.2K)', 'R10'),
    ('FBWH（白）', 'FBWH'),
    ('MSS (MOTOR)', 'MSS'),
    ('CB1', 'CB1'),
    ('(BU)', '(BU)'),
    ('（白）', '（白）'),
    (' (BK)', ' (BK)'),
])
def test_compare_key_strips_after_parenthesis(label, expected):
    """比較キーは半角/全角の括弧以降（直前の空白含む）を除去する。"""
    assert compare_key(label) == expected


def test_aggregate_labels_merges_by_compare_key_across_files():
    """複数ファイルの行を比較キーで合算する（括弧違いは同一ラベル）。"""
    results = {
        'f1': {'rows': [{'ラベル': 'R10(2.2K)', '個数': 1}, {'ラベル': 'CB1', '個数': 2}]},
        'f2': {'rows': [{'ラベル': 'R10', '個数': 3}]},
        'f3': {'rows': [{'ラベル': 'X1', '個数': 9}]},
    }
    assert aggregate_labels(results, ['f1', 'f2']) == {'R10': 4, 'CB1': 2}


def test_aggregate_region_labels_only_selected_regions():
    """指定した領域名だけを集計し、該当が無い領域は空dictで返す。"""
    results = {
        'f1': {'region_label_counts': {'BOX': {'CB1(A)': 1, 'CB1': 1}, 'OTHER': {'X': 1}}},
        'f2': {'region_label_counts': {'BOX': {'CB1': 2}}},
    }
    agg = aggregate_region_labels(results, ['f1', 'f2'], ['BOX', 'NONE'])
    assert agg == {'BOX': {'CB1': 4}, 'NONE': {}}


def test_summarize_layout():
    """Summary の項目順: ヘッダー（ファイル数・B絞り込み）→区分カウント。"""
    df = compare_labels({'A': 1, 'B': 1}, {'B': 1, 'C': 1})
    s = summarize(df, summary_header(3, 5, 'UNIT内結線図のみ', 2))
    assert list(s) == ['A ファイル数', 'B ファイル数', 'B 絞り込み条件', 'B 対象ファイル数',
                       'A ユニークラベル数', 'B ユニークラベル数', 'A のみ', 'B のみ', '両方']
    assert (s['A ファイル数'], s['B ファイル数'], s['B 対象ファイル数']) == (3, 5, 2)
    assert (s['A のみ'], s['B のみ'], s['両方']) == (1, 1, 1)
    assert summarize_metrics(df) == {k: s[k] for k in list(s)[4:]}


def test_compare_labels_by_region():
    """領域ごとに比較し領域名列付きで連結する。無い領域は空扱い。"""
    df, metrics = compare_labels_by_region(
        {'R1': {'A': 1}, 'R2': {'B': 1}}, {'R1': {'A': 1}}, ['R2', 'R1', 'R3'])
    assert list(df.columns) == REGION_DIFF_COLUMNS
    assert list(df['領域名']) == ['R2', 'R1']
    assert list(metrics) == ['R2', 'R1', 'R3']
    assert metrics['R2']['A のみ'] == 1 and metrics['R1']['両方'] == 1
    assert metrics['R3']['A ユニークラベル数'] == 0


def test_compare_labels_by_region_empty_regions():
    df, metrics = compare_labels_by_region({}, {}, [])
    assert list(df.columns) == REGION_DIFF_COLUMNS and len(df) == 0 and metrics == {}


def test_build_region_summary_rows_layout():
    """ヘッダー行（領域名空欄）→領域ごとの5行ブロック（先頭行だけ領域名）。"""
    _, metrics = compare_labels_by_region({'R1': {'A': 1}}, {'R1': {}}, ['R1'])
    rows = build_region_summary_rows(metrics, summary_header(1, 2, '全部', 2))
    assert [r['項目'] for r in rows[:4]] == [
        'A ファイル数', 'B ファイル数', 'B 絞り込み条件', 'B 対象ファイル数']
    assert all(r['領域名'] == '' for r in rows[:4])
    assert [r['領域名'] for r in rows[4:]] == ['R1', '', '', '', '']


def test_blank_repeated_column():
    df = pd.DataFrame({'領域名': ['R1', 'R1', 'R2'], 'x': [1, 2, 3]})
    assert list(blank_repeated_column(df, '領域名')['領域名']) == ['R1', '', 'R2']


@pytest.mark.parametrize('kubun, a, b, expected', [
    ('A のみ', 1, pd.NA, ROW_STYLE_A_ONLY),
    ('B のみ', pd.NA, 1, ROW_STYLE_B_ONLY),
    ('両方', 2, 2, ROW_STYLE_MATCH),
    ('両方', 1, 2, ROW_STYLE_MISMATCH),
    ('両方', 1, pd.NA, ROW_STYLE_MISMATCH),
])
def test_row_style(kubun, a, b, expected):
    """区分・個数一致による表示スタイル（青/緑/黄/無色）。"""
    assert row_style(kubun, a, b) == expected
