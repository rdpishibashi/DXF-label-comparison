"""共有ファイルが DXF-extract-labels（primary）とバイト一致していることを守る。

受入条件（2026-09-26 ユーザー要望「ラベル抽出・指定領域UIは複数プロジェクトで共通化・
同時更新したい」）:
  Given DXF-extract-labels が本プロジェクトと同じ親フォルダ（Tools/）にある
  When  下記 SHARED_FILES を比較する
  Then  すべてバイト一致する（変更は primary で行い、そのままコピーする）

DXF-extract-labels がチェックアウトされていない環境（Streamlit Cloud 等）では skip する。
"""
import hashlib
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
PRIMARY_ROOT = os.path.abspath(os.path.join(PROJECT_ROOT, '..', 'DXF-extract-labels'))

SHARED_FILES = (
    'model/extract_labels.py',
    'model/common_utils.py',
    'model/ref_designator.py',
    'model/label_classifier.py',
    'model/region_detector.py',
    'model/extraction_pipeline.py',
    'view/region_selection.py',
    'config.py',
)


def _md5(path):
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


@pytest.mark.skipif(not os.path.isdir(PRIMARY_ROOT),
                    reason='DXF-extract-labels が隣にチェックアウトされていない')
@pytest.mark.parametrize('rel_path', SHARED_FILES)
def test_shared_file_identical_to_extract_labels(rel_path):
    primary = os.path.join(PRIMARY_ROOT, rel_path)
    copy = os.path.join(PROJECT_ROOT, rel_path)
    assert os.path.isfile(primary), f'primary に {rel_path} がありません'
    assert _md5(copy) == _md5(primary), (
        f'{rel_path} が DXF-extract-labels と一致しません。primary 側の変更を'
        'そのままコピーしてください（本プロジェクト側だけを変更しないこと）')
