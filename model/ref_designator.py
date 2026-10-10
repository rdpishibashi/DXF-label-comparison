"""機器符号抽出モジュール（v2.0.0）— DXF抽出パイプライン

DXF-extract-labels 固有の新機能であり、他プロジェクトとの共有コピーは無い
（`model/extract_labels.py` とは独立に保つ）。

**2026-07-30 判定条件の簡素化**: 除外パターン・追加パターン・確定パターン・
「未確定ラベル」UIでのレビュー・連動採用・判断ログ（採用/非採用の記録）を
すべて廃止し、以下の単純なパイプラインに一本化した（ユーザー指定）:

  1. 図面枠内、かつ図面情報枠（タイトルブロック）外のラベルをスコープとする
  2. ラベル末尾に括弧で閉じた文字列がある場合は、その括弧より前の部分で
     判定する（ただし出力は原文のまま）
  3. `label_classifier.classify_label()` が機器符号候補（DESIGNATOR）と判定した
     ラベルを、除外や確認なしにそのまま全件出力する

図面情報欄の除外は、従来の構造的除外（図面枠線を直接の子に持つ
「フォーマットブロック」由来のテキストを丸ごと除外）から、幾何検出
（図面枠の右下、右辺・底辺が図面枠に接する矩形をタイトル項目名の並びから
特定する）に置き換えた。詳細は `_detect_info_frame_bbox()` を参照。

機器符号候補の判定は `label_classifier.py` の `classify_label()`（機器符号／領域名／
どちらでもないの3分類）に一元化されており、`region_detector.py` の領域名候補
フィルタも同じ関数を使う。
"""
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import ezdxf

from .common_utils import normalize_width, select_layout_result, is_invisible
from .extract_labels import extract_text_from_entity, _block_has_text_content
from .region_detector import detect_drawing_frames, assign_region_labels
from .label_classifier import normalize_label, is_ref_designator_label  # noqa: F401（再エクスポート）


# ============================================================
# 1. 図面枠検出（従来通り）
# ============================================================
#
# 図面枠線は lineweight=frame_lineweight かつ color=7 の LINE 4本で構成される
# （region_detector.py の DEFAULT_REGION_CONFIG と同じ判定。lineweight 単独では
# 無関係な線分を誤って拾い図面枠検出が壊れることを実データで確認済みのため、
# color=7 の併用を維持する。2026-07-10 ユーザー確認）。

_FRAME_COLOR = 7


def _format_block_names(doc, frame_lineweight: int, frame_color: int = _FRAME_COLOR) -> set:
    """図面枠線を直接の子として持つブロック名の集合を返す（テンプレートブロック）。

    ネストされた INSERT の中までは辿らない（frame線はブロック定義の直接の子に
    置かれる想定。実データで確認済み）。図面枠の検出、および図面情報枠の幾何
    検出に使う全線分の収集範囲を、この種のブロックに限定するために使う
    （2026-07-30、テキストの構造的除外自体は廃止したが、線分収集範囲を
    テンプレートブロックに限定する目的では引き続き使う。実データでは
    図面情報枠を構成する内部の格子線もこのブロックの直接の子として
    存在することを確認済み）。
    """
    names = set()
    for blk in doc.blocks:
        for x in blk:
            if (x.dxftype() == 'LINE'
                    and getattr(x.dxf, 'lineweight', None) == frame_lineweight
                    and getattr(x.dxf, 'color', None) == frame_color):
                names.add(blk.name)
                break
    return names


def _collect_frame_and_labels(doc, frame_lineweight: int, frame_color: int = _FRAME_COLOR,
                               check_layer: bool = True):
    """図面枠線・テンプレートブロック内の全線分・全ラベルエンティティを収集する。

    レイアウトの選び方は `common_utils.select_layout_result()` を参照
    （Model Space に何かあれば常にそちらを使い、完全に空の場合のみ他
    レイアウトを順に試す。レイアウトをまたいで混在させない）。

    `check_layer=False`（2026-09-23追加）: `is_invisible()` のレイヤー単位
    off/frozen判定をスキップする。唯一のタイトルブロック（図面枠を含む）が
    off/frozenレイヤーに置かれている図面のフォールバック探索
    （`collect_in_frame_labels()` 参照）でのみ `False` を渡す。

    戻り値: (frame_lines, block_lines, label_entities)
      frame_lines: [(start, end), ...]（図面枠検出用。lineweight=frame_lineweight
        かつ color=frame_color の線分のみ。Vec3のまま）
      block_lines: [(start, end), ...]（図面情報枠の幾何検出用。図面枠線を
        直接の子として持つテンプレートブロック由来の全線分。lineweight/color
        を問わない）
      label_entities: TEXT/MTEXTエンティティのリスト（テンプレートブロック内も
        含め、図面枠内の全ラベルを対象とする。構造的除外はしない——図面情報欄
        の除外は `_detect_info_frame_bbox()` による幾何検出で行う）
    """
    fmt_blocks = _format_block_names(doc, frame_lineweight, frame_color)
    text_block_cache = {}

    def is_frame_line(e):
        return (getattr(e.dxf, 'lineweight', None) == frame_lineweight
                and getattr(e.dxf, 'color', None) == frame_color)

    def collect_from_layout(layout):
        frame_lines = []
        block_lines = []
        label_entities = []
        for e in layout:
            if is_invisible(e, check_layer=check_layer):
                continue
            t = e.dxftype()
            if t == 'LINE':
                if is_frame_line(e):
                    frame_lines.append((e.dxf.start, e.dxf.end))
            elif t in ('TEXT', 'MTEXT'):
                label_entities.append(e)
            elif t == 'INSERT':
                name = e.dxf.name
                if name in fmt_blocks:
                    try:
                        for v in e.virtual_entities():
                            if is_invisible(v, check_layer=check_layer):
                                continue
                            vt = v.dxftype()
                            if vt == 'LINE':
                                if is_frame_line(v):
                                    frame_lines.append((v.dxf.start, v.dxf.end))
                                block_lines.append((v.dxf.start, v.dxf.end))
                            elif vt in ('TEXT', 'MTEXT'):
                                label_entities.append(v)
                    except Exception:
                        pass
                else:
                    if not _block_has_text_content(doc, name, text_block_cache):
                        continue
                    try:
                        for v in e.virtual_entities():
                            if is_invisible(v, check_layer=check_layer):
                                continue
                            if v.dxftype() in ('TEXT', 'MTEXT'):
                                label_entities.append(v)
                    except Exception:
                        pass
        return frame_lines, block_lines, label_entities

    return select_layout_result(doc, collect_from_layout, is_empty=lambda r: not any(r))


# ============================================================
# 2. 図面情報枠（タイトルブロック）の幾何検出
# ============================================================
#
# 各図面枠の右下、右辺・底辺が図面枠に接する矩形をタイトルブロックとみなす。
# 矩形内にタイトル項目名（下記 _TITLE_BLOCK_TERMS、全角/半角の両方の可能性が
# あるため NFKC 正規化して照合）が一定数以上含まれることを確認して確定する。
# 実データ156件で検証したところ、ULVACの標準テンプレートでは常に幅175×高さ64
# の矩形になった（2026-07-30）。

_TITLE_BLOCK_TERMS = [
    '貼', 'DWG.No', 'DATE', 'NAME', '流用元図番', '計算計量', 'TOLERANCES',
    'UNLESS NOTED', 'MARK', 'REMARKS', 'REVISION', 'APPRV', 'CHECK', 'DESIG',
    'DRAW', 'TITLE', '承認', '検図', '設計', '製図', 'SCALE', 'MFG No.', 'DWG No.',
]
_TITLE_BLOCK_TERMS_NORM = {normalize_width(t).upper() for t in _TITLE_BLOCK_TERMS}

_INFO_FRAME_MIN_MATCH = 3    # タイトル項目名の最小一致数（未満は情報枠なしとみなす）
_INFO_FRAME_Y_GAP = 20.0     # ラベルのyクラスタリング許容ギャップ
_INFO_FRAME_X_GAP = 30.0     # ラベルのxクラスタリング許容ギャップ（横並び複数枠対策）
_INFO_FRAME_SNAP = 2.0       # 境界線の座標マージン


def _detect_info_frame_bbox(block_lines, label_points, xl, xr, y0, y1):
    """1つの図面枠内にある図面情報枠（タイトルブロック）のbboxを幾何検出する。

    手順:
      1. 枠内ラベルのうち、タイトル項目名（`_TITLE_BLOCK_TERMS`）に完全一致
         するものを集める（部分一致ではなく完全一致。回路内容の説明文に
         たまたま "CHECK" 等の語が部分文字列として含まれるケースの誤検出を
         避けるため。例: `SNM-5 DI2 SET POINT (OUT CHECK)` を "CHECK" と
         誤マッチしない）。
      2. y昇順でクラスタリングし、底辺(y0)に接する最初の連続クラスタのみを
         採用する（「流用元図番」の履歴テーブル等、タイトルブロックとは別の
         位置にある同名ラベル群を除外するため）。
      3. さらにx降順（右辺xrに近い方から）でもクラスタリングし、右辺に接する
         連続クラスタのみを採用する（タイトルブロックが横並びに複数存在する
         図面〈旧版タイトルブロックとの比較表示等〉で、右側の枠だけを
         採用するため）。
      4. 一致数が `_INFO_FRAME_MIN_MATCH` 未満ならNone（情報枠なしとみなす）。
      5. クラスタのbboxを囲む実際の境界線（右辺xrに接する水平線・底辺y0に
         接する垂直線）を `block_lines` から探し、それを情報枠の境界とする。
         境界線が見つからない場合はラベルbbox+マージンでフォールバックする。

    戻り値: (left, xr, y0, top) または None（情報枠が検出できない場合）
    """
    matched = []
    for (text, x, y) in label_points:
        if not (xl - 1 <= x <= xr + 1 and y0 - 1 <= y <= y1 + 1):
            continue
        norm = normalize_width(text).upper().strip()
        if norm in _TITLE_BLOCK_TERMS_NORM:
            matched.append((text, x, y))
    if len(matched) < _INFO_FRAME_MIN_MATCH:
        return None

    matched.sort(key=lambda m: m[2])
    cluster = [matched[0]]
    for m in matched[1:]:
        if m[2] - cluster[-1][2] <= _INFO_FRAME_Y_GAP:
            cluster.append(m)
        else:
            break
    if len(cluster) < _INFO_FRAME_MIN_MATCH:
        return None

    cluster.sort(key=lambda m: -m[1])
    xcluster = [cluster[0]]
    for m in cluster[1:]:
        if xcluster[-1][1] - m[1] <= _INFO_FRAME_X_GAP:
            xcluster.append(m)
        else:
            break
    if len(xcluster) < _INFO_FRAME_MIN_MATCH:
        return None

    xs = [x for (_t, x, _y) in xcluster]
    ys = [y for (_t, _x, y) in xcluster]
    bbox_left, bbox_top = min(xs), max(ys)

    h_candidates = []  # (y, left_x)
    v_candidates = []  # (x, top_y)
    for (s, e) in block_lines:
        sx, sy = s[0], s[1]
        ex, ey = e[0], e[1]
        if abs(sy - ey) < 0.01:
            y = (sy + ey) / 2
            if y0 - 1 <= y <= y1 + 1:
                x_min, x_max = min(sx, ex), max(sx, ex)
                if abs(x_max - xr) <= _INFO_FRAME_SNAP:
                    h_candidates.append((y, x_min))
        elif abs(sx - ex) < 0.01:
            x = (sx + ex) / 2
            if xl - 1 <= x <= xr + 1:
                y_min, y_max = min(sy, ey), max(sy, ey)
                if abs(y_min - y0) <= _INFO_FRAME_SNAP:
                    v_candidates.append((x, y_max))

    h_above = [h for h in h_candidates if h[0] >= bbox_top - _INFO_FRAME_SNAP]
    v_left = [v for v in v_candidates if v[0] <= bbox_left + _INFO_FRAME_SNAP]

    if h_above and v_left:
        top_y = min(h_above, key=lambda h: h[0])[0]
        left_x = max(v_left, key=lambda v: v[0])[0]
    else:
        margin = _INFO_FRAME_SNAP
        top_y = bbox_top + margin
        left_x = bbox_left - margin

    return (left_x, xr, y0, top_y)


def collect_in_frame_labels(
    dxf_file: str,
    frame_lineweight: int = 100,
    frame_color: int = _FRAME_COLOR,
    snap: float = 2.0,
    doc=None,
) -> Dict:
    """図面枠内・図面情報枠（タイトルブロック）外のラベルを収集する。

    `doc` に読み込み済みの `ezdxf.Drawing` を渡すと、`dxf_file` を再読み込みしない
    （呼び出し側が同じファイルをフォールバック等で再利用するときの二重読み込み回避。
    `extract_ref_designator_data()` 参照）。省略時は `dxf_file` を読み込む。
    `doc` は読み取りのみで、変更しない。

    戻り値 dict:
      frames: [(xl,xr,y0,y1), ...]
      labels: [(text, x, y), ...]（重複除去済み、正規化前の原文）
      frames_without_info_frame: int（図面情報枠を検出できなかった図面枠の数）
      error: str | None
    """
    result = {
        'frames': [], 'labels': [], 'frames_without_info_frame': 0, 'error': None,
    }
    if doc is None:
        try:
            doc = ezdxf.readfile(dxf_file)
        except Exception as e:
            result['error'] = f'DXFファイルの読み込みに失敗しました: {e}'
            return result

    frame_lines, block_lines, label_entities = _collect_frame_and_labels(
        doc, frame_lineweight, frame_color, check_layer=True)
    frames = detect_drawing_frames(frame_lines, snap)

    # フォールバック（2026-09-23）: 表示中の図面枠が1件も見つからない場合に
    # 限り、レイヤーoff/frozenを無視して図面枠・図面情報枠の検出をやり直す。
    # 唯一のタイトルブロック（図面枠を含む）がoff/frozenレイヤーに置かれている
    # 図面（EE5322-455-02A.dxf/-18A.dxf）で、2026-09-16のレイヤー単位
    # invisible判定追加により図面枠自体が検出できなくなっていた回帰への対応。
    # フォールバックは図面枠・図面情報枠（タイトルブロック）の「手がかり」を
    # 見つけるためだけに使う——**出力するラベル（下記の label_points・
    # 最終的な labels）は常に表示中のエンティティ（label_entities、上で
    # 取得済み）のみを対象にする**（common_utils.is_invisibleのdocstring、
    # Tools/CLAUDE.md参照）。
    block_lines_for_info_frame = block_lines
    fallback_label_points = None
    if not frames:
        fb_frame_lines, fb_block_lines, fb_label_entities = _collect_frame_and_labels(
            doc, frame_lineweight, frame_color, check_layer=False)
        fb_frames = detect_drawing_frames(fb_frame_lines, snap)
        if fb_frames:
            frames = fb_frames
            block_lines_for_info_frame = fb_block_lines
            fallback_label_points = []
            for it in fb_label_entities:
                _, clean_text, (x, y) = extract_text_from_entity(it)
                if clean_text:
                    fallback_label_points.append((clean_text, x, y))

    result['frames'] = frames

    if not frames:
        result['error'] = (
            f'図面枠（太さ {frame_lineweight} の線で囲まれた枠）が見つかりませんでした。'
        )
        return result

    label_points = []
    for it in label_entities:
        _, clean_text, (x, y) = extract_text_from_entity(it)
        if clean_text:
            label_points.append((clean_text, x, y))

    # 図面情報枠（タイトルブロック）の検出には、フォールバックが発動した場合
    # フォールバック側のラベル（非表示のタイトル項目名を含む）を使う——非表示の
    # タイトルブロックそのものを情報枠として認識できないと、次段の「情報枠外」
    # フィルタが働かず、タイトルブロックの文字が出力ラベルに混入してしまう。
    label_points_for_info_frame = (
        fallback_label_points if fallback_label_points is not None else label_points
    )

    info_frame_bboxes = []
    frames_without_info = 0
    for (xl, xr, y0, y1) in frames:
        bbox = _detect_info_frame_bbox(
            block_lines_for_info_frame, label_points_for_info_frame, xl, xr, y0, y1)
        if bbox is not None:
            info_frame_bboxes.append(bbox)
        else:
            frames_without_info += 1
    result['frames_without_info_frame'] = frames_without_info

    seen = set()
    labels = []
    for (text, x, y) in label_points:
        in_frame = any(
            xl - 1 <= x <= xr + 1 and y0 - 1 <= y <= y1 + 1
            for (xl, xr, y0, y1) in frames
        )
        if not in_frame:
            continue
        in_info_frame = any(
            lx - 1 <= x <= rx + 1 and by - 1 <= y <= ty + 1
            for (lx, rx, by, ty) in info_frame_bboxes
        )
        if in_info_frame:
            continue
        key = (text, round(x, 1), round(y, 1))
        if key in seen:
            continue
        seen.add(key)
        labels.append((text, x, y))

    result['labels'] = labels
    return result


def _collect_all_labels_fallback(dxf_file: str, doc=None) -> List[Tuple[str, float, float]]:
    """図面枠が検出できない場合のフォールバック: 図面枠フィルタなしで
    ファイル全体のラベルを収集する。

    レイアウトの選び方は `common_utils.select_layout_result()` を参照
    （Model Space に何かあれば常にそちらを使い、完全に空の場合のみ他
    レイアウトを順に試す）。

    非表示エンティティ（invisible属性・レイヤーoff/frozen）は除外する
    （2026-09-23追加。従来この経路にはis_invisible()チェックが無く、図面枠が
    検出できずこのフォールバックに落ちた図面〈唯一のタイトルブロックが
    off/frozenレイヤーにある等〉で、非表示のタイトルブロックの文字が
    出力ラベルに混入していた。`_collect_frame_and_labels()`と同様、
    直接配置・INSERT自身・virtual_entities展開後の3箇所すべてでチェックする）。

    `doc` に読み込み済みの `ezdxf.Drawing` を渡すと再読み込みしない（読み取りのみ）。"""
    if doc is None:
        try:
            doc = ezdxf.readfile(dxf_file)
        except Exception:
            return []
    text_block_cache = {}

    def collect_from_layout(layout):
        out = []
        for e in layout:
            if is_invisible(e):
                continue
            t = e.dxftype()
            if t in ('TEXT', 'MTEXT'):
                _, clean_text, (x, y) = extract_text_from_entity(e)
                if clean_text:
                    out.append((clean_text, x, y))
            elif t == 'INSERT':
                if not _block_has_text_content(doc, e.dxf.name, text_block_cache):
                    continue
                try:
                    for v in e.virtual_entities():
                        if is_invisible(v):
                            continue
                        if v.dxftype() in ('TEXT', 'MTEXT'):
                            _, clean_text, (x, y) = extract_text_from_entity(v)
                            if clean_text:
                                out.append((clean_text, x, y))
                except Exception:
                    pass
        return out

    return select_layout_result(doc, collect_from_layout, is_empty=lambda r: not r)


# ============================================================
# 3. ラベル正規化・候補パターン判定（座標付き）
# ============================================================

def normalize_labels(
    labels: List[Tuple[str, float, float]],
) -> List[Tuple[str, float, float]]:
    """(text,x,y) リストの text を NFKC 正規化+前後空白除去する（座標は保持）。
    正規化後に空文字列になったものは除く。"""
    out = []
    for (t, x, y) in labels:
        nt = normalize_label(t)
        if nt:
            out.append((nt, x, y))
    return out


# 「機器符号候補」列の値（v3.10.0、機器符号一覧のパイプライン組み込みに伴い
# 'Y'/None の2値から3値に変更）。定数化してタイプミスを防ぐ。
MARK_DEFINED = 'DEF'
MARK_CANDIDATE = 'CAN'


def designator_mark(text: str, master_index=None) -> Optional[str]:
    """textの「機器符号候補」列の値（'DEF'/'CAN'/None）を返す（v3.10.0）。

    判定順序は固定: (1) `master_index`（`designator_master.
    DesignatorMasterIndex`）が渡され、かつ機器符号一覧に含まれていれば
    'DEF'。(2) 一覧に含まれない、または `master_index=None`（一覧が利用
    できない環境）でも、候補パターンに一致すれば 'CAN'（旧 'Y' と同じ判定、
    `is_ref_designator_label()` を再利用）。(3) どちらにも該当しなければ
    None（空欄）。

    一覧にある符号は実測上すべて候補パターンにも一致するため（2026-10実測、
    `docs/REF_DESIGNATOR.md`「機器符号一覧」節参照）、実務上 (1)(2) の順序が
    結果に影響するケースは無いが、設計上は一覧判定を優先する順序で固定する。

    `master_index=None`（鍵未設定・一覧ファイル無し・鍵不一致のいずれか）の
    ときは常に旧来どおり 'CAN'/None の2値のみを返す（DEFは出ない）。
    """
    if master_index is not None and master_index.contains(text):
        return MARK_DEFINED
    if is_ref_designator_label(text):
        return MARK_CANDIDATE
    return None


def classify_labels(
    labels: List[Tuple[str, float, float]],
) -> List[Tuple[str, float, float]]:
    """正規化済み (text,x,y) リストから、機器符号（候補）パターンに一致する
    ものだけを抽出して返す（`is_ref_designator_label()` 参照）。一致すれば
    除外・確認なしにそのまま全件を候補として採用する（2026-07-30、判定条件の
    簡素化）。

    v3.0.0時点では抽出パイプライン本体からは呼ばれない（`extract_ref_designator_data()`
    が枠内の全ラベルを保持するようになったため）。機器符号判定ロジック単体の
    テスト・外部からの利用のために残している。
    """
    return [item for item in labels if is_ref_designator_label(item[0])]


# ============================================================
# 4. 通常モード（領域なし）用トップレベル関数
# ============================================================

def extract_ref_designator_data(
    dxf_file: str,
    frame_lineweight: int = 100,
    frame_color: int = _FRAME_COLOR,
    original_filename: Optional[str] = None,
) -> Dict:
    """通常モード（領域なし）用: 図面枠内・図面情報枠外の全ラベルを収集する。

    v3.0.0（2026-08）で機器符号パターンによる絞り込みを廃止し、枠内・情報枠外の
    全ラベルを保持するように変更した（旧「機器符号（候補）以外も抽出」オプション
    削除に伴い、機器符号候補かどうかはラベルごとの属性として出力する方針に統一。
    `is_ref_designator_label()` 参照。行データ生成時（`build_labeled_rows()` 等）に
    ラベル単位で判定する）。

    戻り値 dict:
      filename: str
      labels: [(ラベル, x, y), ...]（正規化済み、図面枠内・情報枠外の全ラベル）
      total_in_frame: int（= len(labels)）
      frames: int（検出した図面枠の数＝ページ数。図面枠が検出できない場合は0）
      warning: str | None （図面枠・図面情報枠が見つからずフォールバックした場合等）
    """
    info = {
        'filename': original_filename or dxf_file,
        'labels': [],
        'total_in_frame': 0,
        'frames': 0,
        'warning': None,
    }

    # 1回だけ読み込み、図面枠検出とフォールバックの両方で使い回す（従来はフォールバック
    # 時に同じファイルを2回読み込んでいた。40MBで約2.8秒の無駄）。読み込みに失敗した
    # ときは doc=None のまま渡し、各関数が従来どおりの失敗処理（error 設定・空リスト）を行う。
    try:
        doc = ezdxf.readfile(dxf_file)
    except Exception:
        doc = None
    collected = collect_in_frame_labels(dxf_file, frame_lineweight, frame_color, doc=doc)
    info['frames'] = len(collected.get('frames', []))
    if collected['error']:
        # フォールバック自体は正常な抽出結果につながる（実際に正しく機器符号が
        # 抽出できるケースがほとんど）ため、「見つかりませんでした」という
        # 失敗を思わせる文言は避け、実施した内容を淡々と伝える言い回しにする
        # （ユーザー指摘、2026-07-14）。
        info['warning'] = (
            f'図面枠（太さ {frame_lineweight} の線で囲まれた枠）を検出できなかったため、'
            '図面枠内の制約なしに全ラベルを抽出しました。'
        )
        raw_labels = _collect_all_labels_fallback(dxf_file, doc=doc)
    else:
        raw_labels = collected['labels']
        if collected.get('frames_without_info_frame'):
            info['warning'] = (
                f"{collected['frames_without_info_frame']}個の図面枠で図面情報枠"
                "（タイトルブロック）を検出できなかったため、該当する図面枠については"
                "情報枠内ラベルを除外せずに抽出しました。"
            )

    normalized = normalize_labels(raw_labels)
    info['total_in_frame'] = len(normalized)
    info['labels'] = normalized
    return info


# ============================================================
# 5. 領域付きモード用: 領域名を付与した行データの構築
# ============================================================

def _aggregate_assigned_labels(assigned):
    """`assign_region_labels()` の出力 `[(text,x,y,names), ...]` から集計辞書を
    作る（`build_labeled_rows`〈named_regions 指定時〉と `build_region_output`
    で共用する自己完結的な集計ロジック）。

    戻り値: (cnt, region_of, in_region_count, label_count_per_region, region_label_counts)
      cnt: {ラベル: 出現数}
      region_of: {ラベル: {所属領域名, ...}}
      in_region_count: いずれかの領域に所属するラベルの出現件数
      label_count_per_region: {領域名: そのラベル出現数の合計}
      region_label_counts: {領域名: Counter({ラベル: 出現数})}
    """
    cnt = Counter()
    region_of = defaultdict(set)
    in_region_count = 0
    label_count_per_region = defaultdict(int)
    region_label_counts = defaultdict(Counter)
    for (text, _x, _y, names) in assigned:
        cnt[text] += 1
        if names:
            in_region_count += 1
        for n in names:
            region_of[text].add(n)
            label_count_per_region[n] += 1
            region_label_counts[n][text] += 1
    return cnt, region_of, in_region_count, label_count_per_region, region_label_counts


def build_labeled_rows(
    labels: List[Tuple[str, float, float]],
    named_regions: Optional[List[dict]] = None,
    master_index=None,
) -> List[dict]:
    """(text,x,y) リストから (機器符号候補,ラベル,個数[,領域]) の行データを作る。

    '機器符号候補' は `designator_mark()` の値（'DEF'/'CAN'/None、v3.10.0。
    旧 'Y'/None の2値から、機器符号一覧〈`master_index`〉に一致するものを
    'DEF' として区別する3値に変更）。

    named_regions が指定された場合は `assign_region_labels()` で領域名を
    割り当て、'領域' キー（カンマ区切り文字列）を各行に含める。
    """
    if named_regions is not None:
        assigned = assign_region_labels(labels, named_regions)
        cnt, region_of, _in_region_count, _label_count_per_region, _region_label_counts = \
            _aggregate_assigned_labels(assigned)
        return [
            {'機器符号候補': designator_mark(t, master_index),
             'ラベル': t, '個数': cnt[t], '領域': ', '.join(sorted(region_of[t]))}
            for t in sorted(cnt.keys())
        ]

    cnt = Counter(t for (t, _x, _y) in labels)
    return [
        {'機器符号候補': designator_mark(t, master_index), 'ラベル': t, '個数': cnt[t]}
        for t in sorted(cnt.keys())
    ]


def extract_ref_designator_rows(
    dxf_file: str,
    frame_lineweight: int = 100,
    frame_color: int = _FRAME_COLOR,
    original_filename: Optional[str] = None,
    named_regions: Optional[List[dict]] = None,
) -> Dict:
    """通常/領域付き両モード共用のトップレベル関数。

    図面枠内・図面情報枠外の全ラベルの行データ（機器符号候補・ラベル・個数[・領域]）
    を返す（v3.0.0、機器符号候補以外のラベルも含む）。named_regions を渡すと
    各行に '領域' キーが付与される（領域付きモード用）。

    戻り値 dict:
      filename, warning, total_in_frame, frames: extract_ref_designator_data と同じ
      rows: [{'機器符号候補':str|None,'ラベル':str,'個数':int[,'領域':str]}]
    """
    info = extract_ref_designator_data(dxf_file, frame_lineweight, frame_color, original_filename)
    return {
        'filename': info['filename'],
        'warning': info['warning'],
        'total_in_frame': info['total_in_frame'],
        'frames': info['frames'],
        'rows': build_labeled_rows(info['labels'], named_regions),
    }


def unnamed_output_names(analysis: dict) -> Dict[int, str]:
    """名称候補が1つも無い領域（無名領域）の出力名を `{領域id: 名前}` で返す。

    名前は `"no name"`（この図面に無名領域が1つだけのとき）、複数あれば図面内で
    `"no name 1"`・`"no name 2"`…と領域の並び順に通し番号を付ける（番号は複数の無名領域を
    区別するためだけのもので、1つなら付けない。2026-07-23 ユーザー指摘）。
    `build_named_regions()`・`build_all_regions_summary()` と「領域一覧」の表示名が
    同じ規則を使うための唯一の実装。"""
    unnamed = [reg for reg in analysis.get('regions', []) if not reg.get('name_candidates')]
    return {
        reg['id']: ("no name" if len(unnamed) == 1 else f"no name {i}")
        for i, reg in enumerate(unnamed, start=1)
    }


def unnamed_region_label(output_name: str, area_pct: float) -> str:
    """「領域一覧」に出す無名領域の表示名（例 `no name 2（65%）`）。ファイル名は含めない
    （見出しに表示されているため。2026-10-10 ユーザー指示）。同じ表示名が複数ファイルに
    出た場合は1行にまとまり、「特定」「除外」は該当する全領域に効く。"""
    return f"{output_name}（{area_pct:.0f}%）"


def unnamed_region_labels(analysis: dict) -> Dict[int, str]:
    """無名領域の `{領域id: 「領域一覧」の表示名}`（1ファイル分）。"""
    names = unnamed_output_names(analysis)
    return {
        reg['id']: unnamed_region_label(names[reg['id']], reg.get('area_pct', 0))
        for reg in analysis.get('regions', []) if reg['id'] in names
    }


def build_named_regions(
    analysis: dict,
    name_selections: dict,
    fname: str,
    start_no_name_idx: int = 0,
) -> Tuple[List[dict], int]:
    """領域付きモード用: 解析結果とユーザーが確定した名称選択から `assign_region_labels()`
    に渡す named リストを構築する（かつて存在した `region_detector.build_region_results()`
    〈v3.0.0で削除、旧「機器符号（候補）以外も抽出」オプション専用〉の named 構築部分を
    機器符号抽出パイプライン向けに複製したものが由来。region_detector.py の共有ロジック
    には手を入れないため、ここに独立して実装している）。

    `name_selections[(fname, 領域id)] == ['']` は「採用しない」の印（無名領域を
    「領域一覧」で「特定」OFFにした場合。`view.region_selection.gather_name_selections()`
    が付ける）。その領域は named に含めない。

    戻り値: (named, next_no_name_idx)
    """
    named = []
    regions = analysis.get('regions', [])
    # 無名領域の出力名は `unnamed_output_names()`（「領域一覧」の表示名と同じ規則）。
    # 「特定」OFFの無名領域がある場合も、番号は一覧の表示名と食い違わない。
    no_name_by_id = unnamed_output_names(analysis)
    no_name_idx = start_no_name_idx + len(no_name_by_id)
    for reg in regions:
        chosen_names = name_selections.get((fname, reg['id']), [])
        if chosen_names == ['']:
            continue  # 無名領域を「領域一覧」で「特定」OFFにした（採用しない）
        if not chosen_names and reg['id'] in no_name_by_id:
            chosen_names = [no_name_by_id[reg['id']]]
        for nm in chosen_names:
            if not nm:
                continue
            named.append({
                'polygon': reg['polygon'], 'name': normalize_width(nm),
                'id': reg['id'], 'frame': reg['frame'], 'area_pct': reg['area_pct'],
            })
    return named, no_name_idx


def excluded_region_ids(analysis: dict, excluded_names) -> set:
    """名称候補のいずれかが「除外」指定された領域の id 集合を返す（v3.11.0新設）。

    `excluded_names` は `view.region_selection.global_excluded_region_names()`
    が返す、NFKC正規化（`normalize_width()`）済みの名称集合。この関数側でも
    候補テキストを `normalize_width()` してから比較する（全角/半角表記の
    揺れを吸収する既存の規約、`region_detector._matched_checked_candidates()`
    と同じ）。

    名称候補が1件も無い領域（無名領域）は、「領域一覧」の表示名
    （`unnamed_region_label()`。例 `no name 2（65%）`）が `excluded_names` に
    含まれれば除外する（2026-10-10 ユーザー決定。以前は無名領域は除外対象外だった）。
    候補のうち1件でも一致すれば、どの名前を採用するかに関わらずその領域全体を除外する
    （「領域の確認」での個別選択は不要、2026-10-08 ユーザー決定）。
    """
    if not excluded_names:
        return set()
    ids = set()
    for reg_id, label in unnamed_region_labels(analysis).items():
        if label in excluded_names:
            ids.add(reg_id)
    for reg in analysis.get('regions', []):
        for (_dist, text) in reg.get('name_candidates', []):
            if normalize_width(text) in excluded_names:
                ids.add(reg['id'])
                break
    return ids


def filter_labels_outside_excluded(
    labels: List[Tuple[str, float, float]],
    excluded_polygons: List[list],
) -> List[Tuple[str, float, float]]:
    """除外領域（境界線上を含む）の内側にあるラベルを取り除いたリストを返す
    （v3.11.0新設）。

    `build_all_regions_summary()` と同じ手法（領域を一時的な疑似名付きの
    named リストにして `assign_region_labels()` を呼ぶ）を使う。
    `assign_region_labels()` 内部の `_point_in_polygon(boundary_eps=1e-4)` が
    境界線上の点を内側として扱うため、「領域線上も含めて除外」はこの既存判定
    のままで満たされる。入れ子（除外領域の内側に別の特定領域がある）の場合も、
    外側の除外領域に内包される時点でラベルは除去される（2026-10-08
    ユーザー承認）。

    判定はラベルのアンカー点 (x, y) のみで行う（テキストのbboxは見ない）。
    既存の `領域` 列の割り当て（`assign_region_labels()` 自体）と同じ規約に
    意図的に合わせているため、ここだけ別の判定基準を導入しないこと。
    """
    if not excluded_polygons:
        return list(labels)
    pseudo_named = [
        {'polygon': poly, 'name': f'excl-{i}'} for i, poly in enumerate(excluded_polygons)
    ]
    assigned = assign_region_labels(labels, pseudo_named)
    return [(t, x, y) for (t, x, y, names) in assigned if not names]


def build_region_output(
    labels: List[Tuple[str, float, float]],
    named: List[dict],
    sort_value: str = 'asc',
    master_index=None,
) -> Dict:
    """(text,x,y) リストと named（`build_named_regions()` の出力）から、
    `create_region_excel_output()` に渡せる1ファイル分の集計結果を作る。

    かつて存在した `region_detector.build_region_results()`（v3.0.0で削除）の
    集計部分（1ファイル分）を機器符号抽出パイプライン向けに複製したものが由来
    （region_detector.py の共有ロジックには手を入れないため、ここに独立して
    実装している）。

    戻り値 dict: rows（'機器符号候補' 列付き、v3.10.0で'DEF'/'CAN'/Noneの3値に
      変更。`designator_mark()` 参照）, named（label_count 付与済み）,
      in_region_count, region_label_counts
    """
    assigned = assign_region_labels(labels, named)
    cnt, region_of, in_region_count, label_count_per_region, region_label_counts = \
        _aggregate_assigned_labels(assigned)

    rows = [
        {'機器符号候補': designator_mark(t, master_index),
         'ラベル': t, '個数': cnt[t], '領域': ', '.join(sorted(region_of[t]))}
        for t in cnt
    ]
    if sort_value == 'asc':
        rows.sort(key=lambda r: r['ラベル'])
    elif sort_value == 'desc':
        rows.sort(key=lambda r: r['ラベル'], reverse=True)

    for r in named:
        r['label_count'] = label_count_per_region.get(r['name'], 0)

    return {
        'rows': rows,
        'named': named,
        'in_region_count': in_region_count,
        'region_label_counts': {n: dict(c) for n, c in region_label_counts.items()},
    }


def build_ref_designator_final(
    ref_data_by_file: Dict[str, Dict],
    sort_value: str = 'asc',
    master_index=None,
) -> Dict:
    """通常モード用。`extract_ref_designator_data()` の結果（ファイル名→dict）から
    `create_ref_designator_excel_output()` に渡せる ref_final を構築する。

    `ref_designator_count`（v3.0.0新設）は rows のうち '機器符号候補' が
    'DEF'/'CAN'（v3.10.0で3値化。旧'Y'に相当）の行の '個数' 合計
    （Summary シートの「機器符号候補ラベル数」に使う。DEF/CANの合算のため
    `master_index` の有無に関わらず値は不変）。
    """
    ref_final = {}
    for fname, data in ref_data_by_file.items():
        rows = build_labeled_rows(data['labels'], master_index=master_index)
        if sort_value == 'desc':
            rows.sort(key=lambda r: r['ラベル'], reverse=True)
        ref_designator_count = sum(
            r['個数'] for r in rows if r['機器符号候補'] in (MARK_DEFINED, MARK_CANDIDATE))
        ref_final[fname] = {
            'rows': rows,
            'total_in_frame': data['total_in_frame'],
            'frames': data.get('frames', 0),
            'ref_designator_count': ref_designator_count,
            'warning': data.get('warning'),
            'main_drawing_number': data.get('main_drawing_number'),
            'source_drawing_number': data.get('source_drawing_number'),
            'title': data.get('title'),
            'subtitle': data.get('subtitle'),
        }
    return ref_final


def build_all_regions_summary(
    analysis: dict,
    name_selections: dict,
    fname: str,
    labels: List[Tuple[str, float, float]],
    master_index=None,
    excluded_ids=frozenset(),
    named_ids=frozenset(),
) -> List[dict]:
    """「領域一覧」シート用: 検出済みの**全領域**（確定・未確定を問わない）について、
    領域名・面積率・領域内ラベル数・領域内機器符号候補数を1ファイル分構築する。

    `build_named_regions()` は「確定済み」または「候補が元々無い（no name）」領域
    しか対象にしない（候補はあるがユーザーが未確定の領域は丸ごと除外される）ため、
    「領域一覧」用には独立に全領域を走査する（v3.0.0、要求「検出結果にかかわらず
    検出した領域をすべて記載」への対応）。

    領域名:
      - 確定済み（`name_selections` に選択あり）: その名前
      - 候補はあるが未確定: 候補をカンマ区切りで列挙（採否に関わらず全候補、
        Tier→距離順）
      - 候補なし: "no name"（`build_named_regions()` と同じ番号付け規則。
        この場合は自動確定として扱う）

    領域内ラベル数・領域内機器符号候補数は、確定状況に関わらずポリゴン内包判定で
    数える（`assign_region_labels()` を、確定名の代わりに領域 id をキーにした
    仮の named リストで呼び出すことで、確定済み領域用の `build_region_output()`
    と同じ判定ロジックを再利用する）。ラベル側の '領域' 列・領域別ラベル一覧には
    含めない（未確定領域の名称候補をラベルの所属先として確定させないため。
    意図的な非対称、2026-08 ユーザー確認）。

    `labels` には**除外前**の全ラベルを渡すこと（v3.11.0、呼び出し元が除外後の
    リストを渡すと「何件除外されたか」が分からなくなる。`領域内ラベル数`/
    `領域内機器符号候補数`は常に除外前の件数を表示する、2026-10-08ユーザー決定）。

    `excluded_ids`/`named_ids`（v3.11.0新設）: 各行に `'mark'`
    （`'除外'`/`'特定'`/`''`）を付与する。`excluded_ids` に含まれる領域は
    `'除外'`（`'特定'`かどうかより優先）、`named_ids`（`build_named_regions()`
    が返した named の id集合）に含まれる領域は `'特定'`、それ以外は `''`
    （候補はあるがどれも特定指定されていない領域）。

    Returns:
        list[dict]: [{'id', 'frame', 'name', 'area_pct', 'label_count',
                       'ref_designator_count', 'mark'}, ...]（regions の順）
    """
    regions = analysis.get('regions', [])

    # 'name' キーは assign_region_labels() 内で `if nm and ...` と真偽値判定
    # されるため、reg['id'] をそのまま使うと id=0 の領域が偽値扱いで無視されて
    # しまう（実際に発生したバグ）。常に truthy な文字列にする。
    def _pseudo_name(region_id):
        return f"region-{region_id}"

    pseudo_named = [{'polygon': reg['polygon'], 'name': _pseudo_name(reg['id'])} for reg in regions]
    assigned = assign_region_labels(labels, pseudo_named)
    count_by_id = defaultdict(int)
    ref_count_by_id = defaultdict(int)
    for (text, _x, _y, names) in assigned:
        matched = designator_mark(text, master_index) is not None
        for nm in names:
            count_by_id[nm] += 1
            if matched:
                ref_count_by_id[nm] += 1

    # 無名領域の名前は `unnamed_output_names()`（build_named_regions() と同じ規則）。
    no_name_by_id = unnamed_output_names(analysis)

    rows = []
    for reg in regions:
        chosen = [c for c in name_selections.get((fname, reg['id']), []) if c]
        candidates = [text for (_dist, text) in reg.get('name_candidates', [])]
        if chosen:
            name = ', '.join(dict.fromkeys(chosen))
        elif candidates:
            name = ', '.join(candidates)
        else:
            name = no_name_by_id[reg['id']]
        pseudo = _pseudo_name(reg['id'])
        if reg['id'] in excluded_ids:
            mark = '除外'
        elif reg['id'] in named_ids:
            mark = '特定'
        else:
            mark = ''
        rows.append({
            'id': reg['id'],
            'frame': reg['frame'],
            'name': normalize_width(name),
            'area_pct': reg['area_pct'],
            'label_count': count_by_id.get(pseudo, 0),
            'ref_designator_count': ref_count_by_id.get(pseudo, 0),
            'mark': mark,
        })
    return rows


def build_ref_designator_region_results(
    ref_data_by_file: Dict[str, Dict],
    region_analyses: Dict[str, dict],
    name_selections_by_file: Dict[str, dict],
    sort_value: str = 'asc',
    master_index=None,
    excluded_names=frozenset(),
) -> Dict:
    """領域付きモード用。`extract_ref_designator_data()` の結果と領域検出結果から
    `create_region_excel_output()` に渡せる region_results を構築する。

    `ref_designator_count`（v3.0.0新設）は rows のうち '機器符号候補' が
    'DEF'/'CAN'（v3.10.0で3値化。旧'Y'に相当）の行の '個数' 合計
    （Summary シートの「機器符号候補ラベル数」に使う。値は `master_index`の
    有無に関わらず不変）。`subtitle` も併せて格納する（v3.0.0、Summary シート
    にサブタイトルが表示されていなかった不具合の修正）。`region_rows` は
    「領域一覧」シート用の全領域データ（`build_all_regions_summary()` 参照）。

    `excluded_names`（v3.11.0新設）: 「除外」指定された領域名の集合
    （`view.region_selection.global_excluded_region_names()` が返す、
    NFKC正規化済みの集合）。該当する領域（境界線上を含む）のラベルは
    `rows`・`total_in_frame`・`ref_designator_count` から完全に取り除かれる
    （2026-10-08ユーザー決定）。`region_rows`（領域一覧）には除外領域の行も
    残し、`mark`列に`'除外'`・ラベル数は**除外前**の件数を記載する
    （何件除外されたか検証できるようにするため）。
    """
    region_results = {}
    for fname, data in ref_data_by_file.items():
        analysis = region_analyses[fname]
        named, _ = build_named_regions(analysis, name_selections_by_file[fname], fname)

        excl_ids = excluded_region_ids(analysis, excluded_names)
        excl_polys = [reg['polygon'] for reg in analysis.get('regions', [])
                      if reg['id'] in excl_ids]
        kept_labels = filter_labels_outside_excluded(data['labels'], excl_polys)

        out = build_region_output(kept_labels, named, sort_value, master_index=master_index)
        ref_designator_count = sum(
            r['個数'] for r in out['rows'] if r['機器符号候補'] in (MARK_DEFINED, MARK_CANDIDATE))
        # 領域一覧（region_rows）には除外前の全ラベルを渡す（除外前の件数を
        # 表示するため）。rows/total_in_frame/ref_designator_count は除外後の
        # kept_labels から算出済み（上記）。
        region_rows = build_all_regions_summary(
            analysis, name_selections_by_file[fname], fname, data['labels'],
            master_index=master_index,
            excluded_ids=excl_ids,
            named_ids={r['id'] for r in named})
        region_results[fname] = {
            'rows': out['rows'],
            'named': out['named'],
            'frames': len(analysis.get('frames', [])),
            'regions_detected': len(analysis.get('regions', [])),
            'regions_named': len({r['id'] for r in named}),
            'total_in_frame': len(kept_labels),
            'ref_designator_count': ref_designator_count,
            'in_region_count': out['in_region_count'],
            'drawing_number': analysis.get('main_drawing_number') or '',
            'title': analysis.get('title'),
            'subtitle': analysis.get('subtitle'),
            'region_label_counts': out['region_label_counts'],
            'region_rows': region_rows,
        }
    return region_results
