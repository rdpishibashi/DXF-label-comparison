"""B側（UNIT内結線図）のファイル絞り込み（Streamlit非依存の純関数）。"""
from model.common_utils import normalize_width

UNIT_TITLE = 'UNIT内結線図'

FILTER_UNIT_ONLY = 'UNIT内結線図のみ'
FILTER_ALL = '全部'
FILTER_OPTIONS = (FILTER_UNIT_ONLY, FILTER_ALL)

MANUAL_CHANGE_SUFFIX = '（手動変更あり）'


def is_unit_wiring_title(title) -> bool:
    """タイトルが 'UNIT内結線図' と一致するか（NFKC正規化・前後空白除去のうえで
    完全一致。「ＵＮＩＴ内結線図」〈全角〉も同じものとして扱う）。"""
    if not title:
        return False
    return normalize_width(str(title)).strip() == UNIT_TITLE


def select_files(title_by_key: dict, filter_mode: str) -> list:
    """{ファイルキー: タイトル} から、filter_mode に応じた対象ファイルキーを
    元の順序のまま返す。「UNIT内結線図のみ」ではタイトルが空（未抽出）のファイルは対象外。"""
    if filter_mode == FILTER_ALL:
        return list(title_by_key)
    if filter_mode == FILTER_UNIT_ONLY:
        return [k for k, t in title_by_key.items() if is_unit_wiring_title(t)]
    raise ValueError(f"不明な filter_mode: {filter_mode}")


def initial_selection(title_by_key: dict) -> dict:
    """B図面一覧のチェックボックスの初期値 {ファイルキー: bool}。
    『UNIT内結線図のみ』の判定（`select_files()`）と同じ。"""
    return {k: is_unit_wiring_title(t) for k, t in title_by_key.items()}


def filter_condition_label(filter_mode: str, initial: dict, current: dict) -> str:
    """Summary の『B 絞り込み条件』に出す文言。『UNIT内結線図のみ』で、初期の選択
    （`initial_selection()`）から手動で変更された場合は『（手動変更あり）』を付ける。"""
    if filter_mode == FILTER_UNIT_ONLY and any(
            bool(current.get(k)) != bool(v) for k, v in initial.items()):
        return filter_mode + MANUAL_CHANGE_SUFFIX
    return filter_mode
