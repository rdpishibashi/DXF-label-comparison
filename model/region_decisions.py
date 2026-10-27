"""領域の分類（streamlit 非依存）。

複数プロジェクトで共有するモジュール（primary: DXF-extract-labels。
DXF-cross-checker・HostPL-comparer にバイト一致のコピーがあり、各プロジェクトの
`tests/regression/spec/test_shared_files_identical_to_extract_labels.py`
が一致を検証する）。変更は primary で行い、コピーへそのまま伝播すること。

「抽出領域の選択」（特定・除外）の状態から、各領域が最終的にどう扱われるかを
`classify_regions()` の1か所で決める。出力用の名称選択（`view.region_selection.
gather_name_selections()`）、「領域の確認」に出すファイルの判定、viewer の表示が
すべてこの結果を使うので、画面と出力の判定は食い違わない。
"""
from dataclasses import dataclass

from .common_utils import normalize_width
from .ref_designator import unnamed_region_labels
from .region_detector import (
    is_region_pending_selection,
    pending_candidates_for_region,
    resolve_globally_decided_name,
)

EXCLUDED = 'excluded'        # 「除外」指定（名称候補のどれか、名称なし領域なら表示名）
UNNAMED = 'unnamed'          # 名称候補が0件（no name）。`adopted` は「特定」がONか
DECIDED = 'decided'          # チェック済み候補がちょうど1件
OTHER_FRAME = 'other_frame'  # 要選択だが、同じファイルの別の図面枠で決まった名称で決まる
PENDING = 'pending'          # 要選択（チェック済み候補が2件以上）
UNSPECIFIED = 'unspecified'  # 候補はあるが、チェック済みが0件（特定OFF）


@dataclass(frozen=True)
class RegionDecision:
    status: str
    name: str | None = None   # DECIDED/OTHER_FRAME: 決まった名称。UNNAMED: 「抽出領域の選択」の表示名
    candidates: tuple = ()    # PENDING: `pending_candidates_for_region()` の結果
    adopted: bool = True      # UNNAMED のときだけ意味を持つ（「特定」がONか）


def region_excluded(reg, excluded_names, unnamed_label=None):
    """この領域の名称候補のいずれかが「除外」指定されているかを判定する
    （`model.ref_designator.excluded_region_ids()` と同じ正規化規約）。
    名称候補が0件の無名領域は、「抽出領域の選択」の表示名（`unnamed_label`）が
    除外指定されているかで判定する。"""
    if not excluded_names:
        return False
    if unnamed_label is not None and unnamed_label in excluded_names:
        return True
    return any(
        normalize_width(text) in excluded_names
        for (_dist, text) in reg.get('name_candidates', [])
    )


def resolve_by_other_frames(reg, analysis, checked_names, excluded_names):
    """要選択の領域 `reg` を、同じファイルの**別の図面枠**で自動決定済みの名称で決める。

    `reg` の「抽出領域の選択」チェック済み候補のうち、別の図面枠の領域が
    `resolve_globally_decided_name()` で決定した名称（NFKC正規化して比較。「除外」指定の
    領域は根拠にしない）と一致するものが**ちょうど1種類**ならその候補テキストを返す。
    0種類・2種類以上なら None。同じ図面枠内の別領域で決まった名称は根拠にしない
    （同一図面内の入れ子・隣接領域は別の物理領域のため）。
    """
    decided = set()
    for r2 in analysis.get('regions', []):
        if r2.get('frame') == reg.get('frame') or region_excluded(r2, excluded_names):
            continue
        name = resolve_globally_decided_name(r2, checked_names)
        if name is not None:
            decided.add(normalize_width(name))
    matches = {}
    for _d, t in pending_candidates_for_region(reg, checked_names):
        if normalize_width(t) in decided:
            matches.setdefault(normalize_width(t), t)
    return next(iter(matches.values())) if len(matches) == 1 else None


def classify_regions(analysis, checked_names, excluded_names, unnamed_adopted=lambda label: True):
    """1ファイルの通常領域（`analysis['regions']`）を分類し、`{領域id: RegionDecision}` を返す。

    判定の優先順位: 除外 → 名称なし → 確定（候補1件）→ 要選択（別の図面枠で決まれば
    OTHER_FRAME）→ 特定OFF。`unnamed_adopted(表示名)` は名称なし領域の「特定」がONかを返す
    呼び出し側の関数（既定は常にON）。`minor_regions` は対象外。"""
    unnamed_labels = unnamed_region_labels(analysis)
    decisions = {}
    for reg in analysis.get('regions', []):
        label = unnamed_labels.get(reg['id'])
        if region_excluded(reg, excluded_names, label):
            decisions[reg['id']] = RegionDecision(EXCLUDED)
        elif label is not None:
            decisions[reg['id']] = RegionDecision(
                UNNAMED, name=label, adopted=bool(unnamed_adopted(label)))
        elif (decided := resolve_globally_decided_name(reg, checked_names)) is not None:
            decisions[reg['id']] = RegionDecision(DECIDED, name=decided)
        elif is_region_pending_selection(reg, checked_names):
            by_other = resolve_by_other_frames(reg, analysis, checked_names, excluded_names)
            if by_other is not None:
                decisions[reg['id']] = RegionDecision(OTHER_FRAME, name=by_other)
            else:
                decisions[reg['id']] = RegionDecision(
                    PENDING, candidates=tuple(pending_candidates_for_region(reg, checked_names)))
        else:
            decisions[reg['id']] = RegionDecision(UNSPECIFIED)
    return decisions
