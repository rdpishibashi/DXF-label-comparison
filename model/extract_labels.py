import ezdxf
import re
import os
import sys
import gc
from typing import List, Tuple, Dict, Optional

from ezdxf.tools.text import plain_mtext

# 抽出設定の読み込み（環境適応型）
# config.py が存在するプロジェクト（DXF-diff-manager 等）ではそこから読み込む
# 存在しないプロジェクトではフォールバックの内部定義を使用
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    sys.path.insert(0, parent_dir)
    from config import extraction_config
except ImportError:
    class ExtractionConfig:
        """DXF抽出関連の設定（デフォルト値）"""
        # 両フォーマット対応: XX0000-000-00X（長）、XX0000-000X（短）
        DRAWING_NUMBER_PATTERN = r'[A-Z]{2}\d{4}-\d{3}(?:-\d{2})?[A-Z]'
        SOURCE_LABEL_PROXIMITY = 80        # 流用元図番ラベルからの検出距離
        DWG_NO_LABEL_PROXIMITY = 80        # DWG No. ラベルからの検出距離
        TITLE_PROXIMITY_X = 80             # TITLE ラベルからの横方向検出距離
        RIGHTMOST_DRAWING_TOLERANCE = 100.0  # 右端図面判定の許容範囲

    extraction_config = ExtractionConfig()

from .common_utils import process_circuit_symbol_labels, is_invisible


def get_layers_from_dxf(dxf_file):
    """DXFファイルからレイヤー一覧を取得する"""
    try:
        doc = ezdxf.readfile(dxf_file)
        layer_names = [layer.dxf.name for layer in doc.layers]
        return sorted(layer_names)
    except Exception as e:
        print(f"レイヤー一覧の取得中にエラーが発生しました: {str(e)}")
        return []


def clean_mtext_format_codes(text: str) -> str:
    r"""MTEXTのフォーマットコードを除去してテキスト内容を返す。

    ezdxf の ``plain_mtext()`` でインラインフォーマットコードを解釈する。
    旧実装（手書き正規表現）と実データ 12,145 件で出力一致を確認済み
    （v1.5.1, 2026-06-15）。加えて旧実装が未対応だった以下も正しく処理する:
      - ``\S`` 分数・スタッキング（例: ``1\S1/2;`` → ``11/2``）
      - ``%%c`` / ``%%d`` / ``%%p``（Ø / ° / ±）
      - ``^I`` / ``^J`` / ``^M`` キャレットシーケンス → 空白
    日本語環境の円マーク（¥）→ バックスラッシュ正規化は plain_mtext の
    前処理として、``\P`` 等で生じる改行 → スペース化は後処理として残す。
    """
    if not text:
        return ""

    # 日本語環境の円マーク（¥）をバックスラッシュに正規化（plain_mtext の前処理）
    cleaned = text.replace('¥', '\\')

    # ezdxf の MTEXT パーサでフォーマットコードを解釈
    cleaned = plain_mtext(cleaned)

    # \P（段落区切り）等で生じる改行をスペースへ（旧実装の挙動を踏襲）
    cleaned = cleaned.replace('\n', ' ')
    return re.sub(r'\s+', ' ', cleaned).strip()


def _block_has_text_content(doc, block_name, cache, _visiting=None):
    """ブロック（再帰的にネストINSERTをたどった先も含む）が TEXT/MTEXT を
    1つでも含むかを判定する。virtual_entities() は変換・複製を伴う重い処理だが、
    手描き回路図ではテキストを持たない記号（コネクタ等）のINSERTが非常に多いため、
    この判定で無関係なINSERTの展開をスキップして高速化する（出力結果は変えない）。

    block_name 単位でメモ化する（cache は呼び出し元が1ファイル処理につき1つ用意する）。
    """
    if block_name in cache:
        return cache[block_name]
    if _visiting is None:
        _visiting = set()
    if block_name in _visiting:
        return False  # 循環参照ガード（ブロックが自身を間接的に参照するケース）
    _visiting.add(block_name)

    has = True  # 判定不能時は安全側（展開する）に倒し、挙動を変えない
    try:
        blk = doc.blocks.get(block_name)
        if blk is not None:
            has = False
            for x in blk:
                xt = x.dxftype()
                if xt in ('TEXT', 'MTEXT'):
                    has = True
                    break
                if xt == 'INSERT' and _block_has_text_content(doc, x.dxf.name, cache, _visiting):
                    has = True
                    break
    except Exception:
        has = True

    _visiting.discard(block_name)
    cache[block_name] = has
    return has


def extract_text_from_entity(entity) -> Tuple[str, str, Tuple[float, float]]:
    """TEXT / MTEXT エンティティからテキストと座標を抽出する"""
    try:
        x, y = 0.0, 0.0

        if entity.dxftype() == 'MTEXT':
            if hasattr(entity.dxf, 'insert'):
                x, y = entity.dxf.insert[0], entity.dxf.insert[1]
            elif hasattr(entity, 'dxf') and hasattr(entity.dxf, 'x') and hasattr(entity.dxf, 'y'):
                x, y = entity.dxf.x, entity.dxf.y
            else:
                try:
                    x = getattr(entity.dxf, 'x', 0.0)
                    y = getattr(entity.dxf, 'y', 0.0)
                except Exception:
                    x, y = 0.0, 0.0
        elif entity.dxftype() == 'TEXT':
            if hasattr(entity.dxf, 'insert'):
                x, y = entity.dxf.insert[0], entity.dxf.insert[1]
            elif hasattr(entity.dxf, 'location'):
                x, y = entity.dxf.location[0], entity.dxf.location[1]

        raw_text = ""

        if entity.dxftype() == 'TEXT':
            if hasattr(entity.dxf, 'text'):
                raw_text = entity.dxf.text
        elif entity.dxftype() == 'MTEXT':
            if hasattr(entity.dxf, 'text'):
                raw_text = entity.dxf.text
            if not raw_text and hasattr(entity, 'text'):
                try:
                    raw_text = entity.text
                except Exception:
                    pass
            if not raw_text and hasattr(entity, 'plain_text'):
                try:
                    raw_text = entity.plain_text()
                except Exception:
                    pass

        if raw_text:
            clean_text = clean_mtext_format_codes(raw_text) if entity.dxftype() == 'MTEXT' else raw_text.strip()
        else:
            clean_text = ""

        return raw_text, clean_text, (x, y)

    except Exception:
        return "", "", (0.0, 0.0)


def extract_drawing_numbers(text: str) -> List[str]:
    """テキストから図面番号フォーマットに一致する文字列を抽出する"""
    drawing_numbers = []
    for match in re.findall(extraction_config.DRAWING_NUMBER_PATTERN, text, re.IGNORECASE):
        if match.upper() not in [dn.upper() for dn in drawing_numbers]:
            drawing_numbers.append(match.upper())
    return drawing_numbers


def calculate_distance(coord1: Tuple[float, float], coord2: Tuple[float, float]) -> float:
    """2点間のユークリッド距離を計算する"""
    import math
    dx = coord1[0] - coord2[0]
    dy = coord1[1] - coord2[1]
    return math.sqrt(dx * dx + dy * dy)


def is_single_uppercase_letter(text: str) -> bool:
    """英大文字1文字かどうかを判定（半角・全角両対応）"""
    if len(text) != 1:
        return False
    if 'A' <= text <= 'Z':
        return True
    if 'Ａ' <= text <= 'Ｚ':  # 全角英大文字
        return True
    return False


def _titleblock_frame_bbox(doc, group_handle, frame_lineweight=100, frame_color=7, margin=1.0,
                            check_layer=True):
    """タイトルブロック（INSERT）が持つ図面枠のバウンディングボックスを返す。

    機器符号抽出（ref_designator.py）と同じ識別キー（lineweight=100 かつ color=7 の
    LINE）で図面枠を検出する。group_handle で指定した INSERT の内部（virtual_entities()
    で展開・ワールド座標変換済み）のみを見るため、同一座標に重なった旧・現行の
    タイトルブロックがあっても自身の枠だけを対象にできる。検出できない場合は None
    （呼び出し側は枠外判定をスキップし、従来どおり内容ベースの判定にフォールバックする）。

    `check_layer=False`（2026-09-23追加）: `is_invisible()` のレイヤー単位判定を
    スキップする。唯一のタイトルブロックがoff/frozenレイヤーに置かれている図面の
    フォールバック探索（`extract_labels()` 参照）で、そのタイトルブロック自身の
    枠を検出できるようにするため。
    """
    if doc is None or not group_handle:
        return None
    try:
        insert_entity = None
        for layout in doc.layouts:
            for e in layout:
                if e.dxftype() == 'INSERT' and getattr(e.dxf, 'handle', None) == group_handle:
                    insert_entity = e
                    break
            if insert_entity is not None:
                break
        if insert_entity is None or is_invisible(insert_entity, check_layer=check_layer):
            return None

        xs, ys = [], []
        for v in insert_entity.virtual_entities():
            if is_invisible(v, check_layer=check_layer):
                continue
            if v.dxftype() == 'LINE':
                if getattr(v.dxf, 'lineweight', None) == frame_lineweight and getattr(v.dxf, 'color', None) == frame_color:
                    xs.extend([v.dxf.start[0], v.dxf.end[0]])
                    ys.extend([v.dxf.start[1], v.dxf.end[1]])
        if not xs:
            return None
        return (min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin)
    except Exception:
        return None


def _is_titleblock_noise_label(text: str, coords: Tuple[float, float], frame_bbox) -> bool:
    """タイトル・サブタイトル候補から除外すべきノイズラベルかどうかを判定する。

    - 数字のみのラベル（半角・全角とも）は常に除外する（例: 図番横の頁数「1/1」が
      別々の TEXT/MTEXT に分かれて「1」「1」のように候補へ混入するケース）。
    - frame_bbox（同一タイトルブロック内の図面枠バウンディングボックス）が判明して
      いる場合は、その外側にあるラベルも除外する（枠外に置かれる位置記号 F/L/H
      やグリッド参照番号）。frame_bbox が None（枠を検出できない図面）の場合は
      この条件を適用しない＝従来どおり内容ベースの判定のみで安全側にフォールバック。
    """
    stripped = text.strip()
    if stripped and stripped.isdigit():
        return True
    if frame_bbox is not None:
        x0, x1, y0, y1 = frame_bbox
        if not (x0 <= coords[0] <= x1 and y0 <= coords[1] <= y1):
            return True
    return False


def extract_title_and_subtitle(
    all_labels: List[Tuple],
    drawing_numbers: Optional[List[Tuple]],
    main_drawing_group=None,
    doc=None,
    check_layer=True,
) -> Dict[str, Optional[str]]:
    """テキストラベルの位置関係からタイトルとサブタイトルを抽出する。

    各ラベルは `(テキスト, 座標)` または `(テキスト, 座標, グループキー)`。
    main_drawing_group（図番が属するタイトルブロックのグループキー）が指定され、
    かつ同一グループ内に TITLE ラベルが存在する場合は、そのグループのラベルだけを
    候補にする。旧・現行のタイトルブロックが同一座標に重なっている図面で、
    旧ブロック由来のサブタイトルが混入するのを防ぐ（図番判別と同じ方式）。
    グループ内に TITLE が無い場合（タイトルブロックがブロック化されず直接
    配置されている図面等）は従来どおり全ラベルで判定する。

    doc（ezdxf Document）を渡すと、main_drawing_group が指すタイトルブロック
    INSERT 内の図面枠（lineweight=100・color=7 の LINE）を検出し、その外側に
    ある位置記号（F/L/H 等）・グリッド参照番号をタイトル／サブタイトル候補から
    除外する（`_is_titleblock_noise_label`）。doc 省略時・枠検出不能時は枠外
    判定を行わず、数字のみラベルの除外のみ行う（内容ベースの判定に安全側で
    フォールバック）。

    `check_layer=False`（2026-09-23追加）: `_titleblock_frame_bbox()` に渡し、
    レイヤー単位のoff/frozen判定をスキップさせる。唯一のタイトルブロックが
    off/frozenレイヤーに置かれている図面のフォールバック探索でのみ使う
    （`extract_labels()` 参照）。
    """
    if not all_labels:
        return {'title': None, 'subtitle': None}

    # ラベルを (テキスト, 座標, グループ) に正規化（グループ未指定は None）
    norm = [(item[0], item[1], item[2] if len(item) >= 3 else None) for item in all_labels]
    if main_drawing_group is not None:
        same_group = [item for item in norm if item[2] == main_drawing_group]
        if any(label.upper().strip() == 'TITLE' for label, _c, _g in same_group):
            norm = same_group
    all_labels = [(label, coords) for label, coords, _g in norm]

    title_text = None
    subtitle_text = None

    # TITLE と REVISION の位置を特定（複数ある場合は最も右側を採用）
    title_label_positions = []
    revision_label_positions = []

    for label, coords in all_labels:
        label_upper = label.upper().strip()
        if label_upper == 'TITLE':
            title_label_positions.append(coords)
        elif label_upper == 'REVISION':
            revision_label_positions.append(coords)

    if not title_label_positions:
        return {'title': None, 'subtitle': None}

    title_label_pos = max(title_label_positions, key=lambda pos: pos[0])
    revision_label_pos = max(revision_label_positions, key=lambda pos: pos[0]) if revision_label_positions else None

    # タイトル候補を収集（TITLE の右側かつ REVISION より下）
    title_proximity_x = extraction_config.TITLE_PROXIMITY_X
    title_candidates = []
    frame_bbox = _titleblock_frame_bbox(doc, main_drawing_group, check_layer=check_layer)

    for label, coords in all_labels:
        label_upper = label.upper().strip()
        if label_upper in ['TITLE', 'REVISION']:
            continue
        if drawing_numbers and any(dn == label for dn, *_ in drawing_numbers):
            continue
        if _is_titleblock_noise_label(label, coords, frame_bbox):
            continue

        x_diff = coords[0] - title_label_pos[0]
        if 10 < x_diff < title_proximity_x:
            if revision_label_pos:
                if coords[1] < revision_label_pos[1]:
                    title_candidates.append((label, coords))
            else:
                title_candidates.append((label, coords))

    if not title_candidates:
        return {'title': None, 'subtitle': None}

    # 座標が近く同じラベルの重複を除去
    coord_tolerance = 1.0
    deduplicated = []
    for label, coords in title_candidates:
        is_dup = False
        for ex_label, ex_coords in deduplicated:
            if (label == ex_label
                    and abs(coords[0] - ex_coords[0]) <= coord_tolerance
                    and abs(coords[1] - ex_coords[1]) <= coord_tolerance):
                is_dup = True
                break
        if not is_dup:
            deduplicated.append((label, coords))

    title_candidates = deduplicated
    if not title_candidates:
        return {'title': None, 'subtitle': None}

    # Y座標でグルーピング（同じ行の単語を結合）
    y_tolerance = 5.0
    grouped_candidates = []
    sorted_candidates = sorted(title_candidates, key=lambda x: (-x[1][1], x[1][0]))

    current_group = []
    current_y = None

    for label, coords in sorted_candidates:
        if current_y is None or abs(coords[1] - current_y) <= y_tolerance:
            current_group.append((label, coords))
            current_y = coords[1]
        else:
            if current_group:
                grouped_candidates.append(current_group)
            current_group = [(label, coords)]
            current_y = coords[1]

    if current_group:
        grouped_candidates.append(current_group)

    if not grouped_candidates:
        return {'title': None, 'subtitle': None}

    # Y座標が最も高いグループ群の中で、X座標が最小のものをタイトルとする
    groups_with_min_x = [
        (group, min(c[0] for _, c in group), sum(c[1] for _, c in group) / len(group))
        for group in grouped_candidates
    ]

    max_y = max(avg_y for _, _, avg_y in groups_with_min_x)
    y_threshold = 10.0
    top_groups = [(g, mx, ay) for g, mx, ay in groups_with_min_x if ay >= max_y - y_threshold]
    # X座標最小のグループを選ぶ。min_x が実質同一（許容誤差内）のグループが
    # 複数ある場合、浮動小数点ノイズの大小で直下のサブタイトル行が選ばれて
    # しまわないよう、Y座標が最も高いグループ（最上段の行）を採用する。
    leftmost_x = min(mx for _, mx, _ in top_groups)
    x_tie_tolerance = 1.0
    leftmost_groups = [(g, mx, ay) for g, mx, ay in top_groups if mx <= leftmost_x + x_tie_tolerance]
    title_group = max(leftmost_groups, key=lambda x: x[2])[0]

    title_group_sorted = sorted(title_group, key=lambda x: x[1][0])
    title_text = ' '.join(label for label, _ in title_group_sorted)
    title_y_coord = title_group_sorted[0][1][1]

    # サブタイトルを探す（タイトルの直下かつ同じX座標範囲）
    title_min_x = min(c[0] for _, c in title_group)
    title_max_x = max(c[0] for _, c in title_group)
    x_tolerance = extraction_config.RIGHTMOST_DRAWING_TOLERANCE

    subtitle_candidates = [
        (g, mx, ay) for g, mx, ay in groups_with_min_x
        if ay < title_y_coord
        and mx <= title_max_x + x_tolerance
        and max(c[0] for _, c in g) >= title_min_x - x_tolerance
    ]

    if subtitle_candidates:
        subtitle_group = max(subtitle_candidates, key=lambda x: (x[2], -x[1]))[0]
        subtitle_group_sorted = sorted(subtitle_group, key=lambda x: x[1][0])
        subtitle_labels = [label for label, _ in subtitle_group_sorted]

        if len(subtitle_labels) > 1 and is_single_uppercase_letter(subtitle_labels[-1]):
            subtitle_labels = subtitle_labels[:-1]

        subtitle_text = ' '.join(subtitle_labels)

    # タイトルとサブタイトルが同一内容の場合はサブタイトルなしとみなす
    # （重なったタイトルブロックの残骸など、同じ行が二重に候補化された場合の安全策）
    if subtitle_text is not None and subtitle_text == title_text:
        subtitle_text = None

    return {'title': title_text, 'subtitle': subtitle_text}


def determine_drawing_number_types(
    drawing_numbers: List[Tuple],
    all_labels: Optional[List[Tuple[str, Tuple[float, float]]]] = None,
    filename: Optional[str] = None,
) -> Dict[str, str]:
    """ラベルと座標に基づいて図番と流用元図番を判別する。

    各候補は `(図番, 座標)` または `(図番, 座標, グループキー)`。グループキーは
    所属タイトルブロック（INSERT）の識別子で、旧・現行のタイトルブロックが同一
    座標に重なっているケースで「図番と流用元図番が同じブロックに属する」ことを
    判定するために使う。図番がファイル名等で確定したら、**同じグループ内**で
    流用元図番を判定し、別ブロック（旧版）の図番を誤って拾わないようにする。

    戻り値の 'main_group' は図番が属するグループキー（不明な場合は None）。
    タイトル抽出（extract_title_and_subtitle）で同一ブロック内に候補を絞る
    ために使う。all_labels の各要素は `(テキスト, 座標)` または
    `(テキスト, 座標, グループキー)`。
    """
    if len(drawing_numbers) == 0:
        return {'main_drawing': None, 'source_drawing': None, 'main_group': None}

    # 候補を (図番, 座標, グループ) に正規化（グループ未指定は None）
    norm = [(item[0], item[1], item[2] if len(item) >= 3 else None) for item in drawing_numbers]

    if len(norm) == 1:
        return {'main_drawing': norm[0][0], 'source_drawing': None, 'main_group': norm[0][2]}

    main_drawing = None
    main_group = None
    source_drawing = None

    # 1. ファイル名と一致する図面番号を図番とする
    if filename:
        from pathlib import Path
        file_stem = Path(filename).stem
        for dn, coords, group in norm:
            if dn == file_stem or dn in file_stem or file_stem in dn:
                main_drawing = dn
                main_group = group
                break

    # 2. ラベルベースの判別
    if all_labels:
        label_pairs = [(item[0], item[1]) for item in all_labels]
        source_label_positions = [
            coords for label, coords in label_pairs
            if '流用元図番' in label or '流用元' in label
        ]
        dwg_label_positions = [
            coords for label, coords in label_pairs
            if ('DWG' in label.upper().replace('\n', '').replace('\r', '').replace(' ', '')
                and 'NO' in label.upper().replace('\n', '').replace('\r', '').replace(' ', ''))
        ]

        # 流用元図番を「流用元図番」ラベルに最も近い図面番号から判別する。
        # 図番のグループが分かっている場合は、同一グループ内の候補に限定して
        # 判定し、重なった別ブロック（旧版）の図番を拾わないようにする。
        if source_label_positions:
            source_pool = norm
            if main_group is not None:
                same_group = [c for c in norm if c[2] == main_group and c[0] != main_drawing]
                if same_group:
                    source_pool = same_group

            min_distance = float('inf')
            closest_dn = None
            for dn, dn_coords, group in source_pool:
                if main_drawing and dn == main_drawing:
                    continue
                for label_coords in source_label_positions:
                    distance = calculate_distance(dn_coords, label_coords)
                    if distance < min_distance:
                        min_distance = distance
                        closest_dn = dn
            if closest_dn and min_distance < extraction_config.SOURCE_LABEL_PROXIMITY:
                if closest_dn != main_drawing:
                    source_drawing = closest_dn

        # 図番を「DWG No.」ラベルに最も近い図面番号から確認
        if dwg_label_positions and not main_drawing:
            min_distance = float('inf')
            closest_dn = None
            closest_group = None
            for dn, dn_coords, group in norm:
                for label_coords in dwg_label_positions:
                    distance = calculate_distance(dn_coords, label_coords)
                    if distance < min_distance:
                        min_distance = distance
                        closest_dn = dn
                        closest_group = group
            if closest_dn and min_distance < extraction_config.DWG_NO_LABEL_PROXIMITY:
                main_drawing = closest_dn
                main_group = closest_group

    # 3. フォールバック: 座標ベースの判別（最も右側の図面を対象）
    if not main_drawing or not source_drawing:
        max_x = max(coords[0] for _, coords, _ in norm)
        x_tolerance = extraction_config.RIGHTMOST_DRAWING_TOLERANCE
        rightmost_numbers = [
            (dn, coords, group) for dn, coords, group in norm
            if coords[0] >= max_x - x_tolerance
        ]
        sorted_numbers = sorted(rightmost_numbers, key=lambda x: (x[1][0] + x[1][1]), reverse=True)

        if not main_drawing:
            main_drawing = sorted_numbers[0][0]
            main_group = sorted_numbers[0][2]

        if not source_drawing and len(sorted_numbers) > 1:
            # 図番グループが分かっている場合は同一グループを優先する
            ordered = sorted_numbers[1:]
            if main_group is not None:
                ordered = [c for c in ordered if c[2] == main_group] + \
                          [c for c in ordered if c[2] != main_group]
            for dn, _coords, _group in ordered:
                if dn != main_drawing:
                    source_drawing = dn
                    break

    # 最終検証: 流用元図番が図番と同じ場合は None にする
    if source_drawing and source_drawing == main_drawing:
        source_drawing = None

    return {'main_drawing': main_drawing, 'source_drawing': source_drawing, 'main_group': main_group}


def _entity_handle(entity):
    return getattr(entity.dxf, 'handle', None)


def _collect_text_entities(doc, msp, selected_layers, check_layer=True):
    """MODEL_SPACE・PAPER_SPACE・INSERT展開の全経路からTEXT/MTEXTエンティティを
    収集する（重複除去はしない）。戻り値は `[(entity, group_key), ...]`。
    group_key は所属タイトルブロック（INSERT）の識別子（INSERT 由来は親 INSERT
    の handle、直接配置は自身の handle）。

    `check_layer=False`（2026-09-23追加）: `is_invisible()` のレイヤー単位
    off/frozen判定をスキップする（エンティティ自身の`invisible`属性のみで
    判定）。`extract_labels()` のフォールバック探索（唯一のタイトルブロックが
    off/frozenレイヤーに置かれている図面向け）でのみ `False` を渡す。
    """
    all_entities_to_process = []

    # MODEL_SPACE
    for e in msp:
        if is_invisible(e, check_layer=check_layer):
            continue
        if e.dxftype() in ['TEXT', 'MTEXT']:
            all_entities_to_process.append((e, _entity_handle(e)))

    # PAPER_SPACE（Model 以外のレイアウト）
    try:
        for layout in doc.layouts:
            if layout.name != 'Model':
                for e in layout:
                    if is_invisible(e, check_layer=check_layer):
                        continue
                    if e.dxftype() in ['TEXT', 'MTEXT']:
                        all_entities_to_process.append((e, _entity_handle(e)))
    except Exception:
        pass

    # INSERT エンティティを virtual_entities() で展開（座標変換を含む）
    # テキストを含まないブロックは virtual_entities() を呼ぶ前にスキップし、
    # 無駄な展開コストを避ける。INSERT自身が非表示なら中身ごと丸ごと除外し
    # （virtual_entities()は親の状態を継承しないため明示チェックが必要）、
    # 展開後の個々の仮想エンティティにも非表示が立っている場合があるため、
    # そちらも個別に除外する。
    block_text_cache = {}
    try:
        for e in msp:
            if e.dxftype() == 'INSERT' and e.dxf.layer in selected_layers:
                if is_invisible(e, check_layer=check_layer):
                    continue
                if not _block_has_text_content(doc, e.dxf.name, block_text_cache):
                    continue
                insert_group = _entity_handle(e)
                try:
                    for virtual_entity in e.virtual_entities():
                        if is_invisible(virtual_entity, check_layer=check_layer):
                            continue
                        if virtual_entity.dxftype() in ['TEXT', 'MTEXT']:
                            all_entities_to_process.append((virtual_entity, insert_group))
                except Exception:
                    pass

        for layout in doc.layouts:
            if layout.name != 'Model':
                for e in layout:
                    if e.dxftype() == 'INSERT' and e.dxf.layer in selected_layers:
                        if is_invisible(e, check_layer=check_layer):
                            continue
                        if not _block_has_text_content(doc, e.dxf.name, block_text_cache):
                            continue
                        insert_group = _entity_handle(e)
                        try:
                            for virtual_entity in e.virtual_entities():
                                if is_invisible(virtual_entity, check_layer=check_layer):
                                    continue
                                if virtual_entity.dxftype() in ['TEXT', 'MTEXT']:
                                    all_entities_to_process.append((virtual_entity, insert_group))
                        except Exception:
                            pass
    except Exception:
        pass

    return all_entities_to_process


def _dedup_text_entities(entities_with_group):
    """同一種・同一レイヤー・同一座標のエンティティを除去する。"""
    seen_entities = set()
    unique_entities = []
    for e, group_key in entities_with_group:
        try:
            entity_key = (
                e.dxftype(),
                e.dxf.layer if hasattr(e.dxf, 'layer') else '',
                getattr(e.dxf, 'insert', (0, 0)) if hasattr(e.dxf, 'insert') else (0, 0),
            )
            if entity_key not in seen_entities:
                seen_entities.add(entity_key)
                unique_entities.append((e, group_key))
        except Exception:
            unique_entities.append((e, group_key))
    return unique_entities


def _build_label_and_drawing_number_candidates(unique_entities, selected_layers):
    """テキスト抽出を行い、`(clean_text, coordinates, group_key)` のラベル一覧
    と図番候補一覧を返す（図番・タイトル判定専用。`extract_labels()` の
    フォールバック探索〈2026-09-23〉で使用）。"""
    all_labels_with_coords = []
    drawing_number_candidates = []
    for e, group_key in unique_entities:
        if e.dxf.layer in selected_layers:
            _, clean_text, coordinates = extract_text_from_entity(e)
            if clean_text:
                all_labels_with_coords.append((clean_text, coordinates, group_key))
                for dn in extract_drawing_numbers(clean_text):
                    drawing_number_candidates.append((dn, coordinates, group_key))
    return all_labels_with_coords, drawing_number_candidates


def extract_labels(dxf_file, filter_non_parts=False, sort_order="asc", debug=False,
                   selected_layers=None, validate_ref_designators=False,
                   extract_drawing_numbers_option=False, extract_title_option=False,
                   include_coordinates=False, original_filename=None):
    """DXFファイルからテキストラベルを抽出する"""
    info = {
        "total_extracted": 0,
        "filtered_count": 0,
        "final_count": 0,
        "processed_layers": 0,
        "total_layers": 0,
        "filename": os.path.basename(dxf_file),
        "invalid_ref_designators": [],
        "main_drawing_number": None,
        "source_drawing_number": None,
        "all_drawing_numbers": [],
        "title": None,
        "subtitle": None,
    }

    try:
        doc = ezdxf.readfile(dxf_file)
        msp = doc.modelspace()

        all_layers = [layer.dxf.name for layer in doc.layers]
        info["total_layers"] = len(all_layers)

        if selected_layers is None:
            selected_layers = all_layers
        info["processed_layers"] = len(selected_layers)

        labels = []
        labels_with_coordinates = []
        drawing_number_candidates = []
        all_labels_with_coords = []

        # エンティティ収集（表示中のみ、check_layer=True）
        # 各要素は (entity, group_key)。group_key は所属タイトルブロック（INSERT）の
        # 識別子。INSERT 由来は親 INSERT の handle、直接配置は自身の handle を使う。
        # 旧・現行のタイトルブロックが同一座標に重なっているケースで、図番と流用元
        # 図番が同じブロックに属することを判定するために用いる。
        entities_visible = _collect_text_entities(doc, msp, selected_layers, check_layer=True)
        unique_entities = _dedup_text_entities(entities_visible)

        # テキスト抽出（出力ラベルは常に表示中のエンティティのみを対象にする。
        # 2026-09-23: 唯一のタイトルブロックがoff/frozenレイヤーに置かれている
        # 図面〈EE5322-455-02A.dxf等〉のフォールバック探索〈後述〉を追加した後も、
        # 実際に出力するラベルは変えない方針は維持する）。
        for e, group_key in unique_entities:
            if e.dxf.layer in selected_layers:
                raw_text, clean_text, coordinates = extract_text_from_entity(e)

                if clean_text:
                    if extract_drawing_numbers_option or extract_title_option:
                        all_labels_with_coords.append((clean_text, coordinates, group_key))

                    # タイトル抽出（extract_title_option）も、同一タイトルブロック
                    # グループ制限（main_drawing_group、下記参照）のために図番候補を
                    # 内部的に必要とするため、extract_drawing_numbers_option の指定に
                    # 関わらず収集する（2026-07-29、呼び出し側の渡し忘れに強くする
                    # ための内部結合解消。詳細は下記コメント）。
                    if extract_drawing_numbers_option or extract_title_option:
                        for dn in extract_drawing_numbers(clean_text):
                            drawing_number_candidates.append((dn, coordinates, group_key))

                    labels.append(clean_text)
                    labels_with_coordinates.append((clean_text, coordinates[0], coordinates[1]))

        info["total_extracted"] = len(labels)

        # フォールバック（2026-09-23）: 表示中のエンティティだけでは図番候補が
        # 1件も見つからない場合に限り、レイヤーoff/frozenを無視して（＝
        # エンティティ自身のinvisible属性のみで）図番・タイトル判定用の
        # ラベル・図番候補を収集し直す。唯一のタイトルブロックがoff/frozen
        # レイヤーに置かれている図面（EE5322-455-02A.dxf/-18A.dxf）で、
        # 2026-09-16のレイヤー単位invisible判定追加により図番・タイトルが
        # 一切取れなくなっていた回帰への対応。
        #
        # 出力ラベル（labels/labels_with_coordinates、上で確定済み）は
        # 変更しない——フォールバックは図番・タイトル・図面枠の「手がかり」を
        # 見つけるためだけに使い、実際に出力するラベルは常に表示中のものに限る
        # （common_utils.is_invisibleのdocstring、Tools/CLAUDE.md参照）。
        # `check_layer` は extract_title_and_subtitle() 経由で
        # _titleblock_frame_bbox() にも伝播し、フォールバック時はタイトル
        # ブロック自身の図面枠（情報枠ノイズ除外用）もoff/frozen無視で検出する。
        check_layer_for_title = True
        if (extract_drawing_numbers_option or extract_title_option) and not drawing_number_candidates:
            entities_all = _collect_text_entities(doc, msp, selected_layers, check_layer=False)
            if len(entities_all) > len(entities_visible):
                unique_entities_all = _dedup_text_entities(entities_all)
                fallback_labels_with_coords, fallback_drawing_number_candidates = \
                    _build_label_and_drawing_number_candidates(unique_entities_all, selected_layers)
                if fallback_drawing_number_candidates:
                    all_labels_with_coords = fallback_labels_with_coords
                    drawing_number_candidates = fallback_drawing_number_candidates
                    check_layer_for_title = False

        # 図面番号の判別
        # main_drawing_group は extract_title_and_subtitle() の同一タイトルブロック
        # グループ制限に使うため、extract_title_option=True であれば
        # extract_drawing_numbers_option の指定に関わらず常に計算する（2026-07-29）。
        # 以前は extract_drawing_numbers_option=True を渡し忘れると main_drawing_group
        # が計算されずこの制限が働かず、複数タイトルブロックが1ファイルに存在する
        # 図面（同一図番のシートが横並びで対象シートだけタイトル行が欠落している等）
        # でタイトル/サブタイトルを誤抽出していた（DXF-diff-manager の呼び出し漏れで
        # 実際に発生）。呼び出し側のオプション指定を尊重するため、info の公開キー
        # （main_drawing_number/source_drawing_number/all_drawing_numbers）は
        # 従来どおり extract_drawing_numbers_option=True のときのみ設定する。
        main_drawing_group = None
        if (extract_drawing_numbers_option or extract_title_option) and drawing_number_candidates:
            filename_for_matching = original_filename if original_filename else dxf_file
            drawing_info = determine_drawing_number_types(
                drawing_number_candidates,
                all_labels=all_labels_with_coords,
                filename=filename_for_matching,
            )
            main_drawing_group = drawing_info['main_group']
            if extract_drawing_numbers_option:
                info["main_drawing_number"] = drawing_info['main_drawing']
                info["source_drawing_number"] = drawing_info['source_drawing']
                info["all_drawing_numbers"] = [dn[0] for dn in drawing_number_candidates]

        # タイトル・サブタイトルの抽出
        if extract_title_option and all_labels_with_coords:
            title_info = extract_title_and_subtitle(
                all_labels_with_coords,
                drawing_numbers=drawing_number_candidates or None,
                main_drawing_group=main_drawing_group,
                doc=doc,
                check_layer=check_layer_for_title,
            )
            info["title"] = title_info['title']
            info["subtitle"] = title_info['subtitle']

        # 機器符号フィルタリング・妥当性チェック
        symbol_result = process_circuit_symbol_labels(
            labels,
            filter_non_parts=filter_non_parts,
            validate_ref_designators=validate_ref_designators,
        )
        processed_labels = symbol_result['labels']
        info["filtered_count"] = symbol_result['filtered_count']
        info["invalid_ref_designators"] = symbol_result['invalid_ref_designators']

        # ソートと返却形式の選択
        if include_coordinates:
            filtered_set = set(processed_labels)
            processed_label_entries = [
                entry for entry in labels_with_coordinates if entry[0] in filtered_set
            ]
            if sort_order == "asc":
                processed_label_entries.sort(key=lambda x: (x[0], x[1], x[2]))
            elif sort_order == "desc":
                processed_label_entries.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
            final_labels = processed_label_entries
        else:
            if sort_order == "asc":
                processed_labels.sort()
            elif sort_order == "desc":
                processed_labels.sort(reverse=True)
            final_labels = processed_labels

        info["final_count"] = len(final_labels)

        del doc
        del msp
        gc.collect()

        return final_labels, info

    except Exception as e:
        print(f"エラー: {str(e)}")
        info["error"] = str(e)
        gc.collect()
        return [], info


def get_title_and_subtitle(dxf_file, original_filename=None):
    """DXFファイルのタイトル・サブタイトルのみを取得する軽量ラッパー。

    タイトル・サブタイトルだけが必要な呼び出し元（ラベル一覧やフィルタ結果は
    不要な場合）のための入り口。内部では `extract_labels()` を
    `extract_drawing_numbers_option=True` と `extract_title_option=True` の
    組み合わせで呼び出す。v1.9.22の内部結合解消により `extract_title_option=True`
    単独でも正しく動作するようになっているが、呼び出し元がオプションの組み合わせを
    都度考えずに済むよう、この関数を経由することを推奨する（2026-07-29追加。
    呼び出し側のオプション指定ミスで実際に不具合が発生した経緯は
    determine_drawing_number_types / extract_title_and_subtitle のdocstring参照）。

    Returns:
        (title, subtitle) のタプル。いずれも str または None。
    """
    _, info = extract_labels(
        dxf_file,
        extract_drawing_numbers_option=True,
        extract_title_option=True,
        original_filename=original_filename,
    )
    return info.get('title'), info.get('subtitle')


def process_multiple_dxf_files(dxf_files, filter_non_parts=False, sort_order="asc", debug=False,
                                selected_layers=None, validate_ref_designators=False,
                                extract_drawing_numbers_option=False, extract_title_option=False,
                                original_filenames=None):
    """複数のDXFファイルからラベルを抽出する"""
    results = {}

    for i, dxf_file in enumerate(dxf_files):
        original_filename = original_filenames[i] if original_filenames and i < len(original_filenames) else None

        if os.path.isdir(dxf_file):
            for root, _, files in os.walk(dxf_file):
                for file in files:
                    if file.lower().endswith('.dxf'):
                        file_path = os.path.join(root, file)
                        labels, info = extract_labels(
                            file_path, filter_non_parts, sort_order, debug,
                            selected_layers, validate_ref_designators,
                            extract_drawing_numbers_option, extract_title_option,
                            original_filename=file,
                        )
                        results[file_path] = (labels, info)
        elif os.path.isfile(dxf_file) and dxf_file.lower().endswith('.dxf'):
            labels, info = extract_labels(
                dxf_file, filter_non_parts, sort_order, debug,
                selected_layers, validate_ref_designators,
                extract_drawing_numbers_option, extract_title_option,
                original_filename=original_filename,
            )
            results[dxf_file] = (labels, info)

    return results
