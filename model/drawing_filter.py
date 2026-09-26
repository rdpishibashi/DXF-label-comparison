"""B側（UNIT内結線図）のファイル絞り込み（Streamlit非依存の純関数）。"""
from model.common_utils import normalize_width

UNIT_TITLE = 'UNIT内結線図'

FILTER_UNIT_ONLY = 'UNIT内結線図のみ'
FILTER_UNIT_EXCLUDED = 'UNIT内結線図以外'
FILTER_ALL = '全部'
FILTER_OPTIONS = (FILTER_UNIT_ONLY, FILTER_UNIT_EXCLUDED, FILTER_ALL)


def is_unit_wiring_title(title) -> bool:
    """タイトルが 'UNIT内結線図' と一致するか（NFKC正規化・前後空白除去のうえで
    完全一致。「ＵＮＩＴ内結線図」〈全角〉も同じものとして扱う）。"""
    if not title:
        return False
    return normalize_width(str(title)).strip() == UNIT_TITLE


def select_files(title_by_key: dict, filter_mode: str) -> list:
    """{ファイルキー: タイトル} から、filter_mode に応じた対象ファイルキーを
    元の順序のまま返す。タイトルが空（未抽出）のファイルは『UNIT内結線図以外』扱い。"""
    if filter_mode == FILTER_ALL:
        return list(title_by_key)
    if filter_mode == FILTER_UNIT_ONLY:
        return [k for k, t in title_by_key.items() if is_unit_wiring_title(t)]
    if filter_mode == FILTER_UNIT_EXCLUDED:
        return [k for k, t in title_by_key.items() if not is_unit_wiring_title(t)]
    raise ValueError(f"不明な filter_mode: {filter_mode}")
