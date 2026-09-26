import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from model.drawing_filter import (
    select_files, is_unit_wiring_title,
    FILTER_UNIT_ONLY, FILTER_UNIT_EXCLUDED, FILTER_ALL,
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


def test_unit_excluded_includes_blank_title():
    """タイトル空欄（未抽出）は『UNIT内結線図以外』に入る。"""
    assert select_files(TITLES, FILTER_UNIT_EXCLUDED) == ['b3', 'b4']


def test_all_returns_every_key_in_order():
    assert select_files(TITLES, FILTER_ALL) == ['b1', 'b2', 'b3', 'b4', 'b5']


def test_partial_title_is_not_unit_wiring():
    """完全一致のみ（『UNIT内結線図(1)』等の部分一致は対象外、従来仕様どおり）。"""
    assert not is_unit_wiring_title('UNIT内結線図(1)')


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        select_files(TITLES, 'xxx')
