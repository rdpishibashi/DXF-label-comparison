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
  - `rc_<ファイルキー>_<領域id>`: 「領域の確認」の選択状態の**正本**（1領域1レコードの dict。形と
    遷移は `model/region_choices.py`）。widget key ではない: ページ上のチェックボックスは
    `rcw_…` で、毎回この正本から写す（viewer ON のときにチェックボックスを描かない回があっても
    Streamlit に消されないようにするため）。「領域名を入力」の選択と入力文字列も同じ正本
"""
import contextlib
import os
import time

import pandas as pd
import streamlit as st

from model import region_choices as rc
from model.common_utils import save_uploadedfile, normalize_width
from model.region_detector import (
    DEFAULT_REGION_CONFIG,
    pending_candidates_for_region,
)
from model.extraction_pipeline import detect_file_regions, extract_file_data
from model.region_decisions import (
    DECIDED, OTHER_FRAME, PENDING,
    classify_regions, region_excluded, resolve_by_other_frames,
)

# 「領域一覧」セクションの見出し（2026-10-17 ユーザー指定で改称。コード中の「領域一覧」は同じ
# セクションを指す。Excel のシート名 `領域一覧` は別物で変えない）
SECTION_TITLE = '抽出領域の選択'
ANALYSES_KEY = rc.ANALYSES_KEY
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


# 判定の本体は `model/region_decisions.py`。app.py・テストが使う従来の名前を残す
_region_excluded = region_excluded


def classify_file_regions(analysis):
    """この図面の通常領域の分類 `{領域id: RegionDecision}`（セッションの「特定」「除外」を反映）。
    名称なし領域の「特定」は `grc_<表示名>`（キーが無ければON）。"""
    return classify_regions(
        analysis, global_checked_region_names(), global_excluded_region_names(),
        unnamed_adopted=lambda label: st.session_state.get(f"grc_{label}", True))


def unconditional_region_names(analyses):
    """「領域の確認」を経ずに決まる領域名（NFKC正規化済みの集合）。

    各ファイルの通常領域のうち、「抽出領域の選択」のチェックで候補がちょうど1件に決まる名称
    （DECIDED）と、同じファイルの別の図面枠で決まった名称で決まるもの（OTHER_FRAME）を集める。
    「領域の確認」で手動選択した名称は含まれない。5%未満の小領域の採用対象
    （`ref_designator.add_minor_region_selections()` の `target_names`）に使う。
    """
    names = set()
    for analysis in analyses.values():
        for d in classify_file_regions(analysis).values():
            if d.status in (DECIDED, OTHER_FRAME):
                names.add(normalize_width(d.name))
    return names


def gather_name_selections(fname, analysis):
    """この図面の領域名選択を集める（本体は `model/region_choices.resolve_name_selections()`）。
    領域の分類はセッションの「特定」「除外」から作る。戻り値は {(ファイルキー, 領域id): 名称のリスト}。"""
    return rc.resolve_name_selections(
        st.session_state, fname, analysis, classify_file_regions(analysis))


# 選択状態の読み書きと遷移の本体は `model/region_choices.py`。以下は `st.session_state` を渡す
# だけの薄いラッパー（呼び出し側の名前と引数を保つ）
normalize_typed_name = rc.normalize_typed_name
match_typed_region_name = rc.match_typed_region_name


def ensure_choice_defaults(fname, reg, cands, analyses, checked_names):
    rc.ensure_choice_defaults(st.session_state, fname, reg, cands, analyses, checked_names)


def on_change_radio(fname, reg_id, clicked_idx, n_cands, clicked_text, checked_names):
    rc.on_change_radio(
        st.session_state, fname, reg_id, clicked_idx, n_cands, clicked_text, checked_names)


def on_change_none(fname, reg_id, n_cands, checked_names):
    rc.on_change_none(st.session_state, fname, reg_id, n_cands, checked_names)


def compute_default_candidate_index(fname, reg, cands, analyses, checked_names):
    return rc.compute_default_candidate_index(
        st.session_state, fname, reg, cands, analyses, checked_names)


def _sync_widget_to_canonical(wkey, fname, reg_id, suffix):
    """ページ上のチェックボックス（widget key `wkey`）の値を、選択状態へ写す。"""
    rc.set_value(st.session_state, fname, reg_id, suffix, st.session_state.get(wkey, False))


def on_widget_radio(fname, reg_id, clicked_idx, n_cands, clicked_text, checked_names):
    """ページ上の候補チェックボックス（`rcw_…`）の変更を選択状態へ写してから既存の挙動を行う。"""
    _sync_widget_to_canonical(f"rcw_{fname}_{reg_id}_{clicked_idx}", fname, reg_id, clicked_idx)
    on_change_radio(fname, reg_id, clicked_idx, n_cands, clicked_text, checked_names)


def on_widget_none(fname, reg_id, n_cands, checked_names):
    _sync_widget_to_canonical(f"rcw_{fname}_{reg_id}_none", fname, reg_id, 'none')
    on_change_none(fname, reg_id, n_cands, checked_names)


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


def region_list_dataframe(candidate_names, values, allow_exclude):
    """「抽出領域の選択」表の元データ。`values` は {名称: (特定, 除外)}。

    通常は `region_list_editor_state()` の `base`（表を作り直さないための固定値）を渡し、
    ロック中（結果表示中）は現在の選択状態 `current` を渡す。ロックで `disabled` が変わると
    ウィジェットのIDが変わって編集内容（ウィジェット側の状態）が失われ、`base` のままだと
    チェックが全部外れて表示されてしまうため（2026-10-17）。"""
    rows = []
    for name in candidate_names:
        spec, excl = values[name]
        row = {'特定': spec}
        if allow_exclude:
            row['除外'] = excl
        row['領域名'] = name
        rows.append(row)
    return pd.DataFrame(rows)


def render_global_region_names_section(candidate_names, locked, action='抽出', allow_exclude=False,
                                       default_on_names=(), side_widget=None, above_table=None):
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

    `side_widget`（引数なしの呼び出し可能オブジェクト、既定 None）: 渡すと表の右側の列で呼び出す
    （表と並べて置きたいオプションウィジェット用。2026-10-17）。None なら表だけを幅いっぱいに置く従来表示。

    `above_table`（引数なしの呼び出し可能オブジェクト、既定 None）: 渡すと表の上で呼び出す（キャプションの
    後。呼び出し側のボタンなどを置く。ここで `grc_<名称>` を書き換えれば、表は外部からの変更として
    作り直される）。

    `candidate_names` が空の場合は案内メッセージのみ表示する（面積条件〈config.py
    の area_ratio / group_area_ratio〉を満たす領域が1件も無かった、または
    呼び出し側の絞り込みで候補が残らなかったケース）。
    """
    if not candidate_names:
        st.info("一定面積の領域は検出されませんでした。")
        return

    st.subheader(SECTION_TITLE)
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

    # 正本だけを更新する。表は `region_list_editor_state()` が「正本が前回書いた値と違う」
    # ことを検知して作り直す（外部クリアと同じ経路）
    if above_table is not None:
        above_table()

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

    df = region_list_dataframe(candidate_names, current if locked else state['base'], allow_exclude)

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

    def _editor():
        return st.data_editor(
            df, key=f"grc_editor_{state['ver']}", hide_index=True,
            disabled=True if locked else ['領域名'],
            column_config=column_config, column_order=column_order, width=table_width,
        )

    if side_widget is None:
        edited = _editor()
    else:
        col_table, col_side = st.columns([5, 4])
        with col_table:
            edited = _editor()
        with col_side:
            side_widget()

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
    除く（v3.11.0新設。§4決定2「除外領域は領域の確認に表示しない」）。同じファイルの別の
    図面枠で決まった名称で決められる領域も除く（`resolve_by_other_frames()`、2026-10-17）。
    戻り値はファイルキーでソートした `[(fname, analysis, pending_regions), ...]`。
    （DXF-extract-labels の viewer 版は全ファイルを対象にする: `view/region_confirm_viewer.py`）
    """
    excluded_names = global_excluded_region_names()
    targets = []
    for fname, analysis in sorted(analyses.items()):
        err = analysis.get('error')
        if err and FRAME_NOT_FOUND_ERROR_MARKER in err:
            continue  # 図面枠が見つからない既知の未対応ケースは表示しない
        decisions = classify_regions(analysis, checked_names, excluded_names)
        pending_regions = [
            r for r in analysis.get('regions', []) if decisions[r['id']].status == PENDING]
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
    ユーザー指定。何のアクションも取れない「空」の状態を見せないため）。

    DXF-extract-labels は図面上の viewer で選ぶ別の画面を使う（`view/region_confirm_viewer.py`）。
    部品（`render_confirmation_file_header()`・`ensure_pending_defaults()`・
    `render_pending_region_blocks()`）は両方が使う。"""
    checked_names = global_checked_region_names()
    targets = region_confirmation_targets(analyses, checked_names)
    if not targets:
        return

    st.subheader("領域の確認")
    st.caption(
        f"「{SECTION_TITLE}」でチェックした領域の名称候補が2件以上ある（どれを採用する"
        "か決めることができない）ファイルのみを表示しています。同じファイルの別の図面で"
        "特定できた名称がある領域は、その名称を自動で採用するため表示しません。候補を1つも"
        f"選択しなかった領域は{action}対象から除外され、ここには表示されません。"
    )

    rendered_any = False
    for item in targets:
        fname, analysis, pending_regions = item
        container = st.container()
        if rendered_any:
            st.divider()
        rendered_any = True
        with container:
            _render_confirmation_file(
                fname, analysis, pending_regions, analyses, checked_names, locked)


def render_confirmation_file_header(fname, analysis, pending_regions):
    """「領域の確認」の1ファイルの見出し（ファイル名・図番/タイトル・図面枠/領域の件数）。
    エラーがあれば警告を出して True を返す（呼び出し側は以降の描画を止める）。"""
    err = analysis.get('error')
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
        return True
    return False


def ensure_pending_defaults(fname, pending_regions, analyses, checked_names):
    """要選択の領域の選択状態を、表示順に既定値で初期化する（`rc.ensure_choice_defaults()`。
    ページ上の選択肢と viewer のどちらで選ぶ場合も、描画の前に行う）。"""
    for reg in pending_regions:
        ensure_choice_defaults(
            fname, reg, pending_candidates_for_region(reg, checked_names),
            analyses, checked_names)


def _render_confirmation_file(fname, analysis, pending_regions, analyses, checked_names, locked):
    """「領域の確認」の1ファイル分（`render_region_confirmation_section()` の本体の一部）。"""
    if render_confirmation_file_header(fname, analysis, pending_regions):
        return
    # 選択状態を表示順に初期化する
    ensure_pending_defaults(fname, pending_regions, analyses, checked_names)
    render_pending_region_blocks(fname, pending_regions, checked_names, locked)


def render_pending_region_blocks(fname, pending_regions, checked_names, locked):
    """要選択の領域ごとに、ページ上の選択肢（📐・行き止まり枝・名称候補のチェックボックス・
    「（領域選択しない）」）を出す。"""
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

        # チェックボックスの widget key は `rcw_…`。毎回、選択状態（正本 `rc_…`）から写す
        for i, (d, t) in enumerate(cands):
            wkey = f"rcw_{fname}_{reg['id']}_{i}"
            st.session_state[wkey] = rc.get_value(st.session_state, fname, reg['id'], i, False)
            st.checkbox(
                f"　{t}　（境界線からの距離 {d:.0f}）",
                key=wkey,
                on_change=on_widget_radio,
                args=(fname, reg['id'], i, len(cands), t, checked_names),
                disabled=locked,
            )

        # 領域選択しない（この領域にはどの名称候補も割り当てない）。
        # 既定は未チェック（従来通りいずれかの候補が既定選択される）で、
        # ユーザーが明示的に選んだときだけこの領域を無名扱いにする。
        wkey_none = f"rcw_{fname}_{reg['id']}_none"
        st.session_state[wkey_none] = rc.get_value(st.session_state, fname, reg['id'], 'none', False)
        st.checkbox(
            "　（領域選択しない）",
            key=wkey_none,
            on_change=on_widget_none,
            args=(fname, reg['id'], len(cands), checked_names),
            disabled=locked,
        )
