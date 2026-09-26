"""ラベル比較のコアロジック（Streamlit非依存の純関数）。

入力は共有パイプライン（`model/ref_designator.py`）がDXFから抽出したファイル単位の
結果（{ファイルキー: {'rows': [...], 'region_label_counts': {...}, 'title': ...}}）。
ラベルは抽出時に NFKC 正規化（全角→半角）済み。
"""
import pandas as pd

from model.ref_designator import is_ref_designator_label

CANDIDATE_COLUMN = '機器符号候補'
DIFF_COLUMNS = [CANDIDATE_COLUMN, 'ラベル', '区分', 'A個数', 'B個数']
REGION_DIFF_COLUMNS = ['領域名'] + DIFF_COLUMNS
KUBUN_BOTH = '両方'
KUBUN_A_ONLY = 'A のみ'
KUBUN_B_ONLY = 'B のみ'

# 表示色（Excel出力・画面表示で共通利用）。
# 区分が「両方」でも個数が一致するかどうかで色を分けるため、区分そのものとは
# 別に「表示スタイル区分」（ROW_STYLE_*）を設ける。
ROW_STYLE_A_ONLY = 'A_ONLY'
ROW_STYLE_B_ONLY = 'B_ONLY'
ROW_STYLE_MISMATCH = 'MISMATCH'
ROW_STYLE_MATCH = 'MATCH'

COLOR_A_ONLY = {'bg_color': '#D9E1F2', 'font_color': '#1F4E79'}      # 青
COLOR_B_ONLY = {'bg_color': '#C6EFCE', 'font_color': '#006100'}      # 緑
COLOR_MISMATCH = {'bg_color': '#FFEB9C', 'font_color': '#9C6500'}    # 黄

# ROW_STYLE_* -> 色（MATCH は無色のため None）
ROW_STYLE_COLORS = {
    ROW_STYLE_A_ONLY: COLOR_A_ONLY,
    ROW_STYLE_B_ONLY: COLOR_B_ONLY,
    ROW_STYLE_MISMATCH: COLOR_MISMATCH,
    ROW_STYLE_MATCH: None,
}


def row_style(kubun: str, a_count, b_count) -> str:
    """区分と個数から表示スタイル区分（ROW_STYLE_*）を返す。

    - 区分が『A のみ』『B のみ』ならそのまま対応する区分を返す（青／緑）。
    - 区分が『両方』の場合、A個数とB個数が一致すれば無色（MATCH）、
      不一致（片方が欠損の場合も含む）なら黄（MISMATCH）。
    """
    if kubun == KUBUN_A_ONLY:
        return ROW_STYLE_A_ONLY
    if kubun == KUBUN_B_ONLY:
        return ROW_STYLE_B_ONLY
    if pd.notna(a_count) and pd.notna(b_count) and a_count == b_count:
        return ROW_STYLE_MATCH
    return ROW_STYLE_MISMATCH


def compare_key(label: str) -> str:
    """比較用のラベル文字列を返す（"("・"（"〈半角/全角とも〉以降を除去し、
    直前の空白があれば併せて除去する）。

    括弧内の補足（抵抗値・ワイヤ色コード等、例 `R10(2.2K)`・`FBWH（白）`）だけが
    異なる同一部品を、A・B比較上は同一ラベルとして扱うため。DXF-extract-labels の
    「結線図-組立図を比較」（`model/assembly_compare._compare_key()`）と同じ規則
    （同ファイルは共有していないため、規則を変える場合は両方を揃えること）。
    括弧を含まない場合、および除去すると空になる場合（括弧で始まる `(BU)` 等の
    電線色などの補足。空キーに合算されて1行にまとまるのを防ぐ）は元の文字列を
    そのまま返す。
    """
    idx = min((i for i in (label.find('('), label.find('（')) if i >= 0), default=-1)
    if idx < 0:
        return label
    return label[:idx].rstrip() or label


def aggregate_labels(results: dict, keys) -> dict:
    """`keys` のファイルの `rows`（ラベル・個数）を比較キー（`compare_key()`）で
    合算し、{比較キー: 合計個数} を返す。"""
    agg: dict = {}
    for key in keys:
        for row in results[key].get('rows', []):
            label = compare_key(row['ラベル'])
            agg[label] = agg.get(label, 0) + int(row['個数'])
    return agg


def aggregate_region_labels(results: dict, keys, regions) -> dict:
    """`keys` のファイルの領域別ラベル個数（`region_label_counts`）のうち、
    `regions` に含まれる領域名だけを比較キーで合算し、
    {領域名: {比較キー: 合計個数}} を返す（該当が無い領域は空dict）。"""
    agg = {region: {} for region in regions}
    for key in keys:
        for region, counts in results[key].get('region_label_counts', {}).items():
            if region not in agg:
                continue
            region_dict = agg[region]
            for label, count in counts.items():
                ck = compare_key(label)
                region_dict[ck] = region_dict.get(ck, 0) + int(count)
    return agg


def compare_labels(a: dict, b: dict) -> pd.DataFrame:
    """{比較キー: 個数} 2 つを比較し、差分 DataFrame を返す。

    - columns: DIFF_COLUMNS（機器符号候補・ラベル・区分・A個数・B個数）
    - 機器符号候補: 比較キーが機器符号候補なら 'Y'、それ以外は None
      （`ref_designator.is_ref_designator_label()`、DXF-extract-labels と同じ判定）
    - 区分 ∈ {'A のみ', 'B のみ', '両方'}
    - 無い側の個数は pd.NA
    - ラベル昇順（sorted）
    """
    labels = sorted(set(a) | set(b))
    rows = []
    for lbl in labels:
        in_a, in_b = lbl in a, lbl in b
        kubun = KUBUN_BOTH if (in_a and in_b) else (KUBUN_A_ONLY if in_a else KUBUN_B_ONLY)
        rows.append({
            CANDIDATE_COLUMN: 'Y' if is_ref_designator_label(lbl) else None,
            'ラベル': lbl,
            '区分': kubun,
            'A個数': a.get(lbl, pd.NA),
            'B個数': b.get(lbl, pd.NA),
        })
    df = pd.DataFrame(rows, columns=DIFF_COLUMNS)
    # 個数は pandas の nullable 整数型にする。素の object dtype のままだと
    # st.dataframe（Arrow経由の描画）で pd.NA が文字列 "None" として
    # 表示されてしまうため（Excel出力側は write_blank で別途空欄化している）。
    df['A個数'] = df['A個数'].astype('Int64')
    df['B個数'] = df['B個数'].astype('Int64')
    return df


def summarize_metrics(df: pd.DataFrame) -> dict:
    """区分カウントのみを返す（ファイル数等のヘッダー情報を含まない軽量版）。

    'A ユニークラベル数'〜'両方' の5項目。`summarize()` と
    `build_region_summary_rows()`（指定領域での比較、領域ごとの内訳）が共用する。
    """
    a_only = int((df['区分'] == KUBUN_A_ONLY).sum())
    b_only = int((df['区分'] == KUBUN_B_ONLY).sum())
    both = int((df['区分'] == KUBUN_BOTH).sum())
    return {
        'A ユニークラベル数': both + a_only,
        'B ユニークラベル数': both + b_only,
        'A のみ': a_only,
        'B のみ': b_only,
        '両方': both,
    }


def summary_header(a_file_count: int, b_file_count: int, b_filter_mode: str,
                   b_target_count: int) -> dict:
    """サマリー先頭のヘッダー項目（ファイル数・B側の絞り込み条件と対象ファイル数）。"""
    return {
        'A ファイル数': a_file_count,
        'B ファイル数': b_file_count,
        'B 絞り込み条件': b_filter_mode,
        'B 対象ファイル数': b_target_count,
    }


def summarize(df: pd.DataFrame, header: dict) -> dict:
    """サマリー用の件数集計を返す（`summary_header()` の項目＋区分カウント）。"""
    result = dict(header)
    result.update(summarize_metrics(df))
    return result


def compare_labels_by_region(a_by_region: dict, b_by_region: dict, regions: list):
    """指定した領域名（regions、表示順）ごとに `compare_labels()` を実行し、
    先頭に『領域名』列を持つ1つの DataFrame に結合する。

    a_by_region/b_by_region: {領域名: {ラベル: 個数}}（該当領域が無ければ空dict扱い）。
    戻り値: (diff_df, metrics_by_region)
      diff_df: columns=REGION_DIFF_COLUMNS。regions の順で連結（regions内では
               ラベル昇順）。regions が空なら0行のDataFrame。
      metrics_by_region: {領域名: summarize_metrics()の戻り値}（regions と同じ順）
    """
    frames = []
    metrics_by_region: dict = {}
    for region in regions:
        region_df = compare_labels(a_by_region.get(region, {}), b_by_region.get(region, {}))
        metrics_by_region[region] = summarize_metrics(region_df)
        region_df = region_df.copy()
        region_df.insert(0, '領域名', region)
        frames.append(region_df)
    if frames:
        diff_df = pd.concat(frames, ignore_index=True)
    else:
        diff_df = pd.DataFrame(columns=REGION_DIFF_COLUMNS)
    return diff_df, metrics_by_region


def blank_repeated_column(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """表示専用: `column` の値が直前の行と同じ場合、その行では空欄にする。

    `compare_labels_by_region()` は領域名ごとにまとまった連続ブロックで
    行を返すため、ブロックの先頭行だけ領域名を残し、残りを空欄にする
    （`build_region_summary_rows()` と同じ「見出し1回＋空欄」レイアウト）。
    呼び出し元の元データは変えず、表示直前にのみ使うこと。
    """
    out = df.copy()
    is_repeat = out[column] == out[column].shift()
    out.loc[is_repeat, column] = ''
    return out


def build_region_summary_rows(metrics_by_region: dict, header: dict) -> list:
    """領域名ごとの `summarize_metrics()` 結果から、サマリーシート用の行リスト
    （領域名・項目・値の3キーを持つ dict のリスト）を構築する。

    先頭に `header`（`summary_header()`）の各項目（領域名は空欄）を置き、
    続けて領域名ごとに5項目（A ユニークラベル数〜両方）のブロックを
    metrics_by_region の順で並べる。各領域ブロックの**先頭行だけ**領域名を入れ、
    同じブロック内の残り4行は空欄にする（ユーザー指定のレイアウト）。
    """
    rows = [{'領域名': '', '項目': k, '値': v} for k, v in header.items()]
    for region, metrics in metrics_by_region.items():
        for i, (key, value) in enumerate(metrics.items()):
            rows.append({'領域名': region if i == 0 else '', '項目': key, '値': value})
    return rows
