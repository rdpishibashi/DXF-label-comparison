"""矩形領域（直交ポリゴン）検出モジュール

電気回路 DXF 内の閉領域（直交ポリゴン。四角形に限らない）を検出し、領域内
ラベルに領域名を付与する。

識別キー:
  - 図面枠      : lineweight=100 かつ color=7(ACI白) の線分
  - 領域境界線  : lineweight=25 かつ color=2(ACI黄) かつ線種が実質的に Continuous

モジュール内の構成（処理パイプラインの順）:
  1.  設定（DEFAULT_REGION_CONFIG）
  2.  DXFジオメトリ収集（_collect_region_geometry 系）
  3.  ポリゴン・点の幾何ユーティリティ（汎用、複数セクションから使われる）
  4.  線分処理の共通ユーティリティ（分類・クラスタリング・結合）
  5.  図面枠検出（detect_drawing_frames）
  6.  閉領域検出（半面探索・行き止まり枝、_detect_regions まで）
  7.  領域名称候補（Tier付き優先順位、region_name_candidates）
  8.  図面回転判定（90°回転対応）
  9.  タイトルブロック除外
  10. 領域検出実行（_run_region_detection）
  10b. 補完面解消（_resolve_complement_faces）
  11. トップレベル解析（公開API: analyze_dxf_regions, assign_region_labels）
"""
import math
import gc
import os
from collections import defaultdict

import ezdxf

from .extract_labels import extract_text_from_entity, extract_drawing_numbers
from .common_utils import normalize_width, select_layout_result, is_invisible
from .ref_designator_patterns import classify_judgment_detailed, matched_confirmed_category


# ============================================================
# 1. 設定
# ============================================================

DEFAULT_REGION_CONFIG = {
    'frame_lineweight': 100,    # 図面枠の線の太さ
    'frame_color': 7,           # 図面枠の色(ACI)。lineweight=100だけでは図面枠以外の
                                # 短い無関係な線分（実例: 色5の小さな線分群）も拾ってしまうため、
                                # 色も合わせて判定する（2026-06-24、サンプル137件で検証）。
    'region_lineweight': 25,    # 領域境界線の太さ
    'region_color': 2,          # 領域境界線の色(ACI)
    'snap': 2.0,                # 軸平行判定・レベルクラスタの許容誤差
    'face_snap': 0.5,           # 矩形を構成する線分同士の接続点(交点)の座標マージン
                                # （2026-10-10に0.1→0.5。角が0.1超ずれた外枠が閉領域にならず
                                # 検出されない図面があったため。変更は config.py の
                                # ADVANCED_OVERRIDES で上書き可）
    'merge_level_tol': 0.5,     # 共線セグメント結合時のレベル座標(縦=x/横=y)一致許容
                                # ※小さくする（別レベルの線=別矩形を結合しない）
    # ギャップ（隙間）の橋渡し方針：部品ラベルは縦線分だけを途切れさせるため、
    # 縦線分のギャップのみ橋渡しし、横線分のギャップは橋渡ししない（別矩形の取り込み防止）。
    'bridge_vertical_gaps': True,    # 縦線分(同一X)のギャップを橋渡しする
    'bridge_horizontal_gaps': False, # 横線分(同一Y)のギャップは橋渡ししない
    'corner_tol': 0.5,               # 縦線端点と横線端点が一致（コーナー）とみなす許容。
                                     # ギャップ両端にコーナー相手がいれば橋渡ししない。
    'span_level_merge': False,  # 共線結合のレベルを「スパンを構成した線分だけ」の平均で
                                # 算出する（既定はレベルクラスタ全体の平均）。レベル汚染
                                # フォールバック（analyze_dxf_regions 4パス目）が True で使う。
    'area_ratio': 0.05,         # 単独の領域の最小面積（枠面積比、整数%比較。v1.9.0で0.15から変更）
    'group_area_ratio': 0.10,   # 同名複数ピースを合算した場合の最小合計面積（枠面積比、整数%比較）
    'min_face_ratio': 0.005,    # 個々の閉領域として残す最小面積（枠面積比、ノイズ除去）
    'name_max_dist': 10.0,      # 名称ラベルの境界からの最大距離
    'name_min_dist': 1.0,       # 名称ラベルの境界からの最小距離（線分上=0 を除外）
    'name_min_letters': 3,      # 名称候補に必要な英字数
    'name_exclude_terms': ('NOTE', '☆'),  # 候補から除外する語（含む場合）
    'name_exclude_lowercase': True,  # 英小文字を含むラベルを名称候補から除外
    'exclude_titleblock': True, # 図番枠（タイトルブロック）を領域から除外
    'exclude_circuit_symbols': True,   # 機器符号(候補)を名称候補から除外
    'circuit_symbol_keep_terms': ('RACK',),  # この語を含むラベルは機器符号扱いしない（例 RACK1）
    'exclude_connection_point_regions': True,  # 境界に接続点(円)を持つ領域(配線ループ)を除外
    'connection_point_threshold': 1,    # 境界上の接続点がこの数(個数)以上なら除外
    'connection_point_margin': 0.05,   # 接続点が境界線上とみなす座標距離マージン
    'name_filter_prefixes': (),  # 指定した文字列のいずれかで名称候補が始まる領域は、
                                 # 面積閾値(area_ratio)を満たさなくても強制的に採用する
                                 # （前方一致、複数指定可）。判定は normalize_width() で
                                 # 半角化してから行う（4節「全角/半角混在への対応」の鉄則）。
                                 # min_face_ratio によるノイズ下限は救済しない。
}


def _area_ratio_met(area, frame_area, ratio):
    """面積比較を整数%で行う。

    UI表示（`{:.0f}%`、四捨五入）と同じ丸めで面積比を整数化し、設定値（比率、
    例 0.05）も同様に整数%化した上で `>=` 比較する。フロート同士の直接比較
    （`area >= frame_area * ratio`）だと、表示上「5%」に見える領域が僅かな
    誤差で4.99...%と評価され不採用になる、逆に「4%」表示なのに4.5%相当で
    採用されるといった、表示値と採否が食い違う挙動を防ぐ。
    """
    if frame_area <= 0:
        return False
    pct = round(100.0 * area / frame_area)
    ratio_pct = round(ratio * 100)
    return pct >= ratio_pct

# 内部定数（マジックナンバーの明示）
_FRAME_MARGIN = 5       # 図面枠フィルタリング時の座標マージン（枠境界に対し ±5 単位で線分を収集）
_MAX_FACE_NODES = 200_000  # 半面探索の暴走ループ防止: 頂点数がこれを超えたら無効とみなす


# ============================================================
# 2. DXFジオメトリ収集
# ============================================================

def _is_continuous_linetype(e, doc):
    """エンティティの線種が実質的に Continuous（実線）かどうかを判定する。

    PHANTOM（二点鎖線）等の装飾的な線種は、lineweight/color が境界線条件
    （region_lineweight/region_color）に一致していても、実際の閉領域の壁を
    表すものではない（手描き図面で「別位置案」やセンターライン的な意味で使われる）。
    `EE6313-546-01E.dxf` で、本来は単一の小さな実体（handle 21AB/21AC/219A/219E、
    Continuous）である "MX CHAMBER" の周囲に、別の handle（21AE/21A1/21A9/2198等、
    PHANTOM）で描かれた二点鎖線の矩形が重なっており、これも境界線として誤認識し、
    本来存在しない「くり抜き」形状の領域が検出される不具合が報告された
    （ユーザー確認: linetype=PHANTOM の矩形は実体ではない）。`linetype='ByLayer'`
    の場合はレイヤーの既定線種まで解決する。
    """
    lt = (getattr(e.dxf, 'linetype', None) or 'BYLAYER').upper()
    if lt == 'BYLAYER':
        layer = doc.layers.get(e.dxf.layer) if doc else None
        lt = (layer.dxf.linetype if layer else 'CONTINUOUS').upper()
    return lt == 'CONTINUOUS'


def _collect_region_geometry(doc, cfg, check_layer=True):
    """図面枠線・領域境界線・テキスト・接続点（CIRCLE を含むブロックの
    INSERT 位置）を収集する。レイアウトの選び方は `select_layout_result()`
    を参照（Model Space に何かあれば常にそちらを使い、完全に空の場合のみ
    他レイアウトを順に試す。レイアウトをまたいで混在させない）。

    `check_layer=False`（2026-09-23追加）: `is_invisible()` のレイヤー単位
    off/frozen判定をスキップする。唯一のタイトルブロック（図面枠を含む）が
    off/frozenレイヤーに置かれている図面のフォールバック探索
    （`analyze_dxf_regions()` 参照）でのみ `False` を渡す。
    """
    flw = cfg['frame_lineweight']
    fcol = cfg['frame_color']
    rlw = cfg['region_lineweight']
    rcol = cfg['region_color']

    _circle_block = {}

    def block_has_circle(name):
        if name not in _circle_block:
            has = False
            try:
                blk = doc.blocks.get(name) if doc else None
                if blk is not None:
                    has = any(x.dxftype() == 'CIRCLE' for x in blk)
            except Exception:
                has = False
            _circle_block[name] = has
        return _circle_block[name]

    def collect_from_layout(layout):
        frame_lines = []
        region_lines = []
        region_lines_lp = []  # LWPOLYLINE 由来の境界線（LINE と分離して収集）
        label_entities = []
        connection_points = []

        def handle_line(e, owner_handle=None):
            lw = getattr(e.dxf, 'lineweight', None)
            col = getattr(e.dxf, 'color', None)
            if lw == flw and col == fcol:
                frame_lines.append((e.dxf.start, e.dxf.end))
            elif (lw == rlw and col == rcol
                  and _is_continuous_linetype(e, doc)):
                # 領域境界線のみ handle を保持する（行き止まり枝の報告用。
                # virtual_entities() 由来（INSERT 展開）は handle が None に
                # なるため、所属 INSERT の handle で代替）。
                region_lines.append((e.dxf.start, e.dxf.end, e.dxf.handle or owner_handle))

        def handle_lwpolyline_lp(e, owner_handle=None):
            """LWPOLYLINE の辺を LINE 相当として収集する（別リストへ）。"""
            lw = getattr(e.dxf, 'lineweight', None)
            if (lw != rlw or getattr(e.dxf, 'color', None) != rcol
                    or not _is_continuous_linetype(e, doc)):
                return
            try:
                pts = list(e.get_points())  # (x, y, bulge, start_width, end_width)
            except Exception:
                return
            n = len(pts)
            if n < 2:
                return
            handle = e.dxf.handle or owner_handle
            close_range = n if e.closed else n - 1
            for i in range(close_range):
                p0 = pts[i]
                p1 = pts[(i + 1) % n]
                if abs(p0[2]) > 1e-6:
                    continue
                region_lines_lp.append(((p0[0], p0[1]), (p1[0], p1[1]), handle))

        for e in layout:
            # invisible属性（非表示設定）が立ったエンティティは紙面に一切表示されない
            # ため、INSERT自身がinvisibleならその中身ごと丸ごと除外する（is_invisibleの
            # docstring参照。旧版タイトルブロックが図面枠・領域境界・領域名候補として
            # 誤検出されるのを防ぐ）。
            if is_invisible(e, check_layer=check_layer):
                continue
            t = e.dxftype()
            if t == 'LINE':
                handle_line(e)
            elif t == 'LWPOLYLINE':
                handle_lwpolyline_lp(e)
            elif t in ('TEXT', 'MTEXT'):
                label_entities.append(e)
            elif t == 'INSERT':
                if block_has_circle(e.dxf.name):
                    ins = e.dxf.insert
                    connection_points.append((ins[0], ins[1]))
                try:
                    for v in e.virtual_entities():
                        if is_invisible(v, check_layer=check_layer):
                            continue
                        vt = v.dxftype()
                        if vt == 'LINE':
                            handle_line(v, owner_handle=e.dxf.handle)
                        elif vt == 'LWPOLYLINE':
                            handle_lwpolyline_lp(v, owner_handle=e.dxf.handle)
                        elif vt in ('TEXT', 'MTEXT'):
                            label_entities.append(v)
                except Exception:
                    pass

        return frame_lines, region_lines, region_lines_lp, label_entities, connection_points

    return select_layout_result(doc, collect_from_layout, is_empty=lambda r: not any(r))


# ============================================================
# 3. ポリゴン・点の幾何ユーティリティ（汎用）
# ============================================================

def _polygon_area(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _polygon_corners(poly, tol=0.5):
    """ポリゴンの角（直角に折れる頂点）だけを抽出し、左下から順に並べて返す。

    面探索由来の共線中間点を除去し、最も左下（最小y→最小x）の角を先頭にする。
    """
    n = len(poly)
    out = []
    for i in range(n):
        p0 = poly[(i - 1) % n]
        p1 = poly[i]
        p2 = poly[(i + 1) % n]
        cross = (p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])
        if abs(cross) > tol:   # 折れ点（共線でない）→ 角
            out.append((round(p1[0], 2), round(p1[1], 2)))
    if not out:
        out = [(round(x, 2), round(y, 2)) for (x, y) in poly]
    start = min(range(len(out)), key=lambda i: (out[i][1], out[i][0]))
    return out[start:] + out[:start]


def _point_in_polygon(pt, poly, boundary_eps=1e-4):
    """ラベルが領域境界線ちょうど（浮動小数点誤差の範囲内）にある場合は内側扱いにする。

    手描き図面の座標は完全一致しないため、名称ラベル・機器符号が領域境界と
    ほぼ同一座標に配置されることがある。素の ray casting は境界上の点を
    浮動小数点誤差（1e-7オーダー）次第で内外どちらにも倒すため、同じ図面
    フォーマットの2図面で片方は境界のわずか内側・もう片方はわずか外側という
    座標になり、後者だけラベルが領域から漏れる不具合が実データで発生した
    （2026-07-23、CNPG01 が EE6491-039-21A.dxf で領域無しになる report）。
    """
    if _dist_point_to_polygon(pt, poly) <= boundary_eps:
        return True
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _dist_point_to_polygon(pt, poly):
    x, y = pt
    best = float('inf')
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        denom = dx * dx + dy * dy
        t = 0.0 if denom == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / denom))
        px, py = x1 + t * dx, y1 + t * dy
        best = min(best, math.hypot(x - px, y - py))
    return best


def _count_connection_points_on_boundary(polygon, points, margin):
    """ポリゴン境界から margin 以内にある接続点の数を返す（bbox で事前絞り込み）。"""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    x0, x1 = min(xs) - margin - 1, max(xs) + margin + 1
    y0, y1 = min(ys) - margin - 1, max(ys) + margin + 1
    n = 0
    for (px, py) in points:
        if x0 <= px <= x1 and y0 <= py <= y1:
            if _dist_point_to_polygon((px, py), polygon) <= margin:
                n += 1
    return n


def _polygon_sample_points(poly):
    """ポリゴンの頂点＋各辺の中点を返す（重なり判定のサンプル点）。

    辺の中点を含めるのは、両ポリゴンの頂点同士は互いの外側にあるが辺が交差して
    重なっているケース（斜めにずれて一部だけ重複する等）を、頂点だけのサンプルでは
    取り落とすため。"""
    pts = list(poly)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        pts.append(((x1 + x2) / 2.0, (y1 + y2) / 2.0))
    return pts


def _polygon_has_point_strictly_inside(pts, poly, tol):
    """pts のいずれかが poly の内部に（境界から `tol` より離れて）あるか。

    単に境界に接しているだけ（隣接する領域が壁を共有する等）は重なりとみなさない。
    """
    for pt in pts:
        if _point_in_polygon(pt, poly) and _dist_point_to_polygon(pt, poly) > tol:
            return True
    return False


def regions_overlap(poly_a, poly_b, tol=1.0):
    """2つの領域ポリゴンが重なっている（一方が他方に完全に内包される場合を含む、
    部分的な重複も含む）かを判定する。

    `EE6313-546-01E.dxf` の `B CHAMBER`（外側）と `BAKE HEATER UNIT RX`（内側、
    完全内包）はもちろん検出されるが、完全な内包に限らず、面積の一部だけが
    重なっているケースも対象に含める（ユーザー要望: 内包だけでなく重なって
    いる部分があれば同期しないとすべき）。単に境界が接している（隣接して壁を
    共有するだけ）場合は重なりとはみなさない。`app.py` の他領域への名称選択
    同期（MPD RACK2 のような空間的に分離した複数ピース合算を想定）が、こうした
    重なる領域同士を誤って同期してしまう不具合の対策として使う（v1.5.11、
    ユーザー報告: 領域1/2 のどちらかでデフォルトでない候補を手動選択すると、
    もう片方も同じ名称に同期されてしまう）。"""
    pts_a = _polygon_sample_points(poly_a)
    pts_b = _polygon_sample_points(poly_b)
    return (_polygon_has_point_strictly_inside(pts_a, poly_b, tol)
            or _polygon_has_point_strictly_inside(pts_b, poly_a, tol))


# ============================================================
# 4. 線分処理の共通ユーティリティ（分類・クラスタリング・結合）
# ============================================================

def _split_axis_aligned(pairs, eps):
    """線分(start,end[,handle])を水平 H[(y,x0,x1)] と垂直 V[(x,y0,y1)] に分類する。

    末尾の handle 要素（行き止まり枝の報告用に region_lines/region_lines_lp が
    持つ）は無視する。2要素(frame_lines)・3要素(region_lines系)のいずれも扱える。
    """
    H = []
    V = []
    for item in pairs:
        s, en = item[0], item[1]
        x1, y1, x2, y2 = s[0], s[1], en[0], en[1]
        if abs(y1 - y2) <= eps and abs(x1 - x2) >= eps:
            H.append(((y1 + y2) / 2.0, min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) <= eps and abs(y1 - y2) >= eps:
            V.append(((x1 + x2) / 2.0, min(y1, y2), max(y1, y2)))
    return H, V


def _cluster_1d(vals, tol):
    vals = sorted(vals)
    out = []
    cur = [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= tol:
            cur.append(v)
        else:
            out.append(sum(cur) / len(cur))
            cur = [v]
    out.append(sum(cur) / len(cur))
    return out


def _gap_has_circle(level, a, b, circles, band):
    """縦線分(level=x)のギャップ [a,b]（y方向）に接続点(円)が乗っているか判定する。"""
    if not circles:
        return False
    for (cx, cy) in circles:
        if abs(cx - level) <= band and a - band <= cy <= b + band:
            return True
    return False


def _has_corner_partner(level, y, h_endpoints, tol):
    """縦線端点 (level, y) に、横線分の端点が一致しているか（＝コーナー相手がいるか）。
    コーナー相手がいる端点は境界がそこで折れるので、ギャップ橋渡ししない。"""
    for (hx, hy) in (h_endpoints or ()):
        if abs(hx - level) <= tol and abs(hy - y) <= tol:
            return True
    return False


def _merge_collinear(items, level_tol, bridge=True, circles=None, circle_band=2.0,
                     h_endpoints=None, corner_tol=0.5, span_levels=False):
    """同一レベル(±level_tol)の共線セグメントを結合する。

    bridge=True のとき隙間（ギャップ）も橋渡しして1本にする（部品で途切れた縦線分の
    復元用）。bridge=False のときは重なり/接触するセグメントのみ結合し、隙間は別スパン
    として残す（横線分。別矩形の取り込みを防ぐ）。

    縦線のギャップ橋渡しは、**ギャップ両端のどちらにも横線分の端点が一致しない**場合
    のみ行う（端点が一致する＝コーナーで境界が折れるステップなので橋渡ししない。これに
    より、別境界片や段差を誤って繋がない）。circles がギャップ上にある場合も橋渡ししない。

    span_levels=True のとき、出力スパンのレベルを「そのスパンを構成した線分だけ」の
    平均で算出する（既定 False はレベルクラスタ全体の平均）。既定の全体平均は、スパンが
    重ならない無関係な近接線分（例: 境界線 y=122.00 の 0.37 上に乗ったコネクタ箱の底辺
    y=122.37）にレベルを汚染され、境界線がシフト → 縦線端点とのノード接続（face_snap）
    が切れて閉領域が不成立になることがある（EE6892-039-05B.dxf 2枠目で実証）。
    `analyze_dxf_regions` のレベル汚染フォールバック（4パス目）が True で再検出する。
    ギャップのコーナー相手・CIRCLE 判定は従来どおりクラスタ全体平均レベルで行う
    （判定許容誤差に対して汚染幅は level_tol 以下なので結果は変わらない）。
    """
    if not items:
        return []
    items = sorted(items, key=lambda t: t[0])
    groups = []
    cur = [items[0]]
    for it in items[1:]:
        if it[0] - cur[-1][0] <= level_tol:
            cur.append(it)
        else:
            groups.append(cur)
            cur = [it]
    groups.append(cur)

    out = []
    for g in groups:
        level = sum(t[0] for t in g) / len(g)
        # merged 要素: [lo, hi, [構成線分のレベル, ...]]
        spans = sorted((t[1], t[2], t[0]) for t in g)
        merged = [[spans[0][0], spans[0][1], [spans[0][2]]]]
        for lo, hi, lv in spans[1:]:
            phi = merged[-1][1]
            if lo <= phi + 1e-6:  # 重なり/接触 → 結合
                merged[-1][1] = max(phi, hi)
                merged[-1][2].append(lv)
            elif (bridge
                  and not _has_corner_partner(level, phi, h_endpoints, corner_tol)
                  and not _has_corner_partner(level, lo, h_endpoints, corner_tol)
                  and not _gap_has_circle(level, phi, lo, circles, circle_band)):
                merged[-1][1] = max(phi, hi)  # 橋渡し（両端コーナー無し・円無し）
                merged[-1][2].append(lv)
            else:
                merged.append([lo, hi, [lv]])  # 隙間 → 別スパンとして分離
        for lo, hi, lvs in merged:
            out.append((sum(lvs) / len(lvs) if span_levels else level, lo, hi))
    return out


# ============================================================
# 5. 図面枠検出
# ============================================================

def detect_drawing_frames(
    frame_lines: list,
    eps: float = 2.0,
    min_side: float = 0.0,
) -> list[tuple[float, float, float, float]]:
    """lineweight=100・color=7 の線分（呼び出し元の `_collect_region_geometry` で
    既にこの2条件で絞り込まれている）から図面枠（複数可）を検出する。
    戻り値: [(xl,xr,y0,y1), ...]

    注: 枠の辺が複数線分に分断されている場合（例: ブロック内で line が分割されて
    いるケース）でも正しく検出できるよう、分類後に共線セグメントを結合してから
    判定する。

    横辺（上辺・下辺）のペアで x スパンが一致するものを1つの枠の候補とし、
    その両端 x に対応する縦辺（左辺・右辺）が実在するかを確認して初めて枠として
    採用する（2026-07-26、高さの異なる複数枠が同一 x 位置に左辺を共有する
    ケースで不具合発覚・修正）。

    旧実装は縦辺のみを使い「全枠が同じ y 範囲を共有する」ことを前提に x 座標を
    左右ペアへクラスタ化していたが、`_merge_collinear` は同一 x（レベル）上で
    y 範囲が重なる線分を無条件で1本に結合するため、高さの異なる2つの枠が
    左辺を共有する場合（`EE6868-516-01A.dxf`: 枠A x:[25,584] y:[10,410]・
    枠B x:[25,821] y:[20,574]、両枠とも左辺が x≈25）に、枠A・枠Bの左辺
    （y:[10,410] と y:[20,574]、x=25で重複）が y:[10,574] という実在しない
    1本の縦辺へ誤結合され、さらに x クラスタが `[25,584,821]` の奇数個（本来
    4個のはずが左辺の共有で3個）になることでペアリングが崩れ、枠Bの右辺
    （821）が捨てられて枠Bが検出されず、実際には大面積の領域（`EFEM UPPER`・
    `SBS1`）を含む枠が丸ごと検出漏れになっていた。横辺は枠A・枠Bで y レベルが
    異なるため誤結合せず、x スパンの一致で正しく枠ごとに区別できる。
    """
    H, V = _split_axis_aligned(frame_lines, eps)
    # 接触/重複する同一レベル上の線分を1本に統合してから判定
    # （例: EE6888-631-01A.dxf の右辺が y=367.5 で2分割されているケース）
    Hm = _merge_collinear(H, eps, bridge=False)  # [(y, x0, x1), ...]
    Vm = _merge_collinear(V, eps, bridge=False)  # [(x, y0, y1), ...]
    if len(Hm) < 2 or len(Vm) < 2:
        return []

    def _has_vertical_coverage(x, y0, y1):
        return any(abs(vx - x) <= eps and vy0 - eps <= y0 and y1 <= vy1 + eps
                   for (vx, vy0, vy1) in Vm)

    Hs = sorted(Hm, key=lambda h: h[0])
    n = len(Hs)
    used = set()
    frames = []
    for i in range(n):
        if i in used:
            continue
        y_i, x0_i, x1_i = Hs[i]
        for j in range(i + 1, n):
            if j in used:
                continue
            y_j, x0_j, x1_j = Hs[j]
            if abs(x0_i - x0_j) > eps or abs(x1_i - x1_j) > eps:
                continue
            y0, y1 = min(y_i, y_j), max(y_i, y_j)
            if (y1 - y0) < min_side:
                continue
            xl, xr = min(x0_i, x1_i), max(x0_i, x1_i)
            if _has_vertical_coverage(xl, y0, y1) and _has_vertical_coverage(xr, y0, y1):
                frames.append((xl, xr, y0, y1))
                used.add(i)
                used.add(j)
                break
    frames.sort(key=lambda f: (f[0], f[2]))
    return frames


# ============================================================
# 6. 閉領域検出（半面探索・行き止まり枝）
# ============================================================

def _build_planar_graph(Hm, Vm, eps):
    """結合済み水平線 Hm[(y,x0,x1)]・垂直線 Vm[(x,y0,y1)] から、端点接続ベースの
    平面グラフ（隣接リスト adj とノード座標 node_xy）を構築する。

    接続は **線分の端点が相手の線分に乗っている箇所（角・T字）のみ** で作る。
    中ほど同士の交差（どちらの端点でもない交差）では接続しない。これにより、
    コネクタ横線が矩形右辺の途中を横切るだけの箇所で誤って繋がるのを防ぐ。
    座標は許容誤差クラスタリングで正規化する（round の境界で一致点が分裂するのを
    防ぐ。手描きの微小ズレ、例 y=231.91 と 231.96 を同一ノードに寄せる）。

    戻り値: (adj, node_xy)。adj は {node_key: {隣接node_key, ...}} の隣接リスト
    （無向グラフ、双方向に登録）。node_xy は {node_key: (x, y)} の実座標。
    """
    ctol = max(eps, 0.2)

    def _canon_map(values):
        sv = sorted(set(values))
        m = {}
        if not sv:
            return m
        cluster = [sv[0]]
        for v in sv[1:]:
            if v - cluster[-1] <= ctol:
                cluster.append(v)
            else:
                c = sum(cluster) / len(cluster)
                for u in cluster:
                    m[u] = c
                cluster = [v]
        c = sum(cluster) / len(cluster)
        for u in cluster:
            m[u] = c
        return m

    all_x = set()
    all_y = set()
    for (y, x0, x1) in Hm:
        all_y.add(y); all_x.add(x0); all_x.add(x1)
    for (x, y0, y1) in Vm:
        all_x.add(x); all_y.add(y0); all_y.add(y1)
    cx = _canon_map(all_x)
    cy = _canon_map(all_y)

    def cluster_key(x, y):
        return (round(cx[x], 3), round(cy[y], 3))

    v_endpoints = []
    for (x, y0, y1) in Vm:
        v_endpoints.append((x, y0))
        v_endpoints.append((x, y1))
    h_endpoints = []
    for (y, x0, x1) in Hm:
        h_endpoints.append((x0, y))
        h_endpoints.append((x1, y))

    node_xy = {}
    line_pts = {}
    # 横線上のノード = 自身の端点 ＋ そこに端点で接する縦線の位置
    for hi, (y, x0, x1) in enumerate(Hm):
        xs = [x0, x1]
        for (vx, vy) in v_endpoints:
            if x0 - eps <= vx <= x1 + eps and abs(vy - y) <= eps:
                xs.append(vx)
        for x in xs:
            k = cluster_key(x, y)
            node_xy[k] = (x, y)
            line_pts.setdefault(('H', hi), []).append((x, k))
    # 縦線上のノード = 自身の端点 ＋ そこに端点で接する横線の位置
    for vi, (x, y0, y1) in enumerate(Vm):
        ys = [y0, y1]
        for (hx, hy) in h_endpoints:
            if y0 - eps <= hy <= y1 + eps and abs(hx - x) <= eps:
                ys.append(hy)
        for yy in ys:
            k = cluster_key(x, yy)
            node_xy[k] = (x, yy)
            line_pts.setdefault(('V', vi), []).append((yy, k))

    adj = {}
    for pts in line_pts.values():
        pts = sorted(set(pts))
        for a in range(len(pts) - 1):
            ka, kb = pts[a][1], pts[a + 1][1]
            if ka != kb:
                adj.setdefault(ka, set()).add(kb)
                adj.setdefault(kb, set()).add(ka)
    return adj, node_xy


def _peel_dangling_branches(adj, node_xy):
    """次数1のノード（行き止まり）とその辺を再帰的に除去する（2-core抽出）。

    半面探索は次数1のノードに到達すると、戻る辺が1本しかないため必ず同じ辺を
    折り返す。この往復が生のポリゴンに「同じ座標が2回連続する」アーティファクトを
    生む（面積には寄与しないが、頂点座標の表示を汚す）。真の境界閉路は必ず次数2
    以上のノードのみで構成されるため、面探索前にここで除去する。

    `adj` は呼び出し側の辞書を**直接変更**する（除去後のグラフを面探索に渡すため）。

    除去した辺は「枝（連結成分）」単位にまとめて返す。1本の枝が複数の短い線分の
    連なりで構成される場合（部品が複数回切れ目を入れている、あるいは1本の長い
    線が途中まで領域境界として使われ残りが余剰になっている等）も、先端から
    現存グラフへの取り付け点までを1つの枝として扱う（Union-Find で連結成分化）。

    戻り値: [{'edges': [(座標, 座標), ...], 'attachment': 座標 | None}, ...]
    """
    peeled_pairs = []  # (leaf_key, other_key)、除去順
    changed = True
    while changed:
        changed = False
        leaves = [n for n, nbrs in adj.items() if len(nbrs) == 1]
        for leaf in leaves:
            nbrs = adj.get(leaf)
            if not nbrs:
                continue
            other = next(iter(nbrs))
            peeled_pairs.append((leaf, other))
            adj[other].discard(leaf)
            if not adj[other]:
                del adj[other]
            del adj[leaf]
            changed = True

    if not peeled_pairs:
        return []

    parent = {}

    def _uf_find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def _uf_union(a, b):
        ra, rb = _uf_find(a), _uf_find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in peeled_pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        _uf_union(a, b)

    groups = {}
    for a, b in peeled_pairs:
        groups.setdefault(_uf_find(a), []).append((a, b))

    dangling_branches = []
    for edges in groups.values():
        keys = {k for ab in edges for k in ab}
        attach_keys = [k for k in keys if k in adj]
        dangling_branches.append({
            'edges': [(node_xy[a], node_xy[b]) for a, b in edges],
            'attachment': node_xy[attach_keys[0]] if attach_keys else None,
        })
    return dangling_branches


def _trace_faces(adj, node_xy):
    """2-core抽出済みの平面グラフ（adj, node_xy）から、半面探索で閉領域(面)を
    列挙する。各面は次数2以上のノードのみで構成される閉路（半面探索＝各有向辺を
    1回ずつ辿り、各ノードで「来た方向の直前（角度順で1つ前）」の隣接辺へ進む）。"""
    def ang(a, b):
        ax, ay = node_xy[a]
        bx, by = node_xy[b]
        return math.atan2(by - ay, bx - ax)

    order = {n: sorted(nb, key=lambda mm: ang(n, mm)) for n, nb in adj.items()}
    visited = set()
    faces = []
    for u in list(adj.keys()):
        for v in adj[u]:
            if (u, v) in visited:
                continue
            face = []
            cu, cv = u, v
            ok = True
            while True:
                visited.add((cu, cv))
                face.append(node_xy[cu])
                nb = order[cv]
                w = nb[(nb.index(cu) - 1) % len(nb)]
                cu, cv = cv, w
                if (cu, cv) == (u, v):
                    break
                if len(face) > _MAX_FACE_NODES:
                    ok = False
                    break
            if ok and len(face) >= 4:
                faces.append(face)
    return faces


def _find_rectilinear_faces(Hm, Vm, eps):
    """結合済み水平線 Hm[(y,x0,x1)]・垂直線 Vm[(x,y0,y1)] から閉領域(面)と
    行き止まり枝を求める。

    `_build_planar_graph`（平面グラフ構築）→ `_peel_dangling_branches`
    （行き止まり枝の除去・連結成分化）→ `_trace_faces`（半面探索）の3段の
    オーケストレーション。戻り値: (faces, dangling_branches)。
    """
    adj, node_xy = _build_planar_graph(Hm, Vm, eps)
    if not adj:
        return [], []
    dangling_branches = _peel_dangling_branches(adj, node_xy)
    if not adj:
        return [], dangling_branches
    faces = _trace_faces(adj, node_xy)
    return faces, dangling_branches


def _branch_leaf_points(branch, attach_tol=0.5):
    """行き止まり枝(branch)の座標グラフの中で、取り付け点(attachment)以外の
    次数1端点(leaf、枝の"先端")を返す。通常は1点（単純な行き止まり）。"""
    edges = branch.get('edges') or []
    deg = {}
    for (p1, p2) in edges:
        deg[p1] = deg.get(p1, 0) + 1
        deg[p2] = deg.get(p2, 0) + 1
    attachment = branch.get('attachment')
    leaves = []
    for pt, d in deg.items():
        if d != 1:
            continue
        if attachment is not None and math.hypot(pt[0] - attachment[0], pt[1] - attachment[1]) <= attach_tol:
            continue
        leaves.append(pt)
    return leaves


def _resplit_face_with_dangling(face_poly, dangling_branches, Hm, Vm, fsnap, local_tol, margin=2.0):
    """1つの閉領域(face_poly)について、その境界に取り付く行き止まり枝のうち
    「反対側の端点(leaf)も同じ face_poly の境界に近接する」ものがあれば、
    face_poly の bbox 内だけに限定して面探索をやり直し、未接続だった内部の
    仕切り線を復元する。

    グローバルな face_snap やレベル平均化方式には一切触れない——溶接が必要と
    判明した face の bbox 内という限定された範囲でだけ、より緩い許容誤差
    （`local_tol`、merge_level_tol 由来の上限）で再接続を試みる。これにより、
    無関係な遠方の線分同士を誤って結合するリスクを避ける（`EE6888-650-01C.dxf`
    の FL1F①②③、`_merge_collinear` のレベル平均化ドリフトで内部仕切りの
    T字接合が face_snap=0.1 では繋がらず、外周だけの1領域に誤マージされていた
    不具合。グローバルな face_snap 拡大・レベル平均化方式変更はいずれも無関係な
    別ファイルの誤結合を誘発したため、この局所再トレース方式に切替。2026-07-15）。

    戻り値: (sub_faces | None, consumed_branch_ids)。sub_faces は再トレースで
    2面以上に分割できた場合のみ非Noneのリスト（元のfaceそのものも含む。
    合体親候補として後段のN子合体親解消に利用する）。consumed_branch_ids は
    溶接に使った枝を後段の dangling_edges 報告から除外するための識別子集合。
    """
    attach_tol = max(fsnap, 0.5)
    triggered = False
    consumed = set()
    for bi, br in enumerate(dangling_branches):
        att = br.get('attachment')
        if att is None or _dist_point_to_polygon(att, face_poly) > attach_tol:
            continue
        for leaf in _branch_leaf_points(br, attach_tol):
            if _dist_point_to_polygon(leaf, face_poly) <= local_tol:
                triggered = True
                consumed.add(bi)
                break
    if not triggered:
        return None, consumed

    xs = [p[0] for p in face_poly]
    ys = [p[1] for p in face_poly]
    bx0, bx1 = min(xs) - margin, max(xs) + margin
    by0, by1 = min(ys) - margin, max(ys) + margin
    local_H = [h for h in Hm if by0 <= h[0] <= by1 and h[2] >= bx0 and h[1] <= bx1]
    local_V = [v for v in Vm if bx0 <= v[0] <= bx1 and v[2] >= by0 and v[1] <= by1]
    sub_faces, _sub_dangling = _find_rectilinear_faces(local_H, local_V, local_tol)
    result = [f for f in sub_faces
              if all(bx0 - 1 <= x <= bx1 + 1 for x, _y in f)
              and all(by0 - 1 <= y <= by1 + 1 for _x, y in f)]
    if len(result) <= 1:
        return None, set()
    return result, consumed


def _resolve_dangling_handles(dangling_branches, raw_lines, tol=0.5):
    """行き止まり枝（枝ごとの端点ペアのリスト＋取り付け点）について、その経路上に
    ある元のLINE/LWPOLYLINE辺の handle と実座標（クラスタ正規化前）を解決する。

    raw_lines は (start, end, handle) の3要素タプルのリスト
    （`_collect_region_geometry` の region_lines / region_lines_lp）。
    1本の枝が複数の生エンティティ（複数の短い線分の連なり、または閉領域の境界
    として一部だけ使われている1本の長い線の余剰部分）で構成される場合も、枝を
    構成する全セグメントの延長線上にあり区間が重なる全エンティティを対象に含める
    （セグメントをまたいで同じ handle が重複してもエンティティ単位で重複除去する）。
    """
    results = []
    for branch in dangling_branches:
        entities = []
        seen = set()
        for (p1, p2) in branch['edges']:
            x1, y1 = p1
            x2, y2 = p2
            vertical = abs(x1 - x2) <= tol
            level = (x1 + x2) / 2.0 if vertical else (y1 + y2) / 2.0
            lo, hi = (min(y1, y2), max(y1, y2)) if vertical else (min(x1, x2), max(x1, x2))
            for (s, en, handle) in raw_lines:
                sx, sy = s[0], s[1]
                ex, ey = en[0], en[1]
                if vertical:
                    if abs(sx - level) > tol or abs(ex - level) > tol:
                        continue
                    seg_lo, seg_hi = min(sy, ey), max(sy, ey)
                else:
                    if abs(sy - level) > tol or abs(ey - level) > tol:
                        continue
                    seg_lo, seg_hi = min(sx, ex), max(sx, ex)
                if seg_hi < lo - tol or seg_lo > hi + tol:
                    continue
                key = handle if handle else (round(sx, 3), round(sy, 3), round(ex, 3), round(ey, 3))
                if key in seen:
                    continue
                seen.add(key)
                entities.append({
                    'handle': handle,
                    'start': (round(sx, 2), round(sy, 2)),
                    'end': (round(ex, 2), round(ey, 2)),
                })
        attachment = branch.get('attachment')
        results.append({
            'attachment': (round(attachment[0], 2), round(attachment[1], 2)) if attachment else None,
            'entities': entities,
        })
    return results


def _detect_regions(RH, RV, frame, frame_area, cfg, circles=None, raw_lines=None):
    """1つの図面枠内で、面積>=枠面積×area_ratio の閉領域を検出する。

    戻り値: (regions, dangling) のタプル。dangling は `_resolve_dangling_handles`
    の出力形式（枝＝連結成分ごとの attachment/handle/座標）。どの領域に関係する
    枝かの絞り込みは呼び出し側（`analyze_dxf_regions`）が `attachment` 座標と
    各領域の polygon で行う。
    """
    xl, xr, y0, y1 = frame
    m = _FRAME_MARGIN
    Hf = [h for h in RH if y0 - m <= h[0] <= y1 + m and h[2] >= xl - m and h[1] <= xr + m]
    Vf = [v for v in RV if xl - m <= v[0] <= xr + m and v[2] >= y0 - m and v[1] <= y1 + m]
    if not Hf or not Vf:
        return [], []
    # 共線セグメントの結合はレベル座標を厳密一致(merge_level_tol)で行い、別レベルの
    # 線（=別矩形）を誤って繋がない。ギャップ橋渡しは既定で縦線分のみ（部品ラベルは
    # 縦線分を途切れさせる）。横線分のギャップは既定では橋渡ししない。接続点(交点)判定
    # は face_snap。ギャップが CIRCLE で繋がっている場合は橋渡ししない（配線ループ除外）。
    # 図面全体が90°回転しているファイルでは部品が横線分を途切れさせるため、
    # bridge_horizontal_gaps=True 指定時は縦線分の端点をコーナー相手として
    # （x/y を入れ替えて）同じ安全条件で橋渡しする（_detect_regions を呼ぶ側が
    # 候補ゼロ時のフォールバックとして有効化する）。
    mtol = cfg.get('merge_level_tol', 0.5)
    fsnap = cfg.get('face_snap', 0.1)
    bridge_v = cfg.get('bridge_vertical_gaps', True)
    bridge_h = cfg.get('bridge_horizontal_gaps', False)
    cband = cfg.get('connection_point_margin', 2.0)
    ctol = cfg.get('corner_tol', 0.5)
    fcircles = [c for c in (circles or []) if xl - m <= c[0] <= xr + m and y0 - m <= c[1] <= y1 + m]
    # 横線分の端点（縦ギャップのコーナー相手判定用）
    h_endpoints = []
    for (hy, hx0, hx1) in Hf:
        h_endpoints.append((hx0, hy))
        h_endpoints.append((hx1, hy))
    # 縦線分の端点（横ギャップのコーナー相手判定用。x/y を入れ替えて _has_corner_partner
    # ／_gap_has_circle の (level, 位置) 引数順に合わせる）
    v_endpoints_swapped = []
    for (vx, vy0, vy1) in Vf:
        v_endpoints_swapped.append((vy0, vx))
        v_endpoints_swapped.append((vy1, vx))
    circles_swapped = [(cy, cx) for (cx, cy) in fcircles]
    span_levels = cfg.get('span_level_merge', False)
    Hm = _merge_collinear(Hf, mtol, bridge=bridge_h, circles=circles_swapped, circle_band=cband,
                          h_endpoints=v_endpoints_swapped, corner_tol=ctol,
                          span_levels=span_levels)
    Vm = _merge_collinear(Vf, mtol, bridge=bridge_v, circles=fcircles, circle_band=cband,
                          h_endpoints=h_endpoints, corner_tol=ctol,
                          span_levels=span_levels)
    # 端点接続ベースの面探索（中ほど交差では繋がない）ため、部品矩形の縦線は領域辺の
    # 途中を横切るだけで接続せず、回り込みは発生しない。
    faces, dangling = _find_rectilinear_faces(Hm, Vm, fsnap)
    # 局所修復（v1.9.1）: 行き止まり枝の反対端点も同じ face の境界に近接する場合、
    # その face の bbox 内だけに限定して再トレースし、内部仕切りの未接続T字を復元する
    # （`_resplit_face_with_dangling` 参照）。
    local_tol = max(mtol, 0.5)
    expanded_faces = []
    consumed_branch_idx = set()
    for f in faces:
        sub, consumed = _resplit_face_with_dangling(f, dangling, Hm, Vm, fsnap, local_tol)
        if sub:
            expanded_faces.extend(sub)
            consumed_branch_idx |= consumed
        else:
            expanded_faces.append(f)
    faces = expanded_faces
    if consumed_branch_idx:
        dangling = [br for bi, br in enumerate(dangling) if bi not in consumed_branch_idx]
    thr = frame_area * cfg.get('min_face_ratio', 0.005)
    regions = []
    seen = set()
    for f in sorted(faces, key=_polygon_area, reverse=True):
        a = _polygon_area(f)
        if a < thr:
            continue
        xs = [p[0] for p in f]
        ys = [p[1] for p in f]
        bb = (round(min(xs)), round(max(xs)), round(min(ys)), round(max(ys)))
        if bb in seen:
            continue
        seen.add(bb)
        regions.append({'polygon': f, 'area': a})
    dangling_resolved = _resolve_dangling_handles(dangling, raw_lines or [])
    return regions, dangling_resolved


# ============================================================
# 7. 領域名称候補（Tier付き優先順位）
# ============================================================

def _is_letter(ch):
    """半角・全角の英字（大小問わず）かどうかを判定する。

    手書き回路DXFには領域名がすべて全角（例: `ＳＹＳＴＥＭ　Ｉ／Ｆ　ＢＯＸ`）で
    書かれている図面があり、ASCII 限定判定では英字0字とみなされ
    `name_min_letters` 条件で常に除外されていた（`is_single_uppercase_letter()`
    の全角対応と同じ考え方）。
    """
    if ch.isascii() and ch.isalpha():
        return True
    return 'Ａ' <= ch <= 'Ｚ' or 'ａ' <= ch <= 'ｚ'


def _is_lowercase_letter(ch):
    if 'a' <= ch <= 'z':
        return True
    return 'ａ' <= ch <= 'ｚ'


def _count_letters(s):
    return sum(1 for ch in s if _is_letter(ch))


def _is_valid_name_candidate(t, min_letters, exclude_lowercase, exclude_terms,
                              exclude_circuit_symbols, circuit_keep_terms):
    """領域名候補ラベルとして有効かを返す（ポリゴン非依存フィルタ）。

    region_name_candidates() と _name_union_parent() の両方で使われる共通判定。
    """
    if _count_letters(t) < min_letters:
        return False
    if exclude_lowercase and any(_is_lowercase_letter(ch) for ch in t):
        return False
    # 除外語・keep-term の照合は半角相当で行う（機器符号フィルタ側も
    # normalize_width で判定するため、全角 ＲＡＣＫ１ 等が「機器符号として
    # 除外されるのに keep-term は素通り」という不整合を防ぐ）。
    up = normalize_width(t).upper()
    # 矩形領域名称の1文字目に "(" が来ることはない（例: 全角 "（ＣＬＯＳＥ）" は
    # 状態表示ラベルであり領域名ではない）。途中に "(" を含む名称
    # （例 "TMP (TMP-1003LM)"）は対象外。
    if up.strip().startswith('('):
        return False
    # 矩形領域名称は英大文字で始まる語であることを要求する（2026-10 ユーザー
    # 指示）。長い日本語の注記文（例: "注1) MD SHUTTER I/F B.Dの接地方法
    # （ノイズ対策)について..."）が、既存の除外語（'NOTE'・'☆'）に引っかから
    # ないまま領域名候補に混入する不具合の対策。長さによる制限は設けない
    # （"MD SHUTTER I/F B.D-Ⅵ" のような長い正当な名称は除外しないため）。
    stripped = up.strip()
    if not stripped or not ('A' <= stripped[0] <= 'Z'):
        return False
    if any(term.upper() in up for term in (exclude_terms or ())):
        return False
    if exclude_circuit_symbols and not any(k.upper() in up for k in (circuit_keep_terms or ())):
        # 2026-07-26 変更: `filter_non_circuit_symbols()`（"英字2文字以上"等の
        # 粗い正規表現）は、正当な領域名（例 `MFFX`）まで機器符号として誤って
        # 除外していた（ユーザー報告: `DE5027-563-03A.dxf` 図面1/領域3で
        # `MFFX` が名称候補にならない不具合。`filter_non_circuit_symbols(['MFFX'])`
        # が「英字のみ2文字以上」パターンに一致し除外していたことが判明）。
        # `ref_designator.py` の分類（`reference_designator_list.xlsx` の
        # `RemainingUnclassified`/`ConfirmedDesignators` で実データ検証済み）に
        # 判定を置き換える。`classify_judgment_detailed()` の3状態
        # （'no_match'/'excluded'/'candidate'）をそのまま使う必要がある——
        # ラッパーの `is_ref_designator_candidate()`（'candidate' のみ True）だけで
        # 判定すると、'no_match'（スペース等を含み機器符号の形にすら一致しない。
        # 例 `EFEM UPPER`・`SYSTEM I/F BOX` のような複数語の実在領域名）と
        # 'excluded'（`GND` 等、既知の非designator語で除外）を区別できず、
        # 複数語の正当な領域名まで丸ごとブロックする重大な回帰を起こした
        # （実装中に発覚・訂正）。
        #   - 'no_match'（形状自体が不一致。複数語・スラッシュ等を含む大半の
        #     実在領域名）→ 除外しない
        #   - 'excluded' かつ `unit_names` 以外（`GND`・`MOTOR`等、既知の
        #     非designator語。旧実装でも機器符号として除外されていた）→ 除外する
        #   - 'excluded' かつ `unit_names`（`CTC`・`EFEM`・`CASE`・`FOUP`・
        #     `SHIELD`等）→ 除外しない（2026-07-26 ユーザー報告で追加確認:
        #     `DE5327-405-26A.dxf` 図面1/領域2で `CTC` が領域名候補にならない
        #     不具合。`unit_names` は「ユニット/モジュール名」であり
        #     機器符号（候補）ではないという判定用のカテゴリで、領域名としては
        #     むしろ正当な候補である。common_nouns/cable_colors/titleblock_terms
        #     等の他の除外カテゴリとは意味が異なるため、`unit_names` のみ例外
        #     扱いする）
        #   - 'candidate' かつ確定 Reference Designator（`R10`・`CN3`・`CNCNT`〈追加
        #     機器符号パターン `added_desig:CN.*` に一致〉等）→ 除外する
        #   - 'candidate' かつ未確定（`RemainingUnclassified` 相当、例
        #     `MFFX`・`FB`）→ 除外しない（今回の主目的）
        # `RACK1` はこの判定に到達する前に `circuit_keep_terms=('RACK',)` の
        # 分岐で既にバイパスされるため無関係。分類ロジックは
        # `ref_designator_patterns`（ezdxf/region_detector.py に依存しない
        # 純粋な文字列判定モジュール、2026-07-26 分割）から使う——
        # `ref_designator.py`（DXF抽出パイプライン）は region_detector.py に
        # 依存するため、そちらを import すると循環依存になる。
        status, cat = classify_judgment_detailed(up, up)
        if status == 'excluded' and cat != 'unit_names':
            return False
        if status == 'candidate' and matched_confirmed_category(up) is not None:
            return False
    return True


def _horizontal_edges_at_extreme(polygon, side, level_tol=2.0):
    """ポリゴンの下端(side='bottom')または上端(side='top')にある横エッジ群
    [(x0,x1,y), ...] を返す（`_vertical_edges_at_extreme` の横版）。"""
    ys = [p[1] for p in polygon]
    target_y = min(ys) if side == 'bottom' else max(ys)
    segs = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if abs(y1 - y2) < 0.5 and abs(y1 - target_y) <= level_tol:
            segs.append((min(x1, x2), max(x1, x2), y1))
    return segs


def _bottom_edges(polygon, level_tol=2.0):
    """ポリゴンの下端（最小y）にある横エッジ群 [(x0,x1,y), ...] を返す。"""
    return _horizontal_edges_at_extreme(polygon, 'bottom', level_tol)


def _top_edges(polygon, level_tol=2.0):
    """ポリゴンの上端（最大y）にある横エッジ群 [(x0,x1,y), ...] を返す（`_bottom_edges`の上端版）。"""
    return _horizontal_edges_at_extreme(polygon, 'top', level_tol)


def _notch_bottom_edges(polygon, level_tol=2.0, probe=0.5):
    """最下端レベル以外にある下向き横エッジ群 [(x0,x1,y), ...] を返す。

    「下向き」＝エッジ中点の probe 直上が領域内・probe 直下が領域外。
    長方形では常に空（下向きエッジは最下端のみ）で、L字型等の非矩形ポリゴンの
    切り欠き部の横エッジだけが該当する。実例: EE6491-039-04A.dxf の
    SYSTEM I/F BOX。FLAT CABLE 部と一体のL字型領域で、名称ラベルが切り欠き部の
    下向きエッジ（最下端ではない）の直上にあるため、最下端エッジ（Tier1）と
    上端エッジ（Tier2）だけを見る従来の探索では候補から漏れていた。
    `region_name_candidates` が Tier2 スキャンにこのエッジ群を加えて使う。
    """
    min_y = min(p[1] for p in polygon)
    segs = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if abs(y1 - y2) >= 0.5:
            continue
        my = (y1 + y2) / 2.0
        if abs(my - min_y) <= level_tol:
            continue  # 最下端レベルは Tier1（_bottom_edges）の担当
        x0, x1s = min(x1, x2), max(x1, x2)
        if x1s - x0 < probe:  # 極小エッジは内外判定が不安定なため除外
            continue
        mx = (x0 + x1s) / 2.0
        if (_point_in_polygon((mx, my + probe), polygon)
                and not _point_in_polygon((mx, my - probe), polygon)):
            segs.append((x0, x1s, my))
    return segs


def _vertical_edges_at_extreme(polygon, side, level_tol=2.0):
    """ポリゴンの左端(side='left')または右端(side='right')にある縦エッジ群
    [(y0,y1,x), ...] を返す（図面全体が90°回転している場合の下端/上端の代替）。"""
    xs = [p[0] for p in polygon]
    target_x = min(xs) if side == 'left' else max(xs)
    segs = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if abs(x1 - x2) < 0.5 and abs(x1 - target_x) <= level_tol:
            segs.append((min(y1, y2), max(y1, y2), x1))
    return segs


def _dist_to_edge_segments(pt, segs, axis='y'):
    """点からエッジ群までの最短距離。`axis='y'`（既定）は横エッジ群
    `[(x0,x1,y), ...]` への距離（`_dist_to_bottom_edge` 相当）、`axis='x'` は
    縦エッジ群 `[(y0,y1,x), ...]` への距離（`_dist_to_vertical_edge` 相当）。
    縦版は点・エッジとも (主軸, 固定軸) の順に座標を入れ替えて同じ計算式を流用する。
    """
    a, b = pt if axis == 'y' else pt[::-1]
    best = float('inf')
    for (a0, a1, eb) in segs:
        if a0 <= a <= a1:
            d = abs(b - eb)
        else:
            d = math.hypot(a - (a0 if a < a0 else a1), b - eb)
        best = min(best, d)
    return best


def _dist_to_bottom_edge(pt, bottom_segs):
    """点から下端横エッジ群までの最短距離。"""
    return _dist_to_edge_segments(pt, bottom_segs, axis='y')


def _dist_to_vertical_edge(pt, vertical_segs):
    """点から縦エッジ群までの最短距離（_dist_to_bottom_edge の縦版）。"""
    return _dist_to_edge_segments(pt, vertical_segs, axis='x')


def _dist_and_center_to_edge_segments(pt, segs, axis='y'):
    """点から最も近いエッジ分節までの垂直距離と、その分節の中点までの沿軸距離
    （「中央らしさ」）を (distance, center_offset) で返す（`_dist_to_edge_segments`
    の拡張版。region_name_candidates() の候補順位付け専用。2026-07-26 追加）。

    最も近い分節の決定基準は `_dist_to_edge_segments` と同じ（垂直距離が最小の
    分節）。center_offset はその分節の中点から点までの沿軸方向の距離で、
    小さいほど分節（＝辺）の中央寄りに位置することを意味する。
    """
    a, b = pt if axis == 'y' else pt[::-1]
    best_d = float('inf')
    best_center = 0.0
    for (a0, a1, eb) in segs:
        if a0 <= a <= a1:
            d = abs(b - eb)
        else:
            d = math.hypot(a - (a0 if a < a0 else a1), b - eb)
        if d < best_d:
            best_d = d
            best_center = abs(a - (a0 + a1) / 2.0)
    return best_d, best_center


def _dist_and_center_to_bottom_edge(pt, bottom_segs):
    """`_dist_and_center_to_edge_segments` の下端横エッジ版。"""
    return _dist_and_center_to_edge_segments(pt, bottom_segs, axis='y')


def _dist_and_center_to_vertical_edge(pt, vertical_segs):
    """`_dist_and_center_to_edge_segments` の縦エッジ版。"""
    return _dist_and_center_to_edge_segments(pt, vertical_segs, axis='x')


def region_name_candidates(
    polygon: list[tuple[float, float]],
    labels: list[tuple[str, float, float]],
    max_dist: float = 10.0,
    min_dist: float = 1.0,
    min_letters: int = 3,
    limit: int = 8,
    exclude_circuit_symbols: bool = True,
    exclude_terms: tuple = ('NOTE', '☆'),
    exclude_lowercase: bool = True,
    circuit_keep_terms: tuple = ('RACK',),
    rotated_edge_roles: tuple | None = None,
) -> tuple[list[tuple[float, str]], dict[str, int]]:
    """領域名候補ラベルを優先順位（Tier）→距離順に返す（テキスト重複除去）。

    優先順位（ユーザー確認による仕様、2026-06-21 v1.5.9）:
      Tier 1: 矩形領域内にあり、下端横エッジの最近傍（`rotated_edge_roles` 指定時は
              その1番目の側の縦エッジ＝図面回転時の下端相当。実例で確認済み:
              `DE5434-553-10B.dxf` のような回転角+90°多数派の図面では右端）
      Tier 2: 矩形領域内にあり、上端横エッジの最近傍（`rotated_edge_roles` 指定時は
              2番目の側の縦エッジ＝上端相当。回転角+90°多数派の図面では左端）
      Tier 3: Tier 1/2 のいずれでも候補が見つからない場合のみ、ポリゴン全体の境界
              （任意の辺）への最短距離でフォールバック評価する（領域内外を問わない）。
    各 Tier 内は距離が近い順。Tier1/2 はいずれも `min_dist`未満（境界線分上＝
    部品符号等が偶然乗っただけの無関係なラベル）を除外する。同じテキストが複数
    Tier・複数距離で見つかった場合は、最も優先度の高い Tier・距離のものを残す。
    `rotated_edge_roles=None`（通常図面、または回転方向が判定できない図面）の
    場合、Tier1=下端横エッジ、Tier2=上端横エッジを使う。

    Tier1/2 を**領域内側のラベルに限定する**理由（2026-06-21 追加）: 領域名は
    通常その箱の内側に書かれるため、Tier1/2 が想定する「自分の箱の名前」は内側の
    ラベルである。領域の外側にある別の箱・別の注記等のラベルが、たまたま
    Tier1/2 のエッジ（下端/上端、回転時は右端/左端）に近いという理由だけで
    内側の正しいラベルより優先されてしまう不具合があった（`DE5434-553-10B.dxf`
    の回転領域で、領域外の `EFEM UPPER`〈距離3.9〉が領域内の正しい名称
    `CONTROL BOX CORE FX`〈距離5.2〉より優先されていた。DXF-viewer の Search
    Boundary を「最上位候補のみで照合」するよう変更した際にユーザーが発見）。
    Tier3 のフォールバックは領域内外を問わない（Tier1/2 で候補が無い場合の
    最後の手段のため、範囲を絞らない）。
    条件:
      - 英字 min_letters 字以上
      - exclude_terms のいずれかを含むラベル（例 NOTE, ☆）は除外
      - exclude_lowercase=True のとき英小文字を含むラベルは除外（領域名は大文字）
      - exclude_circuit_symbols=True のとき機器符号（候補）パターン一致は除外

    戻り値: (candidates, tier_by_text) のタプル。
      candidates: [(distance, text), ...]（従来通り。distance は採用された
                  Tier での距離）
      tier_by_text: {text: tier(1|2|3), ...}（`candidates` と同じ重複除去後の
                    エントリに対応。呼び出し側が「この候補がどれだけ確信度が
                    高いか」を判定するために使う）

    並び順（2026-07-26 変更、ユーザー確定）: 「境界線からの距離→辺の中央からの
    距離→Tier（下辺>上辺）」の順で全候補を横断的にソートする。従来は
    「Tier→Tier内で距離」の順（Tier1が常にTier2より優先）だったが、
    `DE5027-558-01B.dxf` 図面3/領域5 で「Tier2（上辺）候補 SY12 RX〈距離4.5、
    辺の中央からのオフセット2.5＝ほぼ中央〉が、Tier1（下辺）候補
    IOG B7〈距離7.3、オフセット26.5＝辺の端で中央から大きく外れる〉より
    直感的に正しい名称であるにもかかわらず、Tier優先のため後方に表示される」
    という実例が見つかり、Tier絶対優先の設計を距離優先に改めた。距離の比較は
    表示用に丸めた値（`round(d, 1)`、UIでの表示丸め`{d:.0f}`とは別に内部的に
    保持している精度）で行う。生の浮動小数点値で比較すると実質的にほぼ確実に
    詳細が異なり「辺の中央」がタイブレークとして機能しないため（表示上「同じ
    距離」に見える候補同士でのみ中央寄り・Tierが意味を持つ）。中央からの
    オフセットは、各候補が実際に最も近いと判定されたエッジ分節（Tier1/2は
    `_dist_and_center_to_bottom_edge`/`_dist_and_center_to_vertical_edge`、
    Tier3はエッジという概念が無いため常に0＝タイブレークに寄与しない）の
    中点からの沿軸距離。
    """
    def _scan(edge_segs, dist_center_fn, require_inside):
        cand = []
        for (t, x, y) in labels:
            if not _is_valid_name_candidate(t, min_letters, exclude_lowercase,
                                            exclude_terms, exclude_circuit_symbols,
                                            circuit_keep_terms):
                continue
            if require_inside and not _point_in_polygon((x, y), polygon):
                continue
            d, c = dist_center_fn((x, y), edge_segs)
            if min_dist <= d <= max_dist:
                cand.append((d, c, t))
        return cand

    if rotated_edge_roles:
        tier1_side, tier2_side = rotated_edge_roles
        tier1_edges = _vertical_edges_at_extreme(polygon, tier1_side)
        tier2_edges = _vertical_edges_at_extreme(polygon, tier2_side)
        dist_center_fn = _dist_and_center_to_vertical_edge
    else:
        tier1_edges = _bottom_edges(polygon)
        # Tier2 は上端エッジに加え、L字型等の切り欠き部の下向きエッジも対象にする
        # （切り欠き直上の名称ラベルを拾う。詳細は _notch_bottom_edges docstring）。
        tier2_edges = _top_edges(polygon) + _notch_bottom_edges(polygon)
        dist_center_fn = _dist_and_center_to_bottom_edge

    tiered = []
    for tier, edges in ((1, tier1_edges), (2, tier2_edges)):
        if not edges:
            continue
        for d, c, t in _scan(edges, dist_center_fn, True):
            tiered.append((tier, d, c, t))

    tiered.sort(key=lambda c: (round(c[1], 1), c[2], c[0]))
    seen = set()
    out = []
    tier_by_text = {}
    for tier, d, _c, t in tiered:
        if t in seen:
            continue
        seen.add(t)
        out.append((round(d, 1), t))
        tier_by_text[t] = tier
        if len(out) >= limit:
            break
    return out, tier_by_text


# ============================================================
# 8. 図面回転判定（90°回転対応）
# ============================================================

def _label_rotation_angle(entity):
    """ラベルエンティティの実効回転角(度, 0-180で正規化前)を返す。
    MTEXT は rotation 属性ではなく text_direction ベクトルで回転が表現される
    ことがあるため、そちらを優先する。"""
    if entity.dxftype() == 'MTEXT':
        try:
            if entity.dxf.hasattr('text_direction'):
                td = entity.dxf.get('text_direction')
                return math.degrees(math.atan2(td[1], td[0]))
        except Exception:
            pass
    return getattr(entity.dxf, 'rotation', 0) or 0


def _is_globally_rotated(label_entities, threshold=0.5):
    """ラベル(TEXT/MTEXT)の過半数が90°(または270°)回転しているか判定する。

    図面全体が90°回転して描かれたファイルでは、部品が横線分（本来の縦線分に
    相当）を途切れさせるため、横線分ギャップ橋渡しが必要になる。しかし通常向き
    の図面で「単純に検出ゼロ件だったから」を条件に橋渡しを許可すると、無関係な
    隣接矩形を誤って結合する副作用の恐れがある。そこでラベルの回転状況から
    図面全体の回転を明示的に判定し、回転図面のときのみ橋渡しを許可する。
    通常図面ではラベル回転はほぼ0%（実データで0〜0.2%程度）、回転図面では
    大半（実データで60〜97%）が90°回転していることを確認済み。
    """
    total = 0
    rotated = 0
    for e in label_entities:
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        total += 1
        ang = _label_rotation_angle(e) % 180.0
        if 80.0 <= ang <= 100.0:
            rotated += 1
    if total == 0:
        return False
    return (rotated / total) >= threshold


def _rotated_edge_roles(label_entities, threshold=0.5):
    """図面全体が90°回転している場合、下端相当/上端相当がどちら側の縦エッジに
    対応するかを判定する。

    `_is_globally_rotated` は回転の有無（角度が90°付近かどうか、符号を区別しない
    `% 180`）しか見ないが、名称候補の優先順位（下端相当を優先1位、上端相当を
    優先2位とする）には回転方向の符号（+90° か -90° か）が必要。

    実例で確認済みの対応（`DE5434-553-10B.dxf`、回転角+90°が多数派）:
      下端相当 = 右端の縦エッジ、上端相当 = 左端の縦エッジ
    回転角-90°が多数派の場合は左右が反転する（下端相当=左端、上端相当=右端）。

    戻り値: (tier1_side, tier2_side) のタプル（'left'/'right'）。回転していない、
    または回転方向の多数派が判定できない場合は None。
    """
    total = 0
    near_plus90 = 0
    near_minus90 = 0
    for e in label_entities:
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        total += 1
        ang = _label_rotation_angle(e)
        ang = ((ang + 180.0) % 360.0) - 180.0  # (-180, 180] に正規化
        if 80.0 <= ang <= 100.0:
            near_plus90 += 1
        elif -100.0 <= ang <= -80.0:
            near_minus90 += 1
    if total == 0:
        return None
    if (near_plus90 / total) >= threshold:
        return ('right', 'left')
    if (near_minus90 / total) >= threshold:
        return ('left', 'right')
    return None


# ============================================================
# 9. タイトルブロック除外
# ============================================================

def _is_titleblock_region(polygon, labels):
    """領域内に図番パターンとタイトル系語が同居していれば図番枠とみなす。"""
    has_dn = False
    has_term = False
    terms = ('TITLE', 'REVISION', 'DWG', '流用元', '図番')
    for (t, x, y) in labels:
        if not _point_in_polygon((x, y), polygon):
            continue
        if not has_dn and extract_drawing_numbers(t):
            has_dn = True
        if not has_term:
            up = t.upper()
            if any(k in up or k in t for k in terms):
                has_term = True
        if has_dn and has_term:
            return True
    return False


# ============================================================
# 10. 領域検出実行（_run_region_detection）
# ============================================================

def _run_region_detection(lines, det_cfg, frames, frame_area, frame_labels,
                          connection_points, rotated_edge_roles):
    """lines から H/V 分類 → (図面枠ごとの候補面リスト, 図面枠ごとの行き止まり枝
    リスト) を返す。`analyze_dxf_regions` の3パス検出（LINEのみ→LWPOLYLINE追加→
    横ギャップ橋渡し）が、それぞれこの関数を1回呼んで結果を得る。"""
    RH, RV = _split_axis_aligned(lines, det_cfg['snap'])
    fc = []
    dangling_by_frame = []
    for fi, frame in enumerate(frames):
        cands_list = []
        det_regions, det_dangling = _detect_regions(
            RH, RV, frame, frame_area, det_cfg,
            connection_points, raw_lines=lines)
        for reg in det_regions:
            if det_cfg['exclude_titleblock'] and _is_titleblock_region(reg['polygon'], frame_labels):
                continue
            if det_cfg['exclude_connection_point_regions']:
                cp = _count_connection_points_on_boundary(
                    reg['polygon'], connection_points, det_cfg['connection_point_margin'])
                if cp >= det_cfg['connection_point_threshold']:
                    continue
            ncands, ntiers = region_name_candidates(
                reg['polygon'], frame_labels,
                max_dist=det_cfg['name_max_dist'], min_dist=det_cfg['name_min_dist'],
                min_letters=det_cfg['name_min_letters'],
                rotated_edge_roles=rotated_edge_roles,
                exclude_circuit_symbols=det_cfg['exclude_circuit_symbols'],
                exclude_terms=det_cfg['name_exclude_terms'],
                exclude_lowercase=det_cfg['name_exclude_lowercase'],
                circuit_keep_terms=det_cfg.get('circuit_symbol_keep_terms', ('RACK',)))
            cands_list.append({
                'polygon': reg['polygon'], 'area': reg['area'],
                'name_candidates': ncands,
                'default_name': ncands[0][1] if ncands else '',
                'default_name_tier': ntiers.get(ncands[0][1]) if ncands else None,
                'tier_by_text': ntiers,
            })
        fc.append(cands_list)
        dangling_by_frame.append([d for d in det_dangling if d['entities']])
    return fc, dangling_by_frame


def _apply_cross_frame_frequency_ranking(regions):
    """同一ファイル内の全フレームを横断して集計した出現頻度（同じ候補テキストが
    何**フレーム**の候補リストに登場するか）を最優先の並び替えキーとして、各
    領域の `name_candidates` を再ソートする（2026-07-26、ユーザー確定。集計
    単位を領域数からフレーム数に変更・2026-07-26追加修正）。

    「同じ名称が複数のページ（フレーム）にまたがって繰り返し現れるほど、
    その名称が本来の領域名である確信度が高い」という考え方に基づく。実例:
    `EE6892-039-05B.dxf` の図面1/領域0（L字型ポリゴンの切り欠きに、検出漏れの
    入れ子小領域の名称ラベルが紛れ込んだケース）で、局所的な距離順では
    「SENSOR I/F B.D-Ⅲ」〈距離1.2、この図面のどの領域にも他に登場しない
    孤立候補〉が「SYSTEM I/F BOX」〈距離3.6だが、図面2/3/4の大領域候補
    （面積74.0%/37.3%/47.7%）にも一貫して登場〉より先に表示・既定選択されて
    しまっていた。頻度優先の並び替えにより SYSTEM I/F BOX が正しく先頭になる。

    集計単位はフレーム数（同名候補を持つ**別フレーム**の個数。同一ファイル内の
    全フレームを対象、面積による重み付けはしない）。**領域数ではない**点に
    注意——当初は領域数で数えていたが、`DE5027-563-03A.dxf` 図面3で、同一
    フレーム内の複数の異なる小領域それぞれの近くに汎用的な機器符号ラベル
    「IOG B7」（3領域）が偶然登場し、正解の「SY15 FX (NX-SOD400)」〈距離4.5、
    2領域〉より領域数で上回ってしまい、距離が遠い（7.3）誤った候補が既定に
    なる不具合が発覚（2026-07-26 ユーザー報告）。両候補とも実際にはフレーム2
    にしか登場しておらず、「複数ページにまたがる」という頻度シグナルの前提
    自体が成立していなかった。フレーム数で数えることで、この2候補はどちらも
    頻度1で同点になり、頻度では決着せず元の距離順（SY15 FXが正しく優先）に
    フォールバックする。一方 `SYSTEM I/F BOX` は4つの別フレームにまたがるため
    引き続き頻度4で正しく救済される。

    `name_candidates` は呼び出し時点で distance→center→tier で安定ソート済み
    であることを前提に、頻度のみをキーにした安定ソートを重ねがけする（同頻度
    内では元の distance→center→tier順序を保つ）。
    `_remove_overlap_claimed_candidates()` より前に呼ぶこと（`_tier_by_text`
    を参照する。同関数はこれを pop するため）。
    """
    freq = defaultdict(set)
    for r in regions:
        frame = r.get('frame')
        for t in {t for _d, t in r.get('name_candidates', [])}:
            freq[t].add(frame)

    for r in regions:
        cands = r.get('name_candidates', [])
        if len(cands) <= 1:
            continue
        reordered = sorted(cands, key=lambda c: -len(freq[c[1]]))
        if reordered != cands:
            r['name_candidates'] = reordered
            r['default_name'] = reordered[0][1]
            r['default_name_tier'] = r.get('_tier_by_text', {}).get(reordered[0][1])


def _region_inside(inner, outer):
    """多角形 inner が outer に完全に含まれる（全頂点が内側または境界上、かつ面積が
    小さい）か。"""
    return (_polygon_area(inner) < _polygon_area(outer)
            and all(_point_in_polygon(p, outer) for p in inner))


def _remove_overlap_claimed_candidates(regions):
    """重なる領域同士で、同じ名称候補テキストをより近い側（小さい距離）の領域
    だけに残し、遠い側からは取り除く（`regions_overlap()` が True の領域間のみ）。

    `region_name_candidates()` は領域ごとに独立して評価するため、入れ子/重なる
    2領域（例 `EE6313-546-01E.dxf` の外側`B CHAMBER`・内側`BAKE HEATER UNIT RX`）
    では、内側領域の名称ラベルが外側領域の境界からも Tier1/2 の許容距離内に
    収まり、外側領域の候補リストにも内側領域の名称が残ることがある
    （v1.5.13 までの既知の非対称ケース。`test_nested_regions_each_get_own_confident_default_name`
    が文書化）。しかし重なる領域は定義上別の物理領域であり（`regions_overlap` を
    名称同期防止に使っている理由そのもの）、内側領域がその名称をはるかに高い
    確信度（小さい距離）で確定的に持っている以上、外側領域がそれを選択肢として
    提示するのは利用者を誤解させる（ユーザー報告: 領域2が確定済みの名称が
    領域1の候補にも出てくるのは矛盾）。同じテキストについて、重なる領域の中で
    最小距離を持つ領域だけに候補を残し、他の重なる領域からは除去する。
    `default_name`/`default_name_tier` も除去結果に応じて再計算する。
    距離が等しい（明確な優劣がない）場合はどちらからも除去しない。
    """
    n = len(regions)
    original = [r['name_candidates'] for r in regions]  # 比較は変更前のスナップショットで行う
    overlap_cache = {}

    def _overlap(i, j):
        key = (i, j) if i < j else (j, i)
        if key not in overlap_cache:
            overlap_cache[key] = regions_overlap(regions[i]['polygon'], regions[j]['polygon'])
        return overlap_cache[key]

    for i in range(n):
        tier_by_text = regions[i].pop('_tier_by_text', {})
        cands = original[i]
        if not cands:
            continue
        kept = []
        for d, t in cands:
            claimed_by_closer = False
            for j in range(n):
                if j == i or not _overlap(i, j):
                    continue
                if any(t2 == t and d2 < d for d2, t2 in original[j]):
                    claimed_by_closer = True
                    break
                # 距離が同じでも、相手 j が自分 i に完全に含まれる内側の領域なら
                # 内側の領域の名称とみなし、外側 i の候補から外す（2026-10-10
                # ユーザー指示。EE3294-039-05A.dxf の 53% 領域から内側 5.6% 領域の
                # `SENSOR I/F B.D-Ⅲ` を外す。切り欠き辺と内側領域の下辺が同じ線で
                # 距離が同値になっていた）。
                if any(t2 == t and d2 == d for d2, t2 in original[j]) \
                        and _region_inside(regions[j]['polygon'], regions[i]['polygon']):
                    claimed_by_closer = True
                    break
            if not claimed_by_closer:
                kept.append((d, t))
        if len(kept) != len(cands):
            regions[i]['name_candidates'] = kept
            regions[i]['default_name'] = kept[0][1] if kept else ''
            regions[i]['default_name_tier'] = (
                tier_by_text.get(kept[0][1]) if kept else None)


# ============================================================
# 10b. 補完面解消（_resolve_complement_faces）
# ============================================================

def _vertex_in_corner_set(vertex, corner_list, tol=1.0):
    """vertex が corner_list の中に許容誤差 tol 以内で一致する点があるか。"""
    vx, vy = vertex
    return any(abs(vx - px) < tol and abs(vy - py) < tol for px, py in corner_list)


def _detect_complement_pairs(regions, tol=1.0):
    """補完面ペア (large_idx, small_idx) のリストを返す。

    補完面とは: small の全コーナー頂点が large のコーナー頂点集合に含まれ、
    large が small より多くのコーナー頂点を持ち、かつ 2 領域が重なる（overlap）
    場合に、large は small の「補完面」と定義する。

    平面グラフ半面探索で境界を共有する2面を生成するとき、共有辺を挟む一方の面が
    「小さい正しい面」、他方が「外側に回り込んだ補完面」として現れる。補完面は
    小さい面より必ず多くの頂点を持ち（追加頂点がある）、小さい面の全頂点を包含する。

    戻り値: [(large_idx, small_idx), ...]
    """
    n = len(regions)
    corners = [r['corners'] for r in regions]
    results = []
    for i in range(n):          # large 候補
        for j in range(n):      # small 候補
            if i == j:
                continue
            ci, cj = corners[i], corners[j]
            if len(ci) <= len(cj):
                continue        # large は small より多くの頂点が必要
            if not all(_vertex_in_corner_set(v, ci, tol) for v in cj):
                continue        # small の全頂点が large の頂点集合に含まれる必要あり
            if not regions_overlap(regions[i]['polygon'], regions[j]['polygon']):
                continue
            results.append((i, j))
    return results


def _extract_complement_subpolygons(large_corners, small_corners, tol=1.0):
    """補完面 large の境界を辿り、small に含まれない追加頂点の連続区間を切り出して
    サブ領域ポリゴン（リスト of リスト[(x,y)]）を返す。

    サブ領域の形状: [attachment_start, extra_v1, ..., extra_vN]
    （attachment_start から extra 頂点列を辿り、attachment_start に直線で戻る閉多角形）
    """
    n = len(large_corners)

    def is_shared(v):
        return _vertex_in_corner_set(v, small_corners, tol)

    flags = [is_shared(v) for v in large_corners]
    subregions = []
    visited_starts = set()
    for i in range(n):
        if flags[i] and not flags[(i + 1) % n]:
            attachment_start = large_corners[i]
            start_idx = (i + 1) % n
            if start_idx in visited_starts:
                continue
            extra_seq = []
            k = start_idx
            while k < n + start_idx:
                cur = large_corners[k % n]
                if is_shared(cur):
                    break
                extra_seq.append(cur)
                k += 1
            if extra_seq:
                visited_starts.add(start_idx)
                subregions.append([attachment_start] + extra_seq)
    return subregions


def _resolve_complement_faces(regions, frame_area, next_id=None):
    """補完面を検出してサブ領域に分割し、補完面を除去した新リストを返す。

    `_remove_overlap_claimed_candidates` より前に呼ぶこと（'_tier_by_text' が
    まだ存在するうちに処理する必要があるため）。

    処理の流れ:
      1. _detect_complement_pairs で大（補完面）・小（基準面）ペアを検出
      2. 補完面の頂点から基準面の頂点を除いた「追加頂点列」でサブ領域ポリゴンを生成
      3. 補完面の名称候補から基準面にクレームされた名称を除き、サブ領域に継承
      4. サブ領域を regions に追加し、補完面を除去して返す
    """
    pairs = _detect_complement_pairs(regions)
    if not pairs:
        return regions

    if next_id is None:
        next_id = max((r['id'] for r in regions), default=-1) + 1

    # 同じ large_i に複数の small_i パートナーが見つかる場合がある——大きい方の
    # 兄弟（B CHAMBER等）との補完ペアに加え、area_ratio 引き下げにより小さい方の
    # 兄弟（FX CHAMBER相当の形状）自体も独立採用され、同じ large_i の補完
    # パートナーとして二重にマッチしてしまうケース（`EE6313-545-01D.dxf`
    # area_ratio=5%で確認: 78.23%の合体面が、63.64%のB CHAMBERとの補完ペア
    # に加えて、14.59%の独立採用済み小面ともマッチし、両方が処理されて
    # 「B CHAMBER」の重複と、Tier評価の異なる誤命名〔VACUUM ROBOT FX〕の
    # 二重領域を生んでいた。2026-07-14 発見・修正）。各 large_i につき面積最大の
    # small_i のみを基準面として差分抽出に使い、それ以外の small_i は
    # 「差分抽出で正しい名称のサブ領域として再生成される側の重複」とみなして
    # 除去する（独立採用時の名称評価〔Tier制〕より、補完面自身の候補順の方が
    # 正しい名称を選べるため——後者を優先する）。
    best_small_for_large = {}
    for large_i, small_i in pairs:
        cur = best_small_for_large.get(large_i)
        if cur is None or regions[small_i]['area'] > regions[cur]['area']:
            best_small_for_large[large_i] = small_i
    redundant_small = {small_i for large_i, small_i in pairs
                       if small_i != best_small_for_large[large_i]}
    pairs = [(large_i, small_i) for large_i, small_i in pairs
             if small_i == best_small_for_large[large_i]]

    to_remove = {large_i for large_i, _ in pairs} | redundant_small
    new_regions = [r for i, r in enumerate(regions) if i not in to_remove]

    for large_i, small_i in pairs:
        comp_face = regions[large_i]
        base_face = regions[small_i]

        claimed = {t for _, t in base_face.get('name_candidates', [])}
        comp_tier = comp_face.get('_tier_by_text', {})

        inherited_cands = [(d, t) for d, t in comp_face.get('name_candidates', [])
                           if t not in claimed]
        inherited_tier = {t: comp_tier[t] for _, t in inherited_cands if t in comp_tier}

        default_name = inherited_cands[0][1] if inherited_cands else ''
        default_name_tier = inherited_tier.get(default_name) if default_name else None

        sub_polys = _extract_complement_subpolygons(comp_face['corners'], base_face['corners'])
        for sub_poly in sub_polys:
            sub_area = _polygon_area(sub_poly)
            new_regions.append({
                'id': next_id,
                'frame': comp_face.get('frame', 0),
                'polygon': sub_poly,
                'corners': _polygon_corners(sub_poly),
                'area': sub_area,
                'area_pct': 100.0 * sub_area / frame_area if frame_area > 0 else 0.0,
                'name_candidates': list(inherited_cands),
                'default_name': default_name,
                'default_name_tier': default_name_tier,
                'dangling_edges': [],
                '_tier_by_text': dict(inherited_tier),
            })
            next_id += 1

    return new_regions


def _detect_union_parents(regions, tol=1.0, area_tol=1.0):
    """結合親領域（union parent）の {親インデックス: (子インデックス, ...)} を返す。

    横線分または縦線分で複数分割された兄弟矩形の「合体親」が補完面として誤検出
    されるケース（例: L CHAMBER / FX CHAMBER を横線分で分割した図面で、親矩形が
    別の領域として残る）に対応する。_resolve_complement_faces は頂点数の差
    （large > small）を前提とするため、全領域が 4 頂点の等頂点数ケースは検出
    できない。子の数は2以上の任意数に対応する（v1.9.1、N子一般化。3分割以上の
    矩形、例 FL1F①②③、を正しく解消するために必要——2子限定の実装では、3つ以上の
    兄弟に分割された矩形の合体親を検出できず、外周の合体親が誤って独立領域の
    まま残ってしまう）。

    子候補の絞り込み: 親Pに内包される（regions_overlapかつ面積がPより小さい）
    候補を面積の大きい順に走査し、既に選んだ候補と重ならないものだけを貪欲に
    採用する。面積降順で走査するため、入れ子（例: 兄弟候補の内部にたまたま
    存在する無関係な小さな詳細領域）があっても、外側の正しい兄弟候補が先に
    選ばれ、内側のノイズ候補は「既選択候補と重なる」として自然に除外される
    （「内包候補の中から、他の内包候補に内包されないものだけを子とする」という
    単純な絞り込みだと、正しい兄弟候補がたまたま無関係な小さな入れ子領域を
    内部に持つだけで誤って子から除外されてしまう。`DE5434-563-03A.dxf` の
    `SB-1A(FX1)`〔7.70%〕が内部に無関係な小領域〔4.94%〕を持つケースで発覚。
    2026-07-15）。

    検出条件（全て満たす）:
      1. area(P) ≈ Σ area(子)  ← P が子群の合体サイズ
      2. P の全コーナーが 子群.corners の和集合に含まれる
      3. 各子について regions_overlap(P, 子)  ← P が子を内包
      4. 子同士は互いに重ならない（兄弟、貪欲選択により自動的に満たす）

    戻り値: {parent_idx: (child_idx, ...), ...}
    """
    n = len(regions)
    corners = [r['corners'] for r in regions]
    areas = [r['area'] for r in regions]
    polys = [r['polygon'] for r in regions]
    result = {}

    for i in range(n):
        contained = [j for j in range(n) if j != i
                     and areas[j] < areas[i] - area_tol
                     and regions_overlap(polys[i], polys[j])]
        if len(contained) < 2:
            continue
        contained.sort(key=lambda j: areas[j], reverse=True)
        selected = []
        for j in contained:
            if not any(regions_overlap(polys[j], polys[s]) for s in selected):
                selected.append(j)
        if len(selected) < 2:
            continue
        # 条件 1: 面積一致
        if abs(areas[i] - sum(areas[j] for j in selected)) > area_tol:
            continue
        # 条件 2: P の全頂点が子群の頂点集合の和に含まれる
        child_corner_pool = [c for j in selected for c in corners[j]]
        if not all(_vertex_in_corner_set(v, child_corner_pool, tol) for v in corners[i]):
            continue
        result[i] = tuple(selected)

    return result


def _force_include_union_children(cands_list, frame_area, area_ratio, area_tol=1.0, corner_tol=1.0):
    """面積フィルタ未適用の生候補リスト `cands_list` に対して合体親（union parent）を
    検出し、子側候補のインデックス集合を返す（呼び出し側が面積閾値を問わず採用する
    ために使う）。

    **合体親自身が単独面積閾値を満たす場合のみ**子をバイパス採用する
    （`frame_area`・`area_ratio` によるゲート）。ゲートが無いと、単独では
    閾値未満の異名兄弟2つの合体面がたまたま結合されただけの、実体の薄い
    小さな親（例 1.4%）でも子だけが無条件採用されてしまう（`EE6888-650-01C.dxf`
    の`（ＣＬＯＳＥ）`0.81%/`ＣＯＭＭＯＮ ＢＯＸ`0.58%が、親`ＣＡＴＨＯＤＥ
    ＭＡＩＮＴＥＮＡＮＣＥ ＳＷ`1.39%からバイパス採用されていた不具合。
    ユーザー報告により追加）。`DE5434-563-03A.dxf`の合体親（15.3%）のように
    親自身が閾値を満たすケースは従来どおり機能する。

    通常の採用判定（単独面積>=20%、または同名2ピース合算>=10%）は、兄弟矩形が
    互いに異なる名称を持ち、かつどちらも単独で閾値未満の場合（例:
    `DE5434-563-03A.dxf` の `SB-1A(FX1)`/`CN I/F B.D TYPE3(CN-IF3-1A)`、各7.6-7.7%）
    に対応できない。この2矩形を包む合体面（外側の閉領域。縦ギャップ橋渡しが両側の
    仕切り線を橋渡しした結果生じる、実体のない面積34292.5の見かけ上の領域）が
    偶然どちらか一方の兄弟と同名候補を共有すると、その兄弟だけが同名2ピース合算で
    救済され、もう一方の兄弟は候補にすら残らず消えていた。

    `_detect_union_parents` は面積一致・頂点包含・非重複という強い幾何学的根拠で
    合体関係を判定するため、採用フィルタより前（全ての生候補が揃った時点）で
    適用すれば、面積閾値に関わらず両方の子を正しく拾える。採用後は既存の
    `_resolve_union_parents` が同じ合体親をあらためて検出し、子の名称を除いた
    固有名が無ければ合体親自体を除去する（従来通り）。

    **補完面ペアと競合する三つ組は除外する**: `_detect_complement_pairs`
    （頂点数が非対称な大小2面のペア。`_resolve_complement_faces` が別途処理する）
    に関与する候補が三つ組（親・子2つ）のいずれかに含まれる場合は force-include
    の対象から外す。1つの面が2つの独立した仕組み（頂点差分による補完面カット出し／
    面積一致による合体親分解）に同時に「取り合われる」と、同じ物理領域が異なる
    形（カット出し後のサブポリゴン／生の面そのもの）で二重に採用されてしまう
    （実例: `EE6313-545-01D.dxf` の B CHAMBER/FX CHAMBER。補完面〔78.2%〕は
    B CHAMBER〔63.64%、単独で閾値超のため本来 force-include 不要〕+ 小面
    〔14.59%〕の合体としても幾何学的に成立してしまい、後者が
    `_resolve_complement_faces` によるカット出し結果と別に、生の面のまま
    二重採用され `B CHAMBER` が2件検出されるようになっていた。B CHAMBER は
    既にこの補完面ペアの「小」側であるため、複合面パターンとして除外される）。
    """
    if len(cands_list) < 3:
        return set()
    enriched = [dict(cf, corners=_polygon_corners(cf['polygon'])) for cf in cands_list]
    complement_involved = set()
    for large_i, small_i in _detect_complement_pairs(enriched):
        complement_involved.add(large_i)
        complement_involved.add(small_i)
    parent_to_children = _detect_union_parents(enriched, tol=corner_tol, area_tol=area_tol)
    child_idx = set()
    for i, children in parent_to_children.items():
        if i in complement_involved or any(c in complement_involved for c in children):
            continue
        if not _area_ratio_met(enriched[i]['area'], frame_area, area_ratio):
            continue
        child_idx.update(children)
    return child_idx


def _name_union_parent(parent_region, child_regions, labels, cfg,
                        exclude_names=None):
    """合体親領域の名称候補を、子領域が未採用のラベルから探索して返す。

    通常の `region_name_candidates` と異なる点:
      - require_inside を緩和し、領域の外側（底辺の下方向）も探索対象にする
        （合体親の名称ラベルが底辺のすぐ外側に置かれることがある。
         例: DE5434-563-03A.dxf の 'FX CHAMBER' @ y=76.4 は polygon 外）
      - 子領域がすでに採用した候補テキストを除外する
      - exclude_names に含まれるテキストを除外する（他の非子領域が使用中の名称）
      - 底辺中央 x 座標への近接度（中心距離）を距離の第2ソートキーにし、
        同距離の候補が複数あるとき中央により近いラベルを優先する

    戻り値: (name_candidates, tier_by_text) のタプル。
      name_candidates: [(distance, text), ...]
      tier_by_text: {text: 1}  （底辺探索由来 → Tier1 相当とみなす）
    """
    polygon = parent_region['polygon']
    max_dist = cfg.get('name_max_dist', 10.0)
    min_dist = cfg.get('name_min_dist', 1.0)
    min_letters = cfg.get('name_min_letters', 3)
    exclude_terms = cfg.get('name_exclude_terms', ('NOTE', '☆'))
    exclude_lowercase = cfg.get('name_exclude_lowercase', True)
    exclude_circuit_symbols = cfg.get('exclude_circuit_symbols', True)
    circuit_keep_terms = cfg.get('circuit_symbol_keep_terms', ('RACK',))

    # 子領域が採用済みのテキスト + 他の非子領域が使用中の名称 を除外対象として収集
    claimed = set(exclude_names or ())
    for child in child_regions:
        for _, t in child.get('name_candidates', []):
            claimed.add(t)

    # 底辺エッジとその x 全体中央を算出
    bottom = _bottom_edges(polygon)
    if not bottom:
        return [], {}
    all_x0 = min(seg[0] for seg in bottom)
    all_x1 = max(seg[1] for seg in bottom)
    center_x = (all_x0 + all_x1) / 2.0

    # 底辺エッジへの距離（領域外も許容）と中央距離でスコアリング
    scored = []
    for (t, x, y) in labels:
        if not _is_valid_name_candidate(t, min_letters, exclude_lowercase,
                                        exclude_terms, exclude_circuit_symbols,
                                        circuit_keep_terms) or t in claimed:
            continue
        dist = _dist_to_bottom_edge((x, y), bottom)
        if dist < min_dist or dist > max_dist:
            continue
        centrality = abs(x - center_x)
        scored.append((dist, centrality, t))

    scored.sort(key=lambda c: (c[0], c[1]))
    seen = set()
    out = []
    tier_by_text = {}
    for dist, _centrality, t in scored:
        if t in seen:
            continue
        seen.add(t)
        out.append((round(dist, 1), t))
        tier_by_text[t] = 1  # 底辺探索由来 → Tier1 相当
        if len(out) >= 8:
            break
    return out, tier_by_text


def _resolve_union_parents(regions, labels=None, cfg=None):
    """結合親領域（N兄弟矩形の合体、N>=2）を検出し、名称を再探索した上でリストを返す。

    子領域が採用済みの名称候補を除外し、底辺中央近接条件を加味して
    合体親固有の名称ラベルを探索する（`_name_union_parent` 参照）。
    未採用ラベルが見つかった場合は親を残して名称を更新する。
    見つからなかった場合は従来通り除去する。
    `labels` が与えられない場合は全ての結合親を除去する（後方互換）。

    `_resolve_complement_faces` の呼び出し後（補完面除去済みの状態）に呼ぶ。
    """
    parent_to_children = _detect_union_parents(regions)
    if not parent_to_children:
        return regions

    to_remove = set()
    if labels is not None:
        effective_cfg = cfg or {}
        parent_indices = set(parent_to_children.keys())
        child_indices = {c for cs in parent_to_children.values() for c in cs}
        # フレーム別に「既使用名称」を管理（異なるフレームは独立して同名を許可）
        parent_claimed_by_frame = defaultdict(set)
        for parent_idx, child_idxs in parent_to_children.items():
            parent = regions[parent_idx]
            parent_frame = parent.get('frame', 0)
            children = [regions[c] for c in child_idxs]
            # 同一フレーム内の非親・非子領域が使用中の名称を除外対象とする
            same_frame_names = {
                regions[i]['default_name']
                for i in range(len(regions))
                if i not in parent_indices and i not in child_indices
                and regions[i].get('default_name')
                and regions[i].get('frame', 0) == parent_frame
            } | parent_claimed_by_frame[parent_frame]
            new_cands, new_tiers = _name_union_parent(
                parent, children, labels, effective_cfg,
                exclude_names=same_frame_names)
            if new_cands:
                # 未採用ラベルが見つかった → 親を残して名称を更新
                parent['name_candidates'] = new_cands
                parent['_tier_by_text'] = new_tiers
                parent['default_name'] = new_cands[0][1]
                parent['default_name_tier'] = new_tiers.get(new_cands[0][1])
                parent_claimed_by_frame[parent_frame].add(new_cands[0][1])
            else:
                # 未採用ラベルがない → 従来通り除去
                to_remove.add(parent_idx)
    else:
        to_remove = set(parent_to_children.keys())

    return [r for i, r in enumerate(regions) if i not in to_remove]


# ============================================================
# 11. トップレベル解析（公開API）
# ============================================================

def analyze_dxf_regions(dxf_file: str, config: dict | None = None) -> dict:
    """DXFファイルを解析し、図面枠・閉領域（名称候補つき）・図面枠内ラベルを返す。

    戻り値 dict:
      frames: [(xl,xr,y0,y1), ...]
      frame_area: float
      labels: [(text, x, y), ...]  （図面枠内のみ）
      regions: [{id, frame, polygon, area, area_pct, name_candidates, default_name,
                 default_name_tier, dangling_edges}]
        default_name_tier: default_name（name_candidates[0]）の優先順位（1/2/3、
          候補が無い場合は None）。`region_name_candidates()` の Tier（下端/上端
          最近傍=1/2、境界全体への距離フォールバック=3）と同義。`app.py` の
          他領域への選択伝播（同期）が、確信度の高い自前の候補（Tier1/2）を
          確信度の低い他領域の選択で上書きしないようにするために使う（v1.5.9）。
        dangling_edges（領域ごと）: [{attachment, entities: [{handle, start, end}, ...]}]
          この領域の境界探索から除外された行き止まり枝（次数1のノードに繋がり、
          どこにも閉じていない境界線分の連結成分）。半面探索がこの枝を折り返す
          ために生じる「同じ頂点が2回連続する」アーティファクトの原因になっていた
          箇所を、面探索前に除去している（v1.5.7）。`attachment` は枝が現存する
          境界グラフに取り付く座標（この領域のポリゴン境界上に乗る）。1本の枝が
          複数の生エンティティ（短い線分の連なり、または閉領域の境界として一部
          だけ使われている長い線の余剰部分）で構成される場合は `entities` に
          複数件入る。取り付け点がどの領域の境界にも乗らない枝（無関係な部品等）
          は報告されない。
      error: str | None
    """
    cfg = dict(DEFAULT_REGION_CONFIG)
    if config:
        cfg.update(config)
    result = {'frames': [], 'frame_area': 0.0, 'labels': [], 'regions': [], 'error': None}
    try:
        doc = ezdxf.readfile(dxf_file)
        msp = doc.modelspace()
        frame_lines, region_lines, region_lines_lp, label_entities, connection_points = \
            _collect_region_geometry(doc, cfg, check_layer=True)

        frames = detect_drawing_frames(frame_lines, cfg['snap'])

        # フォールバック（2026-09-23）: 表示中の図面枠が1件も見つからない場合に
        # 限り、レイヤーoff/frozenを無視して図面枠線のみを取り直す。唯一の
        # タイトルブロック（図面枠を含む）がoff/frozenレイヤーに置かれている
        # 図面（EE5322-455-02A.dxf/-18A.dxf）で、2026-09-16のレイヤー単位
        # invisible判定追加により領域検出自体が実施できなくなっていた回帰への
        # 対応。**region_lines・label_entities・connection_points は表示中
        # （上で取得済み）のまま変更しない**——非表示のタイトルブロック内の
        # 図形・文字を領域境界候補やラベルとして拾わないため
        # （common_utils.is_invisibleのdocstring、Tools/CLAUDE.md参照）。
        if not frames:
            fb_frame_lines, _, _, _, _ = _collect_region_geometry(doc, cfg, check_layer=False)
            fb_frames = detect_drawing_frames(fb_frame_lines, cfg['snap'])
            if fb_frames:
                frames = fb_frames

        result['frames'] = frames
        if not frames:
            # 領域検出（面積比等の計算・矩形領域の列挙）は図面枠が無いと処理を
            # 継続できないため、「何が実施できなかったか」を明確に伝える文言に
            # する（ユーザー指摘、2026-07-14）。
            result['error'] = ('図面枠（太さ %d の線で囲まれた枠）が見つからなかったため、'
                               '領域探索を実施することができませんでした。'
                               % cfg['frame_lineweight'])
            # ただし『ラベル抽出』自体は図面枠に依存しないため、
            # `ref_designator._collect_all_labels_fallback()` と同じ「枠制約
            # なし・重複除去のみ」の方針で `result['labels']` を埋める。
            # 従来はここで空のまま return していたため、`analysis['labels']`
            # のみを参照する呼び出し側（当時の「機器符号（候補）以外も抽出」
            # ON時の集計処理、v3.0.0で廃止済み）がこの図面のラベルを一切
            # 集計できず、複数図面入力時にその図面だけラベルが0件になる
            # 不具合があった（2026-07-23 ユーザー報告: `EE6892-455B.dxf` 等、
            # Model/Paper Space分離で図面枠が見つからない図面を含む入力で
            # 「領域を検出」使用時に発生）。
            seen = set()
            fallback_labels = []
            for it in label_entities:
                _, clean_text, (x, y) = extract_text_from_entity(it)
                if not clean_text:
                    continue
                key = (clean_text, round(x, 1), round(y, 1))
                if key in seen:
                    continue
                seen.add(key)
                fallback_labels.append((clean_text, x, y))
            result['labels'] = fallback_labels
            return result
        frame_area = (frames[0][1] - frames[0][0]) * (frames[0][3] - frames[0][2])
        result['frame_area'] = frame_area

        # 図面枠内ラベル（重複除去）
        seen = set()
        frame_labels = []
        for it in label_entities:
            _, clean_text, (x, y) = extract_text_from_entity(it)
            if not clean_text:
                continue
            in_frame = any(xl - 1 <= x <= xr + 1 and y0 - 1 <= y <= y1 + 1
                           for (xl, xr, y0, y1) in frames)
            if not in_frame:
                continue
            key = (clean_text, round(x, 1), round(y, 1))
            if key in seen:
                continue
            seen.add(key)
            frame_labels.append((clean_text, x, y))
        result['labels'] = frame_labels

        area_ratio = cfg['area_ratio']                      # 単独領域の閾値（整数%比較）
        group_ratio = cfg.get('group_area_ratio', 0.10)      # 同名複数ピース合算の閾値（整数%比較）
        # エスカレーション（LWPOLYLINE追加・90°回転橋渡し・レベル汚染フォールバック）の
        # 発動判定は、単独採用閾値(area_ratio)とは独立の gate_ratio を用いる。
        # area_ratio の既定値が15%から5%に変更されたことで、単独採用には満たない
        # 小さな候補（従来なら「ゼロ件」扱いでエスカレーションが発動していた）が
        # 5%以上というだけでヒット扱いになり、エスカレーションが発動しなくなる
        # 副作用があった（`DE5434-553-10B.dxf` の90°回転枠で、LA CHAMBER等の
        # 小候補が5%を満たすため回転橋渡しパスが走らず、本来27.89%まで正しく
        # 検出されるはずの `CN I/F B.D TYPE3 (CN-IF3-1A)` が0.94%の断片のまま
        # 埋もれて消えていた不具合。2026-07-14 発見）。gate_ratio は
        # area_ratio と 0.15（エスカレーション機構が実データで検証された時点の
        # 既定値）の大きい方とし、area_ratio を15%以上に設定した場合はその値に
        # 追従する。
        gate_ratio = max(area_ratio, 0.15)
        rotated = _is_globally_rotated(label_entities)
        rotated_edge_roles = _rotated_edge_roles(label_entities) if rotated else None

        # 1) LINE のみで領域検出を試みる
        lines_for_detection = region_lines
        frame_cands, dangling_by_frame = _run_region_detection(
            lines_for_detection, cfg, frames, frame_area, frame_labels,
            connection_points, rotated_edge_roles)

        def _zero_hit_frame_indices(cands, ratio):
            return [fi for fi, cl in enumerate(cands)
                    if not any(_area_ratio_met(cf['area'], frame_area, ratio) for cf in cl)]

        def _bbox_key(cf):
            xs = [p[0] for p in cf['polygon']]
            ys = [p[1] for p in cf['polygon']]
            return (round(min(xs)), round(max(xs)), round(min(ys)), round(max(ys)))

        def _merge_cands_lists(base_list, extra_list):
            """base_list を優先しつつ、extra_list にのみ存在する候補(bbox基準)を
            追加する（置き換えではなく合算）。base_list に無いbboxの候補だけを
            追加することで、base_list（LINE-onlyの正しい検出）がextra_list
            （LWPOLYLINE追加等、corner-partner判定を乱しうるパス）に完全に
            上書きされて消える事故を防ぐ（`EE6888-639-01A.dxf` の
            `SYSTEM I/F BOX **`〔4.61%〕がLWPOLYLINE追加パスでは別の誤った
            候補に化けて消えていた不具合。2026-07-14）。"""
            seen = {_bbox_key(cf) for cf in base_list}
            merged = list(base_list)
            for cf in extra_list:
                k = _bbox_key(cf)
                if k not in seen:
                    seen.add(k)
                    merged.append(cf)
            return merged

        # LINE だけで閾値超え候補がゼロだった図面枠に限り、LWPOLYLINE を追加して
        # 再検出する（例: EE6888-631-01A.dxf など境界が LWPOLYLINE で描かれた図面
        # への対応）。判定・再検出とも図面枠単位（zero_fis のみ）で行う——ファイル
        # 全体で1件でも候補があれば以降のパスを丸ごとスキップする実装だと、同じ
        # ファイル内の「別の図面枠がたまたま先に見つかる」だけで、本来
        # LWPOLYLINE/回転対応が必要な図面枠が永久に救済されないバグがあった
        # （`DE5434-553-10B.dxf` の `CONTROL BOX CORE FX`/`RX` で確認。area_ratio を
        # 10%→15% に変えるだけで検出有無が反転するという閾値依存の非単調な挙動が
        # 発覚の手がかりだった。2026-07-12 修正）。
        # LINE でも十分な候補がある図面枠には LWPOLYLINE を追加しない
        # （小部品の LWPOLYLINE が境界線の corner-partner 判定を誤らせるため）。
        # このゲートは gate_ratio（area_ratio と 0.15 の大きい方）で判定する
        # （回転橋渡しパスと同じ理由。`EE6868-405-10A.dxf` の `EFEM UPPER`
        # 〔LWPOLYLINE追加が必要・29.03%〕が、同じ枠内の他の小候補〔7.33%等〕が
        # area_ratio(5%)を満たすだけでゼロ扱いされなくなり、LWPOLYLINE追加パスが
        # 発動しなくなって検出漏れていた不具合。2026-07-14 発見）。
        # 採用は「置き換え」ではなく「合算」（`_merge_cands_lists`）で行う——
        # LWPOLYLINE追加は corner-partner 判定を乱し、既に LINE-only で正しく
        # 検出できていた候補を壊れた別の候補に化けさせて消してしまうことがある
        # （`EE6888-639-01A.dxf` の `SYSTEM I/F BOX **`〔4.61%〕で確認）。合算に
        # よって LINE-only の正しい検出を保持したまま、LWPOLYLINE追加でのみ
        # 見つかる新規候補（`EFEM UPPER`等）だけを追加できる。
        zero_fis = _zero_hit_frame_indices(frame_cands, gate_ratio)
        if zero_fis and region_lines_lp:
            lines_for_detection = region_lines + region_lines_lp
            sub_frames = [frames[fi] for fi in zero_fis]
            fc2, dg2 = _run_region_detection(
                lines_for_detection, cfg, sub_frames, frame_area, frame_labels,
                connection_points, rotated_edge_roles)
            for j, fi in enumerate(zero_fis):
                frame_cands[fi] = _merge_cands_lists(frame_cands[fi], fc2[j])
                dangling_by_frame[fi] = dg2[j]

        # それでも閾値超え候補がゼロだった図面枠に限り、かつラベルの過半数が90°回転
        # している（=図面全体が90°回転して描かれている）場合のみ、横線分のギャップ
        # 橋渡しを有効にして再検出する（安全条件＝縦線分の端点とのコーナー一致無し・
        # CIRCLE無し、は橋渡し縦線分と同じ）。判定・再検出とも図面枠単位（zero_fis の
        # み）で行う理由は上記 LWPOLYLINE パスと同じ（1ファイル内の一部の図面枠だけが
        # 回転コンテンツを持つケースを取りこぼさない）。
        # 回転判定（`rotated`）はファイル全体のラベル集計に基づく既存の判定のまま
        # 維持する——通常向きの図面枠で「単に検出ゼロ件だったから」をトリガーに横線分
        # も橋渡ししてしまうと、無関係な隣接矩形を誤って結合する副作用があるため
        # （`_is_globally_rotated` 参照）。
        # このゲートは gate_ratio（area_ratio と 0.15 の大きい方）で判定する。
        # このパスは既存候補を「置き換える」が、実際には既存候補を包含する
        # スーパーセットを返す（橋渡しを追加するだけで、既存の境界検出を壊さない）
        # ため、area_ratio が低く設定されていても、より大きな正しい領域を見逃さない
        # よう積極的に発動させる（`DE5434-553-10B.dxf` の90°回転枠で、面積の小さい
        # 候補〔LA CHAMBER 13.59%等〕が5%を満たすため回転橋渡しパスがスキップされ、
        # 本来27.89%まで正しく検出されるはずの `CN I/F B.D TYPE3 (CN-IF3-1A)` が
        # 0.94%の断片のまま埋もれて消えていた不具合。2026-07-14 発見・修正）。
        # 既知の限界（2026-07-26、ユーザー承認済み・修正見送り）: このゲート
        # （zero_fis のみ）と直前の LWPOLYLINE 追加ゲートは、いずれも独立に
        # 「ゼロヒットのフレームのみ」で発動する。あるフレームが1件でも
        # area_ratio を満たす候補（例 EFEM UPPER）を見つけた時点でどちらの
        # ゲートも素通りするため、「LWPOLYLINE 追加」と「横ギャップ橋渡し」の
        # "両方" が揃って初めて閉じる入れ子領域（`EE6868-516-01A.dxf` の
        # `EFEM UPPER` 内部、`#FEC`〈x:85.3-630.8〉・`#FEB`〈x:110.3-618.0〉を
        # 辺とする2つの入れ子矩形。上辺の一部が LWPOLYLINE 由来かつ複数分節に
        # 分かれている）は、フレームが既に1件ヒット済みのため両ゲートとも発動
        # せず、検出されないまま残る（`_detect_regions()` に両方の条件を直接
        # 渡せば正しく検出できることを確認済み）。「各図面枠は最低1件見つければ
        # 良い」という現行のエスカレーション設計の構造的な限界であり、解消には
        # ゲート条件自体の見直し（全フレームで常に両パスを試して結果を合算する
        # 等）が必要で影響範囲が広いため、既知の限界として受容する
        # （ユーザー判断。`test_region_extraction.py` の
        # `test_known_limitation_nested_regions_require_both_escalations`
        # 参照）。
        det_cfg = cfg
        zero_fis = _zero_hit_frame_indices(frame_cands, gate_ratio)
        if zero_fis and rotated:
            det_cfg = dict(cfg)
            det_cfg['bridge_horizontal_gaps'] = True
            sub_frames = [frames[fi] for fi in zero_fis]
            fc3, dg3 = _run_region_detection(
                lines_for_detection, det_cfg, sub_frames, frame_area, frame_labels,
                connection_points, rotated_edge_roles)
            for j, fi in enumerate(zero_fis):
                frame_cands[fi] = fc3[j]
                dangling_by_frame[fi] = dg3[j]

        # 4パス目（レベル汚染フォールバック・図面枠単位）: 領域境界線と同属性の無関係な
        # 近接線分（例: 境界線 y=122.00 の 0.37 上のコネクタ箱底辺 y=122.37）が共線結合の
        # レベルクラスタ平均を汚染して境界線をシフトさせ、縦線端点との接続が切れて閉領域が
        # 不成立になる枠がある（EE6892-039-05B.dxf の2枠目、SYSTEM I/F BOX）。
        # そのような「閾値超え候補ゼロの枠」に限り、スパン単位レベル（汚染なし）で再検出する。
        # 発動条件（明示的な信号。「ゼロ件だから試す」だけをトリガーにしない）:
        #   (a) 閾値超えゼロの枠がある
        #   (b) 他の枠には閾値超えの領域がある（=全枠ゼロの図面タイプ〔電源基板の回路図等〕
        #       では発動しない。EE6333-610-07A 等で基板外形を誤検出するのを防ぐ）
        # 採用条件: 回復した領域の名称が他枠で検出済みの名称と一致する枠のみ置き換える
        #   （1ファイル複数図面は同じユニット群の続きを描くため、同名領域が枠をまたいで
        #    現れることを回復の根拠とする）。
        zero_fis = [fi for fi, cl in enumerate(frame_cands)
                    if not any(_area_ratio_met(cf['area'], frame_area, gate_ratio) for cf in cl)]
        hit_names = {cf['default_name']
                     for fi, cl in enumerate(frame_cands) if fi not in zero_fis
                     for cf in cl
                     if _area_ratio_met(cf['area'], frame_area, gate_ratio) and cf['default_name']}
        if zero_fis and hit_names:
            cfg_span = dict(det_cfg)
            cfg_span['span_level_merge'] = True
            sub_frames = [frames[fi] for fi in zero_fis]
            fc2, dg2 = _run_region_detection(
                lines_for_detection, cfg_span, sub_frames, frame_area, frame_labels,
                connection_points, rotated_edge_roles)
            for j, fi in enumerate(zero_fis):
                if any(_area_ratio_met(cf['area'], frame_area, gate_ratio) and cf['default_name'] in hit_names
                       for cf in fc2[j]):
                    frame_cands[fi] = fc2[j]
                    dangling_by_frame[fi] = dg2[j]

        # 2) 第1図面（最左フレーム）で「同名複数ピース合算>=group_thr」となる名称を
        #    ターゲットとする（MPD RACK2 のように2矩形で合算が閾値超のケース）。
        #    他図面では、このターゲット名称の矩形を面積に関係なく抽出する。
        target_names = set()
        if frame_cands:
            by_name = defaultdict(list)
            for cf in frame_cands[0]:
                if cf['default_name']:
                    by_name[cf['default_name']].append(cf['area'])
            for nm, areas in by_name.items():
                if len(areas) >= 2 and _area_ratio_met(sum(areas), frame_area, group_ratio):
                    target_names.add(nm)

        # 3) 採用条件: 個別面積>=単独閾値(20%)、または 名称がターゲット（複数ピース合算で
        #    第1図面が閾値超）、または合体親（union parent）の子と確認された候補
        #    （`_force_include_union_children`、面積閾値を問わず採用。後述）、または
        #    名称候補が `name_filter_prefixes` のいずれかで前方一致する候補
        #    （面積閾値を問わず採用。v1.9.3、ユーザー要望: 抽出したい領域名が
        #    分かっている場合に面積比とは無関係に抽出できるようにする）。
        #    行き止まり枝は、その取り付け点(attachment)が当該領域のポリゴン境界上に
        #    乗るものだけを、その領域の `dangling_edges` として絞り込む（無関係な
        #    部品・他領域の枝を混在させない）。
        attach_tol = max(cfg.get('face_snap', 0.1), 0.5)
        name_filter_prefixes = [normalize_width(p) for p in cfg.get('name_filter_prefixes', ()) if p]
        regions = []
        rid = 0
        for fi, cands_list in enumerate(frame_cands):
            frame_dangling = dangling_by_frame[fi] if fi < len(dangling_by_frame) else []
            force_idx = _force_include_union_children(cands_list, frame_area, area_ratio)
            for cidx, cf in enumerate(cands_list):
                name_filter_hit = name_filter_prefixes and any(
                    normalize_width(t).startswith(p)
                    for _d, t in cf['name_candidates']
                    for p in name_filter_prefixes
                )
                if not (_area_ratio_met(cf['area'], frame_area, area_ratio)
                        or (cf['default_name'] and cf['default_name'] in target_names)
                        or cidx in force_idx
                        or name_filter_hit):
                    continue
                region_dangling = [
                    br for br in frame_dangling
                    if br['attachment'] is not None
                    and _dist_point_to_polygon(br['attachment'], cf['polygon']) <= attach_tol
                ]
                regions.append({
                    'id': rid,
                    'frame': fi,
                    'polygon': cf['polygon'],
                    'corners': _polygon_corners(cf['polygon']),
                    'area': cf['area'],
                    'area_pct': 100.0 * cf['area'] / frame_area,
                    'name_candidates': cf['name_candidates'],
                    'default_name': cf['default_name'],
                    'default_name_tier': cf.get('default_name_tier'),
                    'dangling_edges': region_dangling,
                    '_tier_by_text': cf.get('tier_by_text', {}),
                })
                rid += 1
        regions = _resolve_complement_faces(regions, frame_area, next_id=rid)
        regions = _resolve_union_parents(regions, labels=frame_labels, cfg=cfg)
        _apply_cross_frame_frequency_ranking(regions)
        _remove_overlap_claimed_candidates(regions)
        result['regions'] = regions

        del doc, msp
        gc.collect()
    except Exception as e:
        result['error'] = str(e)
        gc.collect()
    return result


def assign_region_labels(
    labels: list[tuple[str, float, float]],
    named_regions: list[dict],
) -> list[tuple[str, float, float, list[str]]]:
    """各ラベル(text,x,y)が内包される領域名のリストを返す。

    named_regions: [{'polygon': [...], 'name': str}]（名称確定済み）。
    戻り値: [(text, x, y, [region_name, ...]), ...]
    """
    out = []
    for (t, x, y) in labels:
        names = []
        for reg in named_regions:
            nm = reg.get('name')
            if nm and _point_in_polygon((x, y), reg['polygon']) and nm not in names:
                names.append(nm)
        out.append((t, x, y, names))
    return out


def collect_unique_region_candidate_names(analyses: dict) -> list[str]:
    """全ファイル・全領域の名称候補（単一候補領域・複数候補領域を問わず）から、
    NFKC正規化（半角統一）した上でユニークな名称一覧をABC順で返す。

    「領域一覧」チェックリスト（app.py）の選択肢構築に使う。1つの領域が
    複数の候補を持つ場合、その全候補（先頭の最有力候補だけでなく）を対象に
    含める——ユーザーがどの候補を採用するかをこの一覧から選べるようにするため。
    """
    names = set()
    for analysis in analyses.values():
        for reg in analysis.get('regions', []):
            for (_dist, text) in reg.get('name_candidates', []):
                names.add(normalize_width(text))
    return sorted(names)


def _matched_checked_candidates(reg: dict, checked_names: set) -> list:
    """領域の候補（`name_candidates`）のうち、「領域一覧」でチェック済みの
    名称と一致するものだけを、元の並び順（Tier→距離順）を保ったまま返す。

    「領域一覧」でチェックされなかった候補は「採用しない」という明示的な
    意思表示とみなすため（2026-07-30 ユーザー指定）、この関数は常に
    チェック済みの候補のみを対象にする——未チェックの候補を「まだ判断
    していないので個別確認に回す」形で残すことはしない。
    """
    return [
        (d, t) for (d, t) in reg.get('name_candidates', [])
        if normalize_width(t) in checked_names
    ]


def resolve_globally_decided_name(reg: dict, checked_names: set) -> str | None:
    """領域の候補のうち、「領域一覧」でチェック済みの名称がちょうど1件だけ
    一致する場合、その候補名を返す（自動決定）。

    一致が0件（どの候補も「領域一覧」でチェックされていない＝ユーザーは
    この領域にはどの名称も採用しないことを選んでいる）、または2件以上
    （どちらを採用するかまだ決められない）の場合は None を返す
    （2026-07-30、ユーザー指定により変更。旧仕様では2件以上一致時に
    最優先の候補を自動採用していたが、複数のチェック済み候補が同一領域に
    競合する場合は「領域の確認」での個別選択に委ねる方針に統一した）。
    """
    matched = _matched_checked_candidates(reg, checked_names)
    if len(matched) == 1:
        return matched[0][1]
    return None


def pending_candidates_for_region(reg: dict, checked_names: set) -> list:
    """「領域の確認」でこの領域に提示すべき候補（＝「領域一覧」でチェック済み
    のものだけ）を、元の並び順を保ったまま返す。`is_region_pending_selection()`
    が True を返す領域（チェック済み候補が2件以上）に対して使う。未チェックの
    候補は選択肢自体に含めない（2026-07-30 ユーザー指定）。
    """
    return _matched_checked_candidates(reg, checked_names)


def is_region_pending_selection(reg: dict, checked_names: set) -> bool:
    """「領域の確認」でファイルごとの個別選択が必要な領域かどうかを返す。

    領域の候補のうち「領域一覧」でチェック済みのものがちょうど2件以上ある
    場合のみ True（＝チェック済み候補同士でどちらを採用するか、まだ決め
    られないケース）。チェック済みが1件なら `resolve_globally_decided_name()`
    により自動決定され、0件なら「どの候補も採用しない」とみなしてここでも
    対象にしない（2026-07-30、ユーザー指定）。
    """
    return len(_matched_checked_candidates(reg, checked_names)) >= 2


def build_region_label_summary(region_results: dict) -> tuple[list[tuple[str, str]], list[dict]]:
    """機器符号抽出パイプライン（`ref_designator.build_ref_designator_region_results()`）
    が構築した region_results から、領域名ごとに全ファイル横断でラベルを
    集計した「領域別ラベル一覧」用の行データを構築する。

    領域名は同名であれば複数ファイルにまたがって1グループに合算される
    （矩形領域抽出は元々「同名複数ピース合算」「他図面でも同名採用」等、複数
    ファイルで同じ領域名を共有する前提の設計であるため、これは自然な拡張）。

    戻り値: (ファイル一覧 [(fname, 表示用図番), ...]（ファイル出現順。図番未抽出
    時はファイル名〔拡張子なし〕にフォールバック）, 行データのリスト)。行データは
    {'領域名': str, 'ラベル': str, '合計個数': int, 'per_file': {fname: 個数}}
    （`per_file` は表示用図番ではなく **fname をキー**にする——2ファイルが同じ図番
    〔未抽出時のフォールバックを含む〕を持つ場合の値衝突を避けるため）。
    領域名は昇順、各領域内のラベルも昇順に整列する。ファイルにそのラベルが
    存在しない場合の個数は 0（`per_file` に未登場のキーとして表現、呼び出し側で
    `.get(fname, 0)` する）。
    """
    files = []
    for fname, data in region_results.items():
        ident = data.get('drawing_number') or os.path.splitext(fname)[0]
        files.append((fname, ident))

    by_region = defaultdict(lambda: defaultdict(int))
    per_file_by_region = defaultdict(lambda: defaultdict(dict))
    for fname, data in region_results.items():
        for region_name, label_counts in data.get('region_label_counts', {}).items():
            for label, count in label_counts.items():
                by_region[region_name][label] += count
                per_file_by_region[region_name][label][fname] = count

    rows = []
    for region_name in sorted(by_region.keys()):
        for label in sorted(by_region[region_name].keys()):
            rows.append({
                '領域名': region_name,
                'ラベル': label,
                '合計個数': by_region[region_name][label],
                'per_file': per_file_by_region[region_name][label],
            })
    return files, rows
