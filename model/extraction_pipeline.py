"""1ファイル単位の領域検出・ラベル抽出（streamlit 非依存）。

複数プロジェクトで共有するモジュール（primary: DXF-extract-labels。
DXF-label-comparison にバイト一致のコピーがあり、同プロジェクトの
`tests/regression/spec/test_shared_files_identical_to_extract_labels.py`
が一致を検証する）。変更は primary で行い、コピーへそのまま伝播すること。

`extract_labels()` を呼ぶ際は `extract_drawing_numbers_option`/
`extract_title_option` を必ずセットで渡す（`Tools/CLAUDE.md` の呼び出し側
オプション整合性ルール。片方だけだと同一タイトルブロック制限が無効になる）。
"""
from .extract_labels import extract_labels
from .region_detector import analyze_dxf_regions
from . import ref_designator


def _drawing_info(dxf_path, fname):
    _, dn_info = extract_labels(
        dxf_path,
        extract_drawing_numbers_option=True,
        extract_title_option=True,
        original_filename=fname,
    )
    return dn_info


def detect_file_regions(dxf_path, fname, region_cfg):
    """1ファイルの矩形領域を検出し、図番・タイトル・サブタイトルを付与した
    解析結果（`analyze_dxf_regions()` の戻り値）を返す。"""
    analysis = analyze_dxf_regions(dxf_path, region_cfg)
    dn_info = _drawing_info(dxf_path, fname)
    analysis['main_drawing_number'] = dn_info.get('main_drawing_number')
    analysis['title'] = dn_info.get('title')
    analysis['subtitle'] = dn_info.get('subtitle')
    return analysis


def extract_file_data(dxf_path, fname, frame_lineweight, analysis=None):
    """1ファイルのラベルを抽出する（`ref_designator.extract_ref_designator_data()`）。

    `analysis`（`detect_file_regions()` の戻り値）を渡した場合（領域付きモード）は、
    図番・タイトル・サブタイトルを解析結果から引き継ぐ（再抽出しない）。
    渡さない場合（通常モード）は `extract_labels()` で図番・参照元図番・
    タイトル・サブタイトルを抽出する。
    """
    data = ref_designator.extract_ref_designator_data(
        dxf_path, frame_lineweight=frame_lineweight, original_filename=fname)
    if analysis is not None:
        data['main_drawing_number'] = analysis.get('main_drawing_number')
        data['title'] = analysis.get('title')
        data['subtitle'] = analysis.get('subtitle')
    else:
        dn_info = _drawing_info(dxf_path, fname)
        data['main_drawing_number'] = dn_info.get('main_drawing_number')
        data['source_drawing_number'] = dn_info.get('source_drawing_number')
        data['title'] = dn_info.get('title')
        data['subtitle'] = dn_info.get('subtitle')
    return data
