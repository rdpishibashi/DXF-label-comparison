import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from model.drawing_filter import (
    select_files, is_unit_wiring_title,
    FILTER_UNIT_ONLY, FILTER_ALL, FILTER_OPTIONS, initial_selection, filter_condition_label,
)

TITLES = {
    'b1': 'UNIT内結線図',
    'b2': 'ＵＮＩＴ内結線図',
    'b3': '部品図',
    'b4': None,
    'b5': 'UNIT内結線図 ',
}


def test_unit_only_matches_fullwidth_and_halfwidth():
    """全角・半角・前後空白の違いは同じタイトルとして扱う。"""
    assert select_files(TITLES, FILTER_UNIT_ONLY) == ['b1', 'b2', 'b5']


def test_unit_only_excludes_blank_title():
    """タイトル空欄（未抽出）は『UNIT内結線図のみ』の対象外。"""
    assert 'b4' not in select_files(TITLES, FILTER_UNIT_ONLY)


def test_filter_options_has_only_unit_only_and_all():
    """『UNIT内結線図以外』は選択肢から削除された（旧値は ValueError）。"""
    assert FILTER_OPTIONS == (FILTER_UNIT_ONLY, FILTER_ALL)
    with pytest.raises(ValueError):
        select_files(TITLES, 'UNIT内結線図以外')


def test_initial_selection_matches_unit_only_filter():
    """手動選択の初期値は『UNIT内結線図のみ』の判定と一致する。"""
    initial = initial_selection(TITLES)
    assert [k for k, v in initial.items() if v] == select_files(TITLES, FILTER_UNIT_ONLY)


def test_filter_condition_label_marks_manual_change():
    """初期選択から変更が無ければ元の文言、変更があれば『（手動変更あり）』。『全部』は常に元の文言。"""
    initial = initial_selection(TITLES)
    assert filter_condition_label(FILTER_UNIT_ONLY, initial, dict(initial)) == FILTER_UNIT_ONLY
    changed = dict(initial, b3=True)
    assert filter_condition_label(FILTER_UNIT_ONLY, initial, changed) == 'UNIT内結線図のみ（手動変更あり）'
    assert filter_condition_label(FILTER_ALL, initial, changed) == FILTER_ALL


def test_all_returns_every_key_in_order():
    assert select_files(TITLES, FILTER_ALL) == ['b1', 'b2', 'b3', 'b4', 'b5']


def test_partial_title_is_not_unit_wiring():
    """完全一致のみ（『UNIT内結線図(1)』等の部分一致は対象外、従来仕様どおり）。"""
    assert not is_unit_wiring_title('UNIT内結線図(1)')


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        select_files(TITLES, 'xxx')
