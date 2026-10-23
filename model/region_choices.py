"""「領域の確認」の選択状態（streamlit 非依存）。

複数プロジェクトで共有するモジュール（primary: DXF-extract-labels。
DXF-label-comparison・HostPL-comparer にバイト一致のコピーがあり、各プロジェクトの
`tests/regression/spec/test_shared_files_identical_to_extract_labels.py`
が一致を検証する）。変更は primary で行い、コピーへそのまま伝播すること。

選択状態は**1領域1レコード**: `state[f"rc_{ファイル}_{領域id}"]` の dict。
  - `sel`: {候補の番号: 選んでいるか}。キーが無い＝未設定（`False` とは区別する）。番号は
    `cands`（最後に番号を振ったときの候補の文字列）に対するもの
  - `cands`: 番号を振ったときの候補の文字列のタプル。未初期化なら無い
  - `none`: 「（領域選択しない）」（要選択の領域用。同じ候補を持つ他の領域へ伝播して立つことがある）
  - `typed` / `typedtext`: 「領域名を入力」を選んでいるか／入力した文字列
  - `skip`: 確定済み・無名の領域の「（領域選択しない）」（伝播されない別のフラグ）
`state` は `st.session_state` でも素の dict でもよい。読むだけではレコードを作らない。
ページ上の widget key（`rcw_…`）は含まない（widget 側は `view/region_selection.py`）。

遷移（候補を選ぶ・選択しない・入力名・他領域への伝播）と、出力用の名称選択
（`resolve_name_selections()`）はここに集め、画面と出力で同じ規則を使う。
"""
import unicodedata

from .region_decisions import (
    DECIDED, EXCLUDED, OTHER_FRAME, PENDING, UNNAMED, UNSPECIFIED,
)
from .region_detector import pending_candidates_for_region, regions_overlap

ANALYSES_KEY = 'region_analyses'

_SCALAR_DEFAULTS = {'none': False, 'typed': False, 'typedtext': '', 'skip': False}


# ---------------------------------------------------------------------------
# レコードの読み書き（suffix は int＝候補の番号、または 'cands'/'none'/'typed'/'typedtext'/'skip'）
# ---------------------------------------------------------------------------

def choice_key(fname, reg_id):
    return f"rc_{fname}_{reg_id}"


def get_value(state, fname, reg_id, suffix, default=None):
    rec = state.get(choice_key(fname, reg_id))
    if rec is None:
        return default
    if isinstance(suffix, int):
        return rec['sel'].get(suffix, default)
    return rec.get(suffix, default)


def has_value(state, fname, reg_id, suffix):
    rec = state.get(choice_key(fname, reg_id))
    if rec is None:
        return False
    if isinstance(suffix, int):
        return suffix in rec['sel']
    return suffix in rec


def set_value(state, fname, reg_id, suffix, value):
    key = choice_key(fname, reg_id)
    rec = state.get(key)
    if rec is None:
        rec = state[key] = {'sel': {}}
    if isinstance(suffix, int):
        rec['sel'][suffix] = value
    else:
        rec[suffix] = value


def setdefault_value(state, fname, reg_id, suffix, value):
    if not has_value(state, fname, reg_id, suffix):
        set_value(state, fname, reg_id, suffix, value)


def pop_value(state, fname, reg_id, suffix):
    rec = state.get(choice_key(fname, reg_id))
    if rec is None:
        return
    if isinstance(suffix, int):
        rec['sel'].pop(suffix, None)
    else:
        rec.pop(suffix, None)


# ---------------------------------------------------------------------------
# 「領域名を入力」の照合
# ---------------------------------------------------------------------------

def normalize_typed_name(text):
    """「領域名を入力」の照合用の正規化: NFKC（全角→半角）・前後の空白除去・英大文字化。"""
    return unicodedata.normalize('NFKC', text or '').strip().upper()


def match_typed_region_name(text, labels):
    """入力された領域名を、図面枠内のラベル `labels`（`(text, x, y)` のリスト）と照合する。

    `normalize_typed_name()` した文字列どうしが**完全一致**する最初のラベルの
    **元の文字列**を返す。入力が空・一致するラベルが無ければ None（＝未選択）。"""
    key = normalize_typed_name(text)
    if not key:
        return None
    for label in labels:
        if normalize_typed_name(label[0]) == key:
            return label[0]
    return None


# ---------------------------------------------------------------------------
# 出力用の名称選択
# ---------------------------------------------------------------------------

def chosen_candidates(state, fname, reg_id, cands):
    """要選択の領域で選ばれている候補の文字列のリスト。選択状態の番号は、最後に初期化したときの
    候補（`cands`）に対するもの。スナップショットが無ければ現在の候補。"""
    snap = get_value(state, fname, reg_id, 'cands')
    current = [t for _d, t in cands]
    if snap is None:
        snap = tuple(current)
    return [t for i, t in enumerate(snap)
            if get_value(state, fname, reg_id, i) and t in current]


def resolve_name_selections(state, fname, analysis, decisions):
    """この図面の領域名選択を集める。領域の分類（`region_decisions.classify_regions()` の結果
    `decisions`）に、「領域の確認」での個別の上書きを重ねる。

    - 除外: 名称を決めない。除外領域のラベルは `ref_designator.filter_labels_outside_excluded()`
      が出力から取り除く。
    - 特定OFF（UNSPECIFIED）: 何も返さない。「領域の確認」の入力名・採用しない印が残っていても
      無視する（「抽出領域の選択」で候補を外した以上、その領域は名称を採用しない）。
    - 「（領域選択しない）」（`skip`。要選択でない領域用）: 採用しない印（`['']`）。入力名より優先
      （入力名は保持され、「領域選択しない」を外すと入力名に戻る）。
    - 「領域名を入力」（`typed`）: 枠内のラベルと完全一致したときだけ採用し、不一致は未選択
      （`['']`）。確定済み・無名の領域の名称変更も同じ。
    - 名称なし: 「特定」がOFFのときだけ `['']`。ONなら `build_named_regions()` が自動で "no name" にする。
    - 確定・別の図面枠で決まった名称: その名称。
    - 要選択: 「領域の確認」で選ばれた候補（無ければ何も返さない）。

    候補の選択は、最後に初期化したときの候補の文字列（`cands`）で解釈する。
    チェック済み候補が増減して番号がずれても、選んだ名称は変わらない。
    戻り値は {(ファイルキー, 領域id): 名称のリスト}。
    """
    name_selections = {}
    for reg in analysis.get('regions', []):
        d = decisions[reg['id']]
        rid = reg['id']
        if d.status in (EXCLUDED, UNSPECIFIED):
            continue
        if d.status != PENDING and get_value(state, fname, rid, 'skip'):
            name_selections[(fname, rid)] = ['']  # 入力名があっても採用しない
            continue
        if get_value(state, fname, rid, 'typed'):
            matched = match_typed_region_name(
                get_value(state, fname, rid, 'typedtext', ''),
                analysis.get('labels', []))
            name_selections[(fname, rid)] = [matched] if matched else ['']
            continue
        if d.status == UNNAMED:
            if not d.adopted:
                name_selections[(fname, rid)] = ['']
            continue
        if d.status in (DECIDED, OTHER_FRAME):
            name_selections[(fname, rid)] = [d.name]
            continue
        chosen = chosen_candidates(state, fname, rid, d.candidates)
        if chosen:
            name_selections[(fname, rid)] = chosen
    return name_selections


# ---------------------------------------------------------------------------
# 初期化・引き継ぎ
# ---------------------------------------------------------------------------

def sync_choice_snapshot(state, fname, reg, cands, analyses, checked_names):
    """候補の選択の番号 `i` は、その領域に提示する候補（「抽出領域の選択」でチェック済みの
    もの）の並びに対する番号。最後に番号を振ったときの候補の文字列を `cands` に持ち、
    候補が増減していたら、選んでいた名称を新しい並びへ引き継ぐ（選んでいた名称が無くなったら
    既定の候補。「領域選択しない」「領域名を入力」は維持）。選択を現在の並びの番号で
    読み書きする前に呼ぶ。スナップショットが無い（初期化前の）領域では何もしない。"""
    rid = reg['id']
    current = tuple(t for _d, t in cands)
    snap = get_value(state, fname, rid, 'cands')
    if snap is None or snap == current:
        return
    selected = {snap[i] for i in range(len(snap))
                if get_value(state, fname, rid, i)}
    for i in range(len(snap)):
        pop_value(state, fname, rid, i)
    carried = [t in selected for t in current]
    if (current and not any(carried)
            and not get_value(state, fname, rid, 'none')
            and not get_value(state, fname, rid, 'typed')):
        carried[compute_default_candidate_index(
            state, fname, reg, cands, analyses, checked_names)] = True
    for j, on in enumerate(carried):
        set_value(state, fname, rid, j, on)
    set_value(state, fname, rid, 'cands', current)


def ensure_choice_defaults(state, fname, reg, cands, analyses, checked_names):
    """この領域の選択状態が未設定なら既定値で初期化する。
    viewer ON/OFF のどちらの経路でも、領域を**表示順に**呼ぶこと（既定の候補は
    他領域の選択状態に依存するため）。候補が変わっていれば選択を引き継ぐ
    （`sync_choice_snapshot()`）。"""
    rid = reg['id']
    sync_choice_snapshot(state, fname, reg, cands, analyses, checked_names)
    if get_value(state, fname, rid, 'cands') is None:
        default_idx = compute_default_candidate_index(
            state, fname, reg, cands, analyses, checked_names)
        for i in range(len(cands)):
            setdefault_value(state, fname, rid, i, i == default_idx)
    set_value(state, fname, rid, 'cands', tuple(t for _d, t in cands))
    setdefault_value(state, fname, rid, 'none', False)
    setdefault_value(state, fname, rid, 'typed', False)
    setdefault_value(state, fname, rid, 'typedtext', '')


def ensure_typed_defaults(state, fname, reg_id):
    """「領域名を入力」の状態を未設定なら初期化する（確定済み・無名の領域の名称変更用）。"""
    setdefault_value(state, fname, reg_id, 'typed', False)
    setdefault_value(state, fname, reg_id, 'typedtext', '')
    setdefault_value(state, fname, reg_id, 'skip', False)


# ---------------------------------------------------------------------------
# 遷移
# ---------------------------------------------------------------------------

def on_change_typed(state, fname, reg_id, n_cands):
    """「領域名を入力」を選んだとき、同一領域の候補・「領域選択しない」を OFF にする
    （他領域には伝播しない）。"""
    if not get_value(state, fname, reg_id, 'typed', False):
        return
    set_value(state, fname, reg_id, 'skip', False)
    for j in range(n_cands):
        set_value(state, fname, reg_id, j, False)
    set_value(state, fname, reg_id, 'none', False)


def apply_viewer_choice(state, fname, reg, kind, checked_names, idx=None, text='', typed_text=None):
    """viewer（ブラウザ内）での選択を反映する。ページ上のチェックボックスと同じ
    遷移（`on_change_*`）を通すので、他領域への伝播・排他は同じ挙動になる。

    kind: 'cand'（候補 idx を選ぶ）／'none'（領域選択しない）／'typed'（領域名を入力を選ぶ）／
    'typedtext'（入力文字列を更新。選んだ扱いにする）／
    'typedoff'（確定済み・無名の領域で入力欄を空にした＝自動の名称に戻す）／
    'editnone'・'editnoneoff'（確定済み・無名の領域の「（領域選択しない）」の ON/OFF）"""
    cands = pending_candidates_for_region(reg, checked_names)
    reg_id = reg['id']
    if kind == 'cand':
        set_value(state, fname, reg_id, idx, True)
        on_change_radio(state, fname, reg_id, idx, len(cands), text, checked_names)
    elif kind == 'none':
        set_value(state, fname, reg_id, 'none', True)
        on_change_none(state, fname, reg_id, len(cands), checked_names)
    elif kind == 'typed':
        set_value(state, fname, reg_id, 'typed', True)
        on_change_typed(state, fname, reg_id, len(cands))
    elif kind == 'typedoff':
        set_value(state, fname, reg_id, 'typed', False)
        set_value(state, fname, reg_id, 'typedtext', '')
    elif kind == 'editnone':
        # 確定済み・無名の領域の「（領域選択しない）」。要選択の領域の `none` は他領域へ伝播して
        # 立つことがあるので、別のフラグ `skip` に持つ（伝播されない）。入力名（`typed`）は保持し、
        # 「領域選択しない」の間だけ採用されない（外すと入力名に戻る）
        set_value(state, fname, reg_id, 'skip', True)
    elif kind == 'editnoneoff':
        set_value(state, fname, reg_id, 'skip', False)
    elif kind == 'typedtext':
        set_value(state, fname, reg_id, 'typedtext', typed_text or '')
        set_value(state, fname, reg_id, 'typed', True)
        on_change_typed(state, fname, reg_id, len(cands))


def on_change_radio(state, fname, reg_id, clicked_idx, n_cands, clicked_text, checked_names):
    """領域名チェックボックスをラジオボタン的に動作させるコールバック。

    `checked_names` は「領域一覧」でのチェック済み名称集合。他領域の候補は
    `pending_candidates_for_region()` で同じ集合に絞り込んでから走査する
    （選択状態の番号が実際の描画と対応するようにするため、2026-07-30）。

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
    if not get_value(state, fname, reg_id, clicked_idx, False):
        return
    for j in range(n_cands):
        if j != clicked_idx:
            set_value(state, fname, reg_id, j, False)
    set_value(state, fname, reg_id, 'none', False)  # 実候補を選んだら「領域選択しない」を解除
    set_value(state, fname, reg_id, 'typed', False)  # 「領域名を入力」も解除

    analyses = state.get(ANALYSES_KEY, {})
    clicked_region = next(
        (r for r in analyses.get(fname, {}).get('regions', []) if r['id'] == reg_id), None)
    clicked_polygon = clicked_region['polygon'] if clicked_region else None

    for fn2, an2 in analyses.items():
        for r2 in an2.get('regions', []):
            if fn2 == fname and r2['id'] == reg_id:
                continue
            if get_value(state, fn2, r2['id'], 'typed'):
                continue  # 「領域名を入力」を選んだ領域には伝播しない
            cands2 = pending_candidates_for_region(r2, checked_names)
            if len(cands2) <= 1:
                continue  # 選択肢なし（自動確定）の領域は対象外
            if not any(t2 == clicked_text for _d2, t2 in cands2):
                continue  # この名称を候補に持たない領域には影響しない
            if (fn2 == fname and clicked_polygon
                    and regions_overlap(clicked_polygon, r2['polygon'])):
                continue  # 重なり（内包/部分重複）のある領域同士は同期しない
            sync_choice_snapshot(state, fn2, r2, cands2, analyses, checked_names)
            for j2, (_d2, t2) in enumerate(cands2):
                set_value(state, fn2, r2['id'], j2, t2 == clicked_text)


def on_change_none(state, fname, reg_id, n_cands, checked_names):
    """「領域選択しない」チェックボックスをラジオボタン的に動作させるコールバック。

    `checked_names` は「領域一覧」でのチェック済み名称集合（他領域の候補の
    絞り込みに使う。理由は `on_change_radio()` の docstring 参照）。

    ON になったとき、(1) 同一領域の名称候補チェックボックスを全て OFF にする、
    (2) この領域自身の候補テキストと一致するチェックボックスを、他領域
    （候補を1つでも持ち、かつ同一ファイル内で重なりのある領域を除く）でも
    OFF にする、(3) (2) の結果その他領域の候補が全て OFF になった場合は、
    その領域自身の「領域選択しない」も ON にする（2026-07-26 ユーザー指摘）。

    「選択しない」を選んだ以上この領域の候補群はどれも採用されなくなるため、
    ON化の伝播（`on_change_radio`）と対称的に OFF化も他領域へ伝播させる
    （「この領域では不採用と決めた名称が、別領域では採用済みのまま」という
    矛盾を避ける）。ON化と異なり「クリックされた1候補」ではなく「この領域の
    候補すべて」が対象になる点に注意。

    **候補1件のみ（自動確定）の領域も対象に含める**: `on_change_radio()` の
    (2) は「候補が2件以上＝選択肢ありの領域のみ」を同期対象とするが、この OFF 伝播は
    「今まさに選ばれた選択しないという能動的な判断」を伝えるものであり、伝播先が
    候補1件だったかどうかは無関係（実例: 2026-07-26 ユーザー報告、`DE5027-563-03A.dxf`）。

    (3): OFF化の結果、他領域の候補が1つも選択されていない状態になると、
    「どの候補も選んでいない」のに「領域選択しない」も OFF という、実質的に
    未決定なまま宙に浮いた状態になる。その領域の「領域選択しない」を ON にして
    状態を明確化する。
    """
    if not get_value(state, fname, reg_id, 'none', False):
        return
    for j in range(n_cands):
        set_value(state, fname, reg_id, j, False)
    set_value(state, fname, reg_id, 'typed', False)  # 「領域名を入力」も解除

    analyses = state.get(ANALYSES_KEY, {})
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
            if get_value(state, fn2, r2['id'], 'typed'):
                continue  # 「領域名を入力」を選んだ領域には伝播しない
            cands2 = pending_candidates_for_region(r2, checked_names)
            if not any(t2 in clicked_texts for _d2, t2 in cands2):
                continue  # この領域の候補を1つも持たない他領域には影響しない
                          # （候補0件・候補1件のみ＝自動確定の領域も対象に含める）
            if (fn2 == fname and regions_overlap(clicked_polygon, r2['polygon'])):
                continue  # 重なり（内包/部分重複）のある領域同士は同期しない
            sync_choice_snapshot(state, fn2, r2, cands2, analyses, checked_names)
            for j2, (_d2, t2) in enumerate(cands2):
                if t2 in clicked_texts:
                    set_value(state, fn2, r2['id'], j2, False)
            if not any(get_value(state, fn2, r2['id'], j2, False)
                       for j2 in range(len(cands2))):
                set_value(state, fn2, r2['id'], 'none', True)


def compute_default_candidate_index(state, fname, reg, cands, analyses, checked_names):
    """他の図面/領域で選択済みの名称があれば、それに一致する候補のインデックスを
    デフォルトとして返す（無ければ0）。

    `cands` は呼び出し元がこの領域用に描画する候補リスト（`pending_candidates_for_region()`
    で「領域一覧」チェック済みのものに絞り込み済み）。他領域を走査する際も
    同じ絞り込みを適用しないと、選択状態の番号が実際に描画されたチェックボックスと
    対応しなくなる（2026-07-30）。

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
    同士は、別個の領域として扱い同期しない（v1.5.11）。
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
                if get_value(state, fn2, r2['id'], j, False):
                    selected_elsewhere.add(t2)
    for i, (_d, t) in enumerate(cands):
        if t in selected_elsewhere:
            return i
    return 0
