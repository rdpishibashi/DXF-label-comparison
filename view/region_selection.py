"""領域選択UI（「領域を検出」→「領域一覧」→「領域選択を完了」→「領域の確認」）と
ファイル単位の抽出ループ（streamlit 依存のビュー層）。

複数プロジェクトで共有するモジュール（primary: DXF-extract-labels。
DXF-label-comparison にバイト一致のコピーがあり、同プロジェクトの
`tests/regression/spec/test_shared_files_identical_to_extract_labels.py`
が一致を検証する）。変更は primary で行い、コピーへそのまま伝播すること。
プロジェクト固有の事情（「領域一覧」に出す名称の範囲、キャプションの動詞、
結果クリア対象のセッションキー、ボタンの表示条件）は呼び出し側が引数で渡す。

セッションキー:
  - `region_analyses`: {ファイルキー: 解析結果}（`run_region_detection()` の戻り値を
    呼び出し側が格納する。ファイルキーは呼び出し側が決める一意な文字列）
  - `region_selection_confirmed`: 「領域選択を完了」済みか
  - `grc_<名称>`: 「領域一覧」の「特定」チェック状態
  - `gre_<名称>`: 「領域一覧」の「除外」チェック状態（v3.11.0新設。
    `allow_exclude=False` の呼び出し側〈DXF-label-comparison〉では列自体を
    表示しないため、常に空）
  - `region_list_editor`: 「領域一覧」表のウィジェット状態（key用ver・元データ・
    最後に書いたチェック状態）。チェック操作で表を作り直さないための固定用
  - `rc_<ファイルキー>_<領域id>_<i>` / `rc_<ファイルキー>_<領域id>_none`:
    「領域の確認」のチェック状態
"""
import contextlib
import os
import time

import pandas as pd
import streamlit as st

from model.common_utils import save_uploadedfile, normalize_width
from model.region_detector import (
    DEFAULT_REGION_CONFIG, regions_overlap,
    resolve_globally_decided_name, is_region_pending_selection,
    pending_candidates_for_region,
)
from model.extraction_pipeline import detect_file_regions, extract_file_data
from model.ref_designator import unnamed_region_labels

ANALYSES_KEY = 'region_analyses'
CONFIRMED_KEY = 'region_selection_confirmed'
SELECTION_KEY_PREFIXES = ('rc_', 'grc_', 'gre_')
# 「領域一覧」表の状態（grc_ 接頭辞の外に置く: global_checked_region_names() に混入させない）
EDITOR_STATE_KEY = 'region_list_editor'
CHECK_COLUMN_WIDTH = 64  # 「特定」「除外」列（漢字2文字＋ソートボタン）のpx幅
NAME_COLUMN_WIDTH = 360  # 「領域名」列のpx幅

# 図面枠が見つからない場合に `analyze_dxf_regions()` が返すエラーメッセージ
# （`region_detector.py` 側の文言）に含まれる識別文字列。このケースは既知の
# 未対応ケースとして「領域の確認」に表示しない
# （Excel出力・他セクションには影響しない）。
FRAME_NOT_FOUND_ERROR_MARKER = '領域探索を実施することができませんでした'


@contextlib.contextmanager
def temp_dxf_file(uploaded_file):
    """アップロードされたファイルを一時ファイルに保存し、処理後に削除する。"""
    tmp = save_uploadedfile(uploaded_file)
    try:
        yield tmp
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def build_region_cfg(region_detection_config):
    """`config.py` の `region_detection_config`（旧「領域検出の詳細設定」フォームの
    11設定）から `region_cfg` を組み立てる。

    組み立て順: `DEFAULT_REGION_CONFIG`（アルゴリズム内部の既定値一式）→
    `region_detection_config` の11設定で上書き → `region_detection_config.
    ADVANCED_OVERRIDES` でさらに上書き（`DEFAULT_REGION_CONFIG` のキーなら
    何でも指定可能）。既定値のみの場合、返り値は `DEFAULT_REGION_CONFIG` と
    完全に等価になる（DXF-extract-labels `tests/unit/test_config_region_cfg.py` で固定）。
    """
    cfg = dict(DEFAULT_REGION_CONFIG)
    rc = region_detection_config
    cfg.update({
        'frame_lineweight': rc.FRAME_LINEWEIGHT,
        'region_lineweight': rc.REGION_LINEWEIGHT,
        'connection_point_margin': rc.CONNECTION_POINT_MARGIN,
        'region_color': rc.REGION_COLOR,
        'name_filter_prefixes': tuple(rc.NAME_FILTER_PREFIXES),
        'name_exclude_terms': tuple(rc.NAME_EXCLUDE_TERMS),
        'area_ratio': rc.AREA_RATIO_PERCENT / 100.0,
        'group_area_ratio': rc.GROUP_AREA_RATIO_PERCENT / 100.0,
        'name_max_dist': float(rc.NAME_MAX_DIST),
        'name_min_dist': float(rc.NAME_MIN_DIST),
        'name_min_letters': rc.NAME_MIN_LETTERS,
    })
    cfg.update(rc.ADVANCED_OVERRIDES)
    return cfg


def run_region_detection(named_files, region_cfg, status_placeholder=None, start_time=None):
    """`named_files`（[(ファイルキー, UploadedFile), ...]）の各ファイルの領域を検出し、
    {ファイルキー: 解析結果} を返す。

    `status_placeholder`/`start_time` を渡すと、処理中のファイルキー・件数・
    経過秒数を表示する（DXF-diff-manager と同じ書式）。"""
    analyses = {}
    total = len(named_files)
    for idx, (key, uf) in enumerate(named_files, start=1):
        if status_placeholder is not None:
            elapsed = time.time() - start_time
            status_placeholder.text(
                f"領域を検出中: {key}（{idx}/{total}件、経過 {elapsed:.1f} 秒）")
        with temp_dxf_file(uf) as tmp:
            analyses[key] = detect_file_regions(tmp, uf.name, region_cfg)
    return analyses


def run_label_extraction(named_files, frame_lineweight, analyses=None,
                         status_placeholder=None, start_time=None):
    """`named_files`（[(ファイルキー, UploadedFile), ...]）の各ファイルのラベルを抽出し、
    {ファイルキー: `extract_file_data()` の戻り値} を返す。

    `analyses`（{ファイルキー: 解析結果}）を渡した場合は領域付きモードとして、
    `analyses` に含まれるファイルキーだけを `analyses` の順で処理する
    （図番等は解析結果から引き継ぐ）。渡さない場合は通常モードとして全ファイルを
    `named_files` の順で処理する。"""
    if analyses is not None:
        by_key = dict(named_files)
        targets = [(key, by_key[key]) for key in analyses]
    else:
        targets = list(named_files)
    results = {}
    total = len(targets)
    for idx, (key, uf) in enumerate(targets, start=1):
        if status_placeholder is not None:
            elapsed = time.time() - start_time
            status_placeholder.text(
                f"ラベルを抽出中: {key}（{idx}/{total}件、経過 {elapsed:.1f} 秒）")
        with temp_dxf_file(uf) as tmp:
            results[key] = extract_file_data(
                tmp, uf.name, frame_lineweight,
                analysis=analyses[key] if analyses is not None else None)
    return results


def global_checked_region_names():
    """「領域一覧」の「特定」チェックボックス（`grc_<name>`）からチェック済みの
    領域名の集合を返す。個々のチェックボックスは `render_global_region_names_section()`
    が描画する（`st.multiselect` ではなく個別チェックボックスにしたのは、
    ポップアップに隠れず全候補を一覧できるようにするため、2026-07-30）。"""
    prefix = 'grc_'
    return {
        k[len(prefix):] for k, v in st.session_state.items()
        if isinstance(k, str) and k.startswith(prefix) and v
    }


def global_excluded_region_names():
    """「領域一覧」の「除外」チェックボックス（`gre_<name>`）からチェック済みの
    領域名の集合を返す（v3.11.0新設）。`allow_exclude=False` で描画した場合は
    `gre_` キー自体が作られないため、常に空集合になる。"""
    prefix = 'gre_'
    return {
        k[len(prefix):] for k, v in st.session_state.items()
        if isinstance(k, str) and k.startswith(prefix) and v
    }


def _region_excluded(reg, excluded_names, unnamed_label=None):
    """この領域の名称候補のいずれかが「除外」指定されているかを判定する
    （`model.ref_designator.excluded_region_ids()` と同じ正規化規約。v3.11.0新設）。
    名称候補が0件の無名領域は、「領域一覧」の表示名（`unnamed_label`）が除外指定
    されているかで判定する（2026-10-10 ユーザー決定。以前は無名領域は除外対象外）。"""
    if not excluded_names:
        return False
    if unnamed_label is not None and unnamed_label in excluded_names:
        return True
    return any(
        normalize_width(text) in excluded_names
        for (_dist, text) in reg.get('name_candidates', [])
    )


def gather_name_selections(fname, analysis):
    """この図面の領域名選択を集める。「領域一覧」でのグローバル決定（優先）と、
    「領域の確認」での個別選択（チェック済み候補が2件以上ある領域のみ）を統合する。

    「領域一覧」でチェックされなかった候補は「採用しない」という意思表示と
    みなす（2026-07-30 ユーザー指定）。そのため、候補が0件チェック済みの
    領域はここでは何も返さない（無所属のまま。候補が元々0件の無名領域は
    呼び出し先の `ref_designator.build_named_regions()` 等が自動 "no name"
    命名を行う。無名領域の「特定」「除外」は「領域一覧」の表示名で判定する）。

    「除外」指定された領域はここでは一切名称を決定しない（v3.11.0新設）。
    除外領域のラベルは `ref_designator.filter_labels_outside_excluded()` に
    よって名称採用の有無とは無関係に出力から取り除かれるため、ここで名称を
    決定しても最終結果には影響しないが、「除外は名称採用の判断自体をしない」
    という仕様を明確にするため、あえて候補走査の対象外にする。
    """
    checked_names = global_checked_region_names()
    excluded_names = global_excluded_region_names()
    unnamed_labels = unnamed_region_labels(analysis)
    name_selections = {}
    for reg in analysis.get('regions', []):
        label = unnamed_labels.get(reg['id'])
        if _region_excluded(reg, excluded_names, label):
            continue
        if label is not None:
            # 無名領域は「特定」が既定ON（従来から自動で "no name" として採用していた
            # ため）。「領域一覧」で明示的にOFFにしたときだけ採用しない（['']=採用しない
            # の印。`ref_designator.build_named_regions()` 参照）。「領域一覧」に出さない
            # 呼び出し側（DXF-label-comparison）ではキーが無いので従来どおり採用される。
            if not st.session_state.get(f"grc_{label}", True):
                name_selections[(fname, reg['id'])] = ['']
            continue
        decided = resolve_globally_decided_name(reg, checked_names)
        if decided is not None:
            name_selections[(fname, reg['id'])] = [decided]
            continue
        if not is_region_pending_selection(reg, checked_names):
            continue
        pending_cands = pending_candidates_for_region(reg, checked_names)
        chosen = []
        for i, (_d, t) in enumerate(pending_cands):
            if st.session_state.get(f"rc_{fname}_{reg['id']}_{i}"):
                chosen.append(t)
        if chosen:
            name_selections[(fname, reg['id'])] = chosen
    return name_selections


def on_change_radio(fname, reg_id, clicked_idx, n_cands, clicked_text, checked_names):
    """領域名チェックボックスをラジオボタン的に動作させるコールバック。

    `checked_names` は「領域一覧」でのチェック済み名称集合。他領域の候補は
    `pending_candidates_for_region()` で同じ集合に絞り込んでから走査する
    （`rc_<fname>_<reg_id>_<i>` のインデックスが実際の描画と対応するように
    するため、2026-07-30）。

    チェックが ON になったとき、(1) 同一領域の他チェックボックスを OFF にし、
    (2) 同じ名称候補を持つ他領域（候補が2件以上＝選択肢ありの領域のみ）にも
    同じ名称を選択状態として伝播する。チェックボックスは初回生成時にしか
    デフォルト値を設定できない（st.session_state 既存キーは上書きされない）ため、
    生成後にユーザーが選択を変更したときの伝播はこのコールバックで明示的に行う。

    ただし、同一ファイル内で互いに重なり（完全な内包も部分的な重複も含む）が
    ある領域（例: `EE6313-546-01E.dxf` の `B CHAMBER`〈外側〉と
    `BAKE HEATER UNIT RX`〈内側、完全内包〉）は、まったく別の物理位置を指す
    別個の領域であり、同期してはならない（v1.5.11、ユーザー報告: デフォルトでない
    候補を手動選択すると、重なりのある別領域も同じ名称に同期されてしまう。
    当初は内包のみを対象としていたが、部分的な重複も対象にすべきとの指摘により
    `regions_overlap()` に一般化）。同期は元々 MPD RACK2 のような、空間的に分離
    した（重ならない）複数ピースが同じ名称を共有するケースを想定したもの。
    """
    ck = f"rc_{fname}_{reg_id}_{clicked_idx}"
    if not st.session_state.get(ck, False):
        return
    for j in range(n_cands):
        if j != clicked_idx:
            st.session_state[f"rc_{fname}_{reg_id}_{j}"] = False
    st.session_state[f"rc_{fname}_{reg_id}_none"] = False  # 実候補を選んだら「領域選択しない」を解除

    analyses = st.session_state.get(ANALYSES_KEY, {})
    clicked_region = next(
        (r for r in analyses.get(fname, {}).get('regions', []) if r['id'] == reg_id), None)
    clicked_polygon = clicked_region['polygon'] if clicked_region else None

    for fn2, an2 in analyses.items():
        for r2 in an2.get('regions', []):
            if fn2 == fname and r2['id'] == reg_id:
                continue
            cands2 = pending_candidates_for_region(r2, checked_names)
            if len(cands2) <= 1:
                continue  # 選択肢なし（自動確定）の領域は対象外
            if not any(t2 == clicked_text for _d2, t2 in cands2):
                continue  # この名称を候補に持たない領域には影響しない
            if (fn2 == fname and clicked_polygon
                    and regions_overlap(clicked_polygon, r2['polygon'])):
                continue  # 重なり（内包/部分重複）のある領域同士は同期しない
            for j2, (_d2, t2) in enumerate(cands2):
                st.session_state[f"rc_{fn2}_{r2['id']}_{j2}"] = (t2 == clicked_text)


def on_change_none(fname, reg_id, n_cands, checked_names):
    """「領域選択しない」チェックボックスをラジオボタン的に動作させるコールバック。

    `checked_names` は「領域一覧」でのチェック済み名称集合（他領域の候補の
    絞り込みに使う。理由は `on_change_radio()` の docstring 参照）。

    ON になったとき、(1) 同一領域の名称候補チェックボックスを全て OFF にする、
    (2) この領域自身の候補テキストと一致するチェックボックスを、他領域
    （候補を1つでも持ち、かつ同一ファイル内で重なりのある領域を除く）でも
    OFF にする、(3) (2) の結果その他領域の候補が全て OFF になった場合は、
    その領域自身の「領域選択しない」も ON にする（2026-07-26 ユーザー指摘）。

    旧実装は (2)(3) を行わず「この領域だけの個別判断」としていた（「選択しない」は
    実在する名称候補ではないため、同名候補を持つ他領域まで一律「選択しない」
    にする理由がない、という理由）。しかし「選択しない」を選んだ以上この領域の
    候補群はどれも採用されなくなるにもかかわらず、`on_change_radio()` の (2)
    によって以前 ON 側へ同期された他領域の同名チェックボックスがそのまま
    残っていると、「この領域では不採用と決めた名称が、別領域では採用済みの
    まま」という矛盾した状態になる。ON化の伝播（`on_change_radio`）と対称的に
    OFF化も伝播させることで解消する。ON化と異なり「クリックされた1候補」では
    なく「この領域の候補すべて」が対象になる点に注意（この領域自身がどの候補
    にも紐付かないことを選んだため）。

    **候補1件のみ（自動確定）の領域も対象に含める**: `on_change_radio()` の
    (2) は「候補が2件以上＝選択肢ありの領域のみ」を同期対象とし、候補1件のみの
    領域（ユーザーが能動的に選んだわけではない自動確定）は除外している。だが
    この OFF 伝播ではその除外を適用しない。実例（2026-07-26 ユーザー報告、
    `DE5027-563-03A.dxf`）: 候補1件のみの領域が複数（領域21-24が候補
    `NXSAF FX1`、領域25-29が候補`SX13 FX`）連続しており、そのうち1つで
    「選択しない」を選んでも、候補1件のみの除外により兄弟領域に伝播せず
    「選択しない」も同期しなかった。ON伝播（能動的な選択と紛れることを防ぐ
    ための除外）と異なり、この OFF 伝播は「今まさに選ばれた選択しないという
    能動的な判断」を伝えるものであり、伝播先が候補1件だったかどうかは無関係
    （むしろ「同じ孤立候補を持つ領域群を一貫させる」というこの機能の目的そのもの
    に合致する）。

    さらに (3): OFF化の結果、他領域の候補が1つも選択されていない状態になると、
    「どの候補も選んでいない」のに「領域選択しない」も OFF という、実質的に
    未決定なまま宙に浮いた状態になる（ユーザー指摘: 「何も選択されていない
    領域は『領域選択しない』を ON にする必要がある」）。この領域自身の候補
    チェックボックスが（今回の伝播とは無関係な既存の OFF 状態も含めて）全て
    OFF になっていれば、その領域の「領域選択しない」を ON にして状態を明確化
    する。
    """
    ck = f"rc_{fname}_{reg_id}_none"
    if not st.session_state.get(ck, False):
        return
    for j in range(n_cands):
        st.session_state[f"rc_{fname}_{reg_id}_{j}"] = False

    analyses = st.session_state.get(ANALYSES_KEY, {})
    clicked_region = next(
        (r for r in analyses.get(fname, {}).get('regions', []) if r['id'] == reg_id), None)
    if not clicked_region:
        return
    clicked_texts = {t for _d, t in pending_candidates_for_region(clicked_region, checked_names)}
    clicked_polygon = clicked_region['polygon']

    for fn2, an2 in analyses.items():
        for r2 in an2.get('regions', []):
            if fn2 == fname and r2['id'] == reg_id:
                continue
            cands2 = pending_candidates_for_region(r2, checked_names)
            if not any(t2 in clicked_texts for _d2, t2 in cands2):
                continue  # この領域の候補を1つも持たない他領域には影響しない
                          # （候補0件・候補1件のみ＝自動確定の領域も対象に含める。
                          # docstring「候補1件のみ（自動確定）の領域も対象に含める」参照）
            if (fn2 == fname and regions_overlap(clicked_polygon, r2['polygon'])):
                continue  # 重なり（内包/部分重複）のある領域同士は同期しない
            for j2, (_d2, t2) in enumerate(cands2):
                if t2 in clicked_texts:
                    st.session_state[f"rc_{fn2}_{r2['id']}_{j2}"] = False
            if not any(st.session_state.get(f"rc_{fn2}_{r2['id']}_{j2}", False)
                       for j2 in range(len(cands2))):
                st.session_state[f"rc_{fn2}_{r2['id']}_none"] = True


def compute_default_candidate_index(fname, reg, cands, analyses, checked_names):
    """他の図面/領域で選択済みの名称があれば、それに一致する候補のインデックスを
    デフォルトとして返す（無ければ0）。

    `cands` は呼び出し元がこの領域用に描画する候補リスト（`pending_candidates_for_region()`
    で「領域一覧」チェック済みのものに絞り込み済み）。他領域を走査する際も
    同じ絞り込みを適用しないと、`rc_<fname>_<reg_id>_<i>` のインデックス `i` が
    実際に描画されたチェックボックスと対応しなくなる（2026-07-30）。

    候補が1件のみ（選択肢なし・自動確定）の領域は同期対象から除外する
    （ユーザーが能動的に選んだわけではない確定のため、隣接領域の候補にも同じ
    ラベルが上がる場合に誤って引き継ぐのを防ぐ）。

    この領域自身の最有力候補が Tier1/2（下端/上端最近傍。回転図面では右端/左端）
    の確信度の高い候補である場合は、他領域からの同期で上書きしない（v1.5.9）。
    同期は元々「この領域自身に強い候補が無く（Tier3）、他の領域で選ばれた同名
    候補を引き継ぐべき」ケース（例: MPD RACK2 のような複数ピース合算）のために
    設けたもので、隣接・入れ子の領域がそれぞれ独自の Tier1/2 候補を持つ場合に
    互いの選択を上書きしてしまう不具合があった（ユーザー報告: EE6313-546-01E.dxf
    の図面1/領域1,2 が同じ選択に同期されるが、本来は領域1=B CHAMBER、
    領域2=BAKE HEATER UNIT RX で別々が正しい）。

    同一ファイル内で互いに重なり（完全な内包も部分的な重複も含む）がある領域
    同士は、別個の領域として扱い同期しない（v1.5.11、ユーザー報告: デフォルト
    でない候補を手動選択すると、重なりのある別領域も同じ名称に同期されてしまう。
    当初は内包のみを対象としていたが、部分的な重複も対象にすべきとの指摘により
    `regions_overlap()` に一般化）。
    """
    if reg.get('default_name_tier') in (1, 2):
        return 0
    selected_elsewhere = set()
    for fn2, an2 in analyses.items():
        for r2 in an2.get('regions', []):
            if fn2 == fname and r2['id'] == reg['id']:
                continue
            cands2 = pending_candidates_for_region(r2, checked_names)
            if len(cands2) <= 1:
                continue  # 選択肢なし（自動確定）の領域はスキップ
            if fn2 == fname and regions_overlap(reg['polygon'], r2['polygon']):
                continue  # 重なり（内包/部分重複）のある領域同士は同期しない
            for j, (_d2, t2) in enumerate(cands2):
                if st.session_state.get(f"rc_{fn2}_{r2['id']}_{j}", False):
                    selected_elsewhere.add(t2)
    for i, (_d, t) in enumerate(cands):
        if t in selected_elsewhere:
            return i
    return 0


def render_corners_popover(corners):
    """領域の頂点座標一覧を📐ポップオーバーで表示する。"""
    with st.popover("📐"):
        st.markdown(f"**頂点の座標**（左下から / {len(corners)}点）")
        st.code(
            '\n'.join(
                f"{i + 1}: ({x:.2f}, {y:.2f})"
                for i, (x, y) in enumerate(corners)
            ) or '(なし)'
        )


def render_dangling_edges_section(region_dangling):
    """この領域の行き止まり枝（境界探索から除外された未閉路の線分群）を
    ⚠️エクスパンダーで表示する。`region_dangling` が空なら何もしない。"""
    if not region_dangling:
        return
    with st.expander(f"⚠️ この領域の行き止まり枝（{len(region_dangling)} 件）"):
        st.caption(
            "この領域の境界探索から除外された、どこにも閉じていない"
            "線分（境界線と同じ線種 lineweight=25/color=2）です。"
            "手描きの作画ミスの可能性があるため、該当する handle を"
            "確認してください。"
        )
        lines = []
        for br in region_dangling:
            att = br.get('attachment')
            att_str = f"({att[0]:.2f}, {att[1]:.2f})" if att else "(不明)"
            lines.append(f"取り付け点 {att_str}:")
            for ent in br['entities']:
                h = ent['handle'] or '(handle不明)'
                (sx, sy), (ex, ey) = ent['start'], ent['end']
                lines.append(
                    f"  handle {h}: "
                    f"({sx:.2f}, {sy:.2f}) - ({ex:.2f}, {ey:.2f})"
                )
        st.code('\n'.join(lines))


def invalidate_region_selection_confirmation():
    """「領域一覧」のチェック状態が変更されたら「領域選択を完了」を取り消す
    コールバック。再度「領域選択を完了」を押すまで「領域の確認」と後続の
    実行ボタンを隠す（決定内容が変わった以上、再確認が必要なため）。"""
    st.session_state[CONFIRMED_KEY] = False


def region_list_editor_state(prev, candidate_names, current):
    """「領域一覧」表のウィジェット状態（key用のver・表の元データbase・
    最後にこちらが書いたチェック状態expected）を返す純粋関数。

    表を作り直す（verを上げてbaseを`current`で取り直す）のは次の場合だけ:
      - 初回（prev が None）
      - 候補名の一覧（順序込み）が変わった
      - `current`（正本）が前回こちらが書いた`expected`と違う
        （「領域を検出」等で外部から正本がクリアされた）
    チェック操作では作り直さない。作り直すとスクロール位置が先頭へ戻る。

    `current` は {名称: (特定, 除外)}。"""
    names = list(candidate_names)
    if (prev is None or prev['names'] != names or prev['expected'] != current):
        return {
            'ver': 0 if prev is None else prev['ver'] + 1,
            'names': names, 'base': dict(current), 'expected': dict(current),
        }
    return prev


def render_global_region_names_section(candidate_names, locked, action='抽出', allow_exclude=False,
                                       default_on_names=()):
    """「領域一覧」セクション: `candidate_names`（呼び出し側が決める名称候補の
    一覧。表示順のまま使う）を表(st.data_editor)で表示し、領域名ごとに「特定」
    （`allow_exclude=True` のときはさらに「除外」）をON/OFFさせる
    （v3.11.0、チェックボックス個別表示から表形式に変更）。「領域選択を完了」
    ボタンを押すまでは「領域の確認」と後続の実行ボタンは表示されない（呼び出し側が
    `region_selection_confirmed` を見て判定する）。

    **特定**: 従来通りの振る舞い。チェック済み候補が「ちょうど1件」の領域は、
    その名称で自動的に決定される。チェック済み候補が「2件以上」残る領域だけが
    「領域の確認」での個別選択の対象になる（どれを採用するかは候補どうしが
    競合しているため個別に選ばせる）。チェック済み候補が「0件」の領域は、
    自動的に除外され「領域の確認」にも表示されない（未チェック＝対象にしないという
    意思表示のため）。候補1件の領域も同様に、この一覧でのチェックが対象にするか
    どうかの唯一のゲートになる（2026-07-30 ユーザー指定で確定した仕様）。

    **除外**（`allow_exclude=True` のときのみ列を表示。v3.11.0新設）: この領域
    （境界線上を含む）の内側にあるラベルを出力結果から完全に取り除く。
    「特定」の有無とは無関係にラベル自体が出力から消える。同じ領域名で「特定」
    「除外」の両方をONにした場合は「除外」が優先される（2026-10-08ユーザー決定）。
    `allow_exclude=False`（例: DXF-label-comparison）のときは「除外」列自体を
    表示しない。

    `default_on_names`: 「特定」の既定値をONにする名称（名称のない領域の表示名。従来から
    自動で採用していた領域の既定の出力を変えないため）。

    `candidate_names` が空の場合は案内メッセージのみ表示する（面積条件〈config.py
    の area_ratio / group_area_ratio〉を満たす領域が1件も無かった、または
    呼び出し側の絞り込みで候補が残らなかったケース）。
    """
    if not candidate_names:
        st.info("一定面積の領域は検出されませんでした。")
        return

    st.subheader("領域一覧")
    if allow_exclude:
        st.caption(
            "検出された全ファイル・全領域の名称候補（複数候補を持つ領域も含む）を"
            "重複なく一覧表示しています。「特定」は、その名称でこの領域を"
            f"{action}対象として採用します。候補が2件以上チェックされた領域は、"
            "「領域選択を完了」を押した後に表示される「領域の確認」で採用する領域を"
            "個別に選択する操作となります。候補を1つも「特定」しなかった領域は、"
            f"単に名称が採用されないだけで、領域内のラベルはそのまま{action}結果に"
            "残ります。「除外」はこの領域（境界線上を含む）の内側にあるラベルを"
            f"{action}結果から取り除きます（出力されません）。"
            "同じ領域名で両方チェックした場合は「除外」が優先されます。"
        )
    else:
        st.caption(
            "検出された全ファイル・全領域の名称候補（複数候補を持つ領域も含む）を"
            f"重複なく一覧表示しています。{action}したい領域名の「特定」にチェックを"
            f"入れてください（複数選択可）。その領域の名称候補がちょうど1件であれば、"
            f"その名称で自動的に{action}されます。候補が2件以上ある領域は、"
            "「領域選択を完了」を押した後に「領域の確認」でどれを採用するかを"
            "個別に選択していただきます。候補を1つも「特定」しなかった領域は、"
            f"単に名称が採用されず、領域内のラベルはそのまま{action}結果に残ります。"
        )

    # data_editor とのライブ連動は「正本dict」方式を使う（streamlitスキル§6参照）
    # ——表示中のdata_editorのsession_stateを直接書き換えると無限同期ループで
    # 画面が固まるため、正本（`grc_<name>`/`gre_<name>`）だけを更新する。
    # 表の元データ（base）とkeyは `region_list_editor_state()` が固定し、
    # チェック操作では作り直さない（作り直すとスクロール位置が先頭へ戻る）。
    default_on = set(default_on_names)
    for name in candidate_names:
        st.session_state.setdefault(f"grc_{name}", name in default_on)
        if allow_exclude:
            st.session_state.setdefault(f"gre_{name}", False)

    current = {
        name: (
            bool(st.session_state[f"grc_{name}"]),
            bool(st.session_state[f"gre_{name}"]) if allow_exclude else False,
        )
        for name in candidate_names
    }
    state = region_list_editor_state(
        st.session_state.get(EDITOR_STATE_KEY), candidate_names, current)
    st.session_state[EDITOR_STATE_KEY] = state

    rows = []
    for name in candidate_names:
        spec, excl = state['base'][name]
        row = {'特定': spec}
        if allow_exclude:
            row['除外'] = excl
        row['領域名'] = name
        rows.append(row)
    df = pd.DataFrame(rows)

    column_config = {'特定': st.column_config.CheckboxColumn('特定', width=CHECK_COLUMN_WIDTH)}
    column_order = ['特定']
    if allow_exclude:
        column_config['除外'] = st.column_config.CheckboxColumn('除外', width=CHECK_COLUMN_WIDTH)
        column_order.append('除外')
    column_order.append('領域名')
    column_config['領域名'] = st.column_config.TextColumn('領域名', width=NAME_COLUMN_WIDTH)
    # 列幅の合計が表の幅より小さいと、余白が全列に均等配分されて幅指定が無効になる
    # （Streamlit仕様）。表自体の幅を列幅の合計に固定して指定どおりの幅にする。
    table_width = CHECK_COLUMN_WIDTH * (len(column_order) - 1) + NAME_COLUMN_WIDTH

    edited = st.data_editor(
        df, key=f"grc_editor_{state['ver']}", hide_index=True,
        disabled=True if locked else ['領域名'],
        column_config=column_config, column_order=column_order, width=table_width,
    )

    edited_by_name = edited.set_index('領域名')
    new_values = {
        name: (
            bool(edited_by_name.loc[name, '特定']),
            bool(edited_by_name.loc[name, '除外']) if allow_exclude else False,
        )
        for name in candidate_names
    }
    if new_values != current:
        for name, (spec, excl) in new_values.items():
            st.session_state[f"grc_{name}"] = spec
            if allow_exclude:
                st.session_state[f"gre_{name}"] = excl
        # key・baseは据え置き（ウィジェットを保持する）。expectedだけ更新して
        # 「こちらが書いた変更」と「外部クリア」を区別できるようにする
        state['expected'] = new_values
        invalidate_region_selection_confirmation()
        st.rerun()

    # 「領域選択を完了」: 確定済みなら非表示にする（「領域を検出」ボタンと同じ
    # パターン——役目を終えたら消え、チェックを変更すると
    # `invalidate_region_selection_confirmation` により再度必要になる）。
    if not st.session_state.get(CONFIRMED_KEY):
        if st.button("領域選択を完了", type="primary"):
            st.session_state[CONFIRMED_KEY] = True
            st.rerun()


def region_confirmation_targets(analyses, checked_names):
    """「領域の確認」に表示すべきファイルを判定する（純粋関数）。

    表示対象は次のいずれか:
      - 「領域一覧」でチェック済みの候補が2件以上残っている領域（＝どれを
        採用するか競合している領域）を1つ以上持つファイル
      - 図面枠未検出以外の genuine なエラーを持つファイル（要選択領域が
        0件でも、エラー自体をユーザーに見せる必要があるため対象に含める）

    図面枠が見つからない既知の未対応ケース（`FRAME_NOT_FOUND_ERROR_MARKER`）
    は対象から除く。「除外」指定された領域は要選択候補の数に関わらず対象から
    除く（v3.11.0新設。§4決定2「除外領域は領域の確認に表示しない」）。
    戻り値はファイルキーでソートした `[(fname, analysis, pending_regions), ...]`。
    """
    excluded_names = global_excluded_region_names()
    targets = []
    for fname, analysis in sorted(analyses.items()):
        err = analysis.get('error')
        if err and FRAME_NOT_FOUND_ERROR_MARKER in err:
            continue  # 図面枠が見つからない既知の未対応ケースは表示しない
        regions = analysis.get('regions', [])
        pending_regions = [
            r for r in regions
            if is_region_pending_selection(r, checked_names)
            and not _region_excluded(r, excluded_names)
        ]
        if not err and not pending_regions:
            continue  # 個別選択が不要なファイルは表示しない
        targets.append((fname, analysis, pending_regions))
    return targets


def render_region_confirmation_section(analyses, locked, action='抽出'):
    """「領域の確認」セクション: 「領域一覧」でチェック済みの候補が2件以上
    残っている領域（＝どれを採用するか競合している領域）だけを対象に、
    チェック済み候補のみの名称候補チェックボックス群を表示する。チェック済み
    候補が0件の領域は自動的に除外され、ここには一切表示されない
    （2026-07-30 ユーザー指定で確定した仕様）。個別選択が不要なファイル
    （エラーが無く、かつ要選択の領域が1つも無いファイル）はセクション自体に
    表示しない（2026-07-30 ユーザー指定）。

    表示するかどうか（領域検出済み かつ「領域選択を完了」済み）は呼び出し側が
    判定する。表示対象ファイルが1件も無い場合（＝全領域が「領域一覧」で決定済み）
    は、見出し・キャプションを含めセクション自体を一切描画しない（2026-09-10
    ユーザー指定。何のアクションも取れない「空」の状態を見せないため）。"""
    checked_names = global_checked_region_names()
    targets = region_confirmation_targets(analyses, checked_names)
    if not targets:
        return

    st.subheader("領域の確認")
    st.caption(
        "「領域一覧」でチェックした領域の名称候補が2件以上ある（どれを採用する"
        "か決めることができない）ファイルのみを表示しています。候補を1つも"
        f"選択しなかった領域は{action}対象から除外され、ここには表示されません。"
    )

    rendered_any = False
    for fname, analysis, pending_regions in targets:
        err = analysis.get('error')
        if rendered_any:
            st.divider()
        rendered_any = True

        st.markdown(f"### {fname}")
        dn_parts = []
        if analysis.get('main_drawing_number'):
            dn_parts.append(f"図番：{analysis['main_drawing_number']}")
        if analysis.get('title'):
            dn_parts.append(f"タイトル：{analysis['title']}")
        if analysis.get('subtitle'):
            dn_parts.append(f"サブタイトル：{analysis['subtitle']}")
        if dn_parts:
            st.caption("　/　".join(dn_parts))
        n_frames = len(analysis.get('frames', []))
        n_regions = len(analysis.get('regions', []))
        st.caption(
            f"図面枠 {n_frames} 個 / 検出領域 {n_regions} 個"
            f"（うち要選択 {len(pending_regions)} 個）"
        )
        if err:
            st.warning(err)
            continue

        for reg_idx, reg in enumerate(pending_regions):
            if reg_idx > 0:
                st.divider()

            corners = reg.get('corners', [])
            # 「図面#/領域#（面積 xx%）」 と 📐 ボタンを同じ行に
            col_header, col_btn = st.columns([8, 1])
            with col_header:
                st.markdown(
                    f"　**図面{reg['frame'] + 1} / 領域{reg['id'] + 1}**"
                    f"　面積 {reg['area_pct']:.0f}%"
                )
            with col_btn:
                render_corners_popover(corners)

            render_dangling_edges_section(reg.get('dangling_edges', []))

            # 「領域一覧」でチェック済みの候補のみを選択肢にする
            # （未チェックの候補は「採用しない」という意思表示とみなし、
            # 選択肢自体に出さない。2026-07-30 ユーザー指定）。
            cands = pending_candidates_for_region(reg, checked_names)
            default_idx = compute_default_candidate_index(
                fname, reg, cands, analyses, checked_names)

            for i, (d, t) in enumerate(cands):
                ck = f"rc_{fname}_{reg['id']}_{i}"
                if ck not in st.session_state:
                    st.session_state[ck] = (i == default_idx)
                st.checkbox(
                    f"　{t}　（境界線からの距離 {d:.0f}）",
                    key=ck,
                    on_change=on_change_radio,
                    args=(fname, reg['id'], i, len(cands), t, checked_names),
                    disabled=locked,
                )

            # 領域選択しない（この領域にはどの名称候補も割り当てない）。
            # 既定は未チェック（従来通りいずれかの候補が既定選択される）で、
            # ユーザーが明示的に選んだときだけこの領域を無名扱いにする。
            ck_none = f"rc_{fname}_{reg['id']}_none"
            if ck_none not in st.session_state:
                st.session_state[ck_none] = False
            st.checkbox(
                "　（領域選択しない）",
                key=ck_none,
                on_change=on_change_none,
                args=(fname, reg['id'], len(cands), checked_names),
                disabled=locked,
            )
