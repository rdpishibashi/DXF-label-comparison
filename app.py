import time
import traceback

import streamlit as st
import pandas as pd

from model.region_detector import collect_unique_region_candidate_names
from model import ref_designator
from model.compare_labels import (
    aggregate_labels, aggregate_region_labels, compare_labels, summarize, summary_header,
    compare_labels_by_region, build_region_summary_rows,
    blank_repeated_column, row_style, ROW_STYLE_COLORS,
)
from model.excel_output import create_compare_excel_output, create_region_compare_excel_output
from model.drawing_filter import select_files, FILTER_OPTIONS
from config import region_detection_config
from view import region_selection

st.set_page_config(page_title="DXF Label Comparison", page_icon="🔍", layout="wide")

OUTPUT_FILENAME = 'label_comparison.xlsx'
A_PREFIX = '[A] '
B_PREFIX = '[B] '

# 比較結果（「展開図-結線図比較」実行時に生成し、「新しい比較を開始」で消す）
_RESULT_KEYS = [
    'cmp_diff', 'cmp_summary', 'cmp_output', 'cmp_is_region_mode', 'cmp_region_metrics',
    'cmp_download_done',
]
# 領域検出の状態（「領域を検出」の再実行・入力ファイルの変更・オプションOFFで消す）
_REGION_KEYS = [region_selection.ANALYSES_KEY, region_selection.CONFIRMED_KEY,
                'cmp_detected_files']


def _handle_error(e):
    st.error(f"エラーが発生しました: {str(e)}")
    st.error(traceback.format_exc())


def _clear_session_keys(keys=(), prefixes=()):
    for k in keys:
        if k in st.session_state:
            del st.session_state[k]
    if prefixes:
        for k in list(st.session_state.keys()):
            if isinstance(k, str) and k.startswith(prefixes):
                del st.session_state[k]


def _clear_region_state():
    _clear_session_keys(keys=_REGION_KEYS, prefixes=region_selection.SELECTION_KEY_PREFIXES)


def _dxf_only(uploaded_files):
    """フォルダD&Dで混ざる非DXFファイルを除外する（`type=` は指定しない。
    streamlit スキル §3: 指定すると混在フォルダの再帰読み込みが壊れる）。"""
    return [f for f in (uploaded_files or []) if f.name.lower().endswith('.dxf')]


def _row_style_factory(diff_df):
    """`_for_display()` は A個数/B個数 を表示用文字列に変換するため、色分けの
    判定（`row_style()`、個数の一致比較を含む）は変換前の元データを使う。"""
    def _row_style(row):
        raw = diff_df.loc[row.name]
        style_key = row_style(raw['区分'], raw['A個数'], raw['B個数'])
        colors = ROW_STYLE_COLORS[style_key]
        css = f"background-color: {colors['bg_color']}; color: {colors['font_color']}" if colors else ''
        return [css] * len(row)
    return _row_style


def _for_display(diff_df):
    """画面表示用に欠損の個数・機器符号候補を空欄にし、『領域名』列があれば連続する重複を空欄にする。"""
    disp = diff_df.copy()
    for col in ('A個数', 'B個数'):
        disp[col] = disp[col].apply(lambda value: '' if pd.isna(value) else f"{int(value):,}")
    disp['機器符号候補'] = disp['機器符号候補'].fillna('')
    if '領域名' in disp.columns:
        disp = blank_repeated_column(disp, '領域名')
    return disp


def _summary_for_display(rows):
    """画面表示用: サマリー行を DataFrame 化する。『値』列は文字列と整数が混在する
    ため、Arrow 変換エラーを避けて表示直前に文字列化する（数値は3桁区切り）。"""
    disp = pd.DataFrame(rows)
    disp['値'] = disp['値'].apply(lambda v: f"{v:,}" if isinstance(v, int) else str(v))
    return disp


def _render_intro():
    st.markdown("""
        <style>
        @font-face {
            font-family: "AppMixedFont";
            src: local("Hiragino Kaku Gothic ProN"), local("Yu Gothic UI"),
                 local("Yu Gothic"), local("Meiryo");
            unicode-range: U+3000-303F,
                           U+3040-30FF,
                           U+FF00-FFEF,
                           U+4E00-9FFF, U+3400-4DBF;
            size-adjust: 94%;
        }
        @font-face {
            font-family: "AppMixedFont";
            src: local("Source Sans Pro"), local("Helvetica Neue"), local("Arial");
        }
        .stApp, .stApp p, .stApp li, .stApp label, .stApp td, .stApp th,
        .stApp h1, .stApp h2, .stApp h3, .stApp input, .stApp button {
            font-family: "AppMixedFont", sans-serif !important;
        }
        </style>
    """, unsafe_allow_html=True)
    st.title("DXF Label Comparison - 展開図-結線図比較")
    st.write(
        "展開接続図のDXFファイル(A)とUNIT内結線図のDXFファイル(B)からラベルを抽出し、"
        "ラベル（機器符号）を比較します。"
    )
    with st.expander("ℹ️ プログラム説明", expanded=False):
        st.info("\n".join([
            "ラベル抽出は DXF-extract-labels と同じ処理です（図面枠内・図面情報枠外の"
            "全ラベル）。ラベルの「(」「（」以降は除いて比較します（例: R10(2.2K) と R10 は"
            "同じラベル）。機器符号パターンに一致するラベルには「機器符号候補」に Y を付けます。",
            "",
            "**使用手順：**",
            "1. A（展開接続図）・B（UNIT内結線図）のDXFファイルをアップロードします"
            "（複数可・フォルダ可。DXF以外のファイルは無視されます）",
            "2. Bの対象範囲（タイトルによる絞り込み）を選びます",
            "3. 「指定領域で比較する」をONにした場合は、「領域を検出」ボタンで矩形領域を検出し、"
            "「領域一覧」（A・Bに共通する領域名）で比較したい領域名にチェックを入れます。"
            "チェックで決定できなかった領域は「領域の確認」で個別に選択します",
            "4. 「展開図-結線図比較」ボタンで比較し、結果をExcelでダウンロードできます",
            "",
            "**Excelファイルの内容：**",
            "- Summaryシート：ファイル数・Bの絞り込み条件・比較件数（指定領域での比較時は領域ごと）",
            "- All Labels diffシート：機器符号候補・ラベル・区分（Aのみ/Bのみ/両方）・A個数・B個数",
            "- 指定領域での比較時は、All Labels diffシートの代わりに領域ごとのシート"
            "（シート名＝領域名。Excelで使えない文字 \\ / * ? : [ ] は _ に置換）",
        ]))


def _render_upload_section(locked):
    st.subheader("DXFファイルのアップロード")
    ver = st.session_state.setdefault('uploader_version', 0)
    col_a, col_b = st.columns(2)
    with col_a:
        files_a = _dxf_only(st.file_uploader(
            "A: 展開接続図（複数可，フォルダ可）", accept_multiple_files=True,
            key=f"uploader_a_{ver}", disabled=locked))
    with col_b:
        files_b = _dxf_only(st.file_uploader(
            "B: UNIT内結線図（複数可，フォルダ可）", accept_multiple_files=True,
            key=f"uploader_b_{ver}", disabled=locked))
        filter_mode = st.radio("Bの対象範囲（タイトルで絞り込み）", FILTER_OPTIONS,
                               key='b_filter_mode', horizontal=True, disabled=locked)
    return files_a, files_b, filter_mode


def _named_files(files_a, files_b):
    """A・B のファイルに一意なファイルキー（'[A] ファイル名' / '[B] ファイル名'）を付ける
    （A・B に同名ファイルがあっても領域選択のセッションキーが衝突しないようにする）。"""
    return ([(A_PREFIX + f.name, f) for f in files_a],
            [(B_PREFIX + f.name, f) for f in files_b])


def _common_region_names(analyses):
    """A・B それぞれの領域名候補の積（ABC順）。片側にしか無い領域名は比較しても全件が
    『A のみ』『B のみ』になるため「領域一覧」に出さない（ユーザー指定）。"""
    a = {k: v for k, v in analyses.items() if k.startswith(A_PREFIX)}
    b = {k: v for k, v in analyses.items() if k.startswith(B_PREFIX)}
    return sorted(set(collect_unique_region_candidate_names(a))
                  & set(collect_unique_region_candidate_names(b)))


def _render_region_section(named_a, named_b, locked):
    """「指定領域で比較する」オプションと、ON時の領域検出・領域一覧・領域の確認。
    戻り値: (region_mode, 比較ボタンを出してよいか)"""
    region_mode = st.checkbox(
        "指定領域で比較する", key='region_mode_enabled', disabled=locked,
        help="ONにすると「領域を検出」ボタン・「領域一覧」「領域の確認」セクションが表示され、"
             "選択した領域名ごとにA・Bのラベルを比較します（図面全体の比較は行いません）。"
             "（検出パラメータは config.py の region_detection_config で設定します）",
    )
    if not region_mode:
        _clear_region_state()
        return False, True

    current_files = sorted(key for key, _f in named_a + named_b)
    if st.session_state.get('cmp_detected_files', current_files) != current_files:
        _clear_region_state()   # 検出後に入力ファイルが変わったら検出結果は無効

    analyses = st.session_state.get(region_selection.ANALYSES_KEY)
    if analyses is None:
        if not locked and st.button("領域を検出", key="detect_regions_btn", type="primary"):
            try:
                with st.spinner('領域を検出中...'):
                    status_placeholder = st.empty()
                    analyses = region_selection.run_region_detection(
                        named_a + named_b, region_selection.build_region_cfg(
                            region_detection_config),
                        status_placeholder, time.time())
                    status_placeholder.empty()
                _clear_region_state()
                _clear_session_keys(keys=_RESULT_KEYS)
                st.session_state[region_selection.ANALYSES_KEY] = analyses
                st.session_state['cmp_detected_files'] = current_files
                st.rerun()
            except Exception as e:
                _handle_error(e)
        return True, False

    common_names = _common_region_names(analyses)
    if not common_names and any(an.get('regions') for an in analyses.values()):
        st.warning("A・Bに共通する領域名がありません。")
        return True, False
    region_selection.render_global_region_names_section(common_names, locked, action='比較')
    if not st.session_state.get(region_selection.CONFIRMED_KEY):
        return True, False
    region_selection.render_region_confirmation_section(analyses, locked, action='比較')
    return True, True


def _run_comparison(named_a, named_b, filter_mode, region_mode):
    frame_lineweight = int(region_selection.build_region_cfg(
        region_detection_config)['frame_lineweight'])
    with st.spinner(f'{len(named_a) + len(named_b)}個のDXFファイルを処理中...'):
        status_placeholder = st.empty()
        start_time = time.time()
        analyses = st.session_state.get(region_selection.ANALYSES_KEY) if region_mode else None
        ref_data = region_selection.run_label_extraction(
            named_a + named_b, frame_lineweight, analyses=analyses,
            status_placeholder=status_placeholder, start_time=start_time)
        if region_mode:
            name_selections = {
                key: region_selection.gather_name_selections(key, analyses[key])
                for key in ref_data
            }
            results = ref_designator.build_ref_designator_region_results(
                ref_data, analyses, name_selections)
        else:
            results = ref_designator.build_ref_designator_final(ref_data)
        status_placeholder.empty()

    a_keys = [key for key, _f in named_a]
    b_all = [key for key, _f in named_b]
    b_keys = select_files({key: results[key].get('title') for key in b_all}, filter_mode)
    header = summary_header(len(a_keys), len(b_all), filter_mode, len(b_keys))

    if region_mode:
        common = set(_common_region_names(analyses))
        regions = sorted(region_selection.global_checked_region_names() & common)
        diff_df, metrics = compare_labels_by_region(
            aggregate_region_labels(results, a_keys, regions),
            aggregate_region_labels(results, b_keys, regions), regions)
        summary = build_region_summary_rows(metrics, header)
        output = create_region_compare_excel_output(diff_df, summary, regions)
        st.session_state['cmp_region_metrics'] = metrics
    else:
        diff_df = compare_labels(aggregate_labels(results, a_keys),
                                 aggregate_labels(results, b_keys))
        summary = summarize(diff_df, header)
        output = create_compare_excel_output(diff_df, summary)
    st.session_state['cmp_diff'] = diff_df
    st.session_state['cmp_summary'] = summary
    st.session_state['cmp_output'] = output
    st.session_state['cmp_is_region_mode'] = region_mode
    st.session_state['cmp_download_done'] = False


def _render_results_section():
    diff_df = st.session_state.get('cmp_diff')
    if diff_df is None:
        return
    summary = st.session_state['cmp_summary']
    st.divider()
    st.subheader("比較結果")
    if st.session_state.get('cmp_is_region_mode'):
        metrics = st.session_state.get('cmp_region_metrics', {})
        totals = {}
        for m in metrics.values():
            for key, value in m.items():
                totals[key] = totals.get(key, 0) + value
        st.info(
            f"対象領域: {len(metrics):,}件　/　"
            f"A のみ: {totals.get('A のみ', 0):,}件　/　B のみ: {totals.get('B のみ', 0):,}件　/　"
            f"両方: {totals.get('両方', 0):,}件"
        )
        st.dataframe(_summary_for_display(summary), width='stretch', hide_index=True)
    else:
        st.info(
            f"A: {summary['A ファイル数']:,}ファイル　/　"
            f"B: {summary['B 対象ファイル数']:,}ファイル（{summary['B 絞り込み条件']}、"
            f"全{summary['B ファイル数']:,}ファイル中）　/　"
            f"A のみ: {summary['A のみ']:,}件　/　B のみ: {summary['B のみ']:,}件　/　"
            f"両方: {summary['両方']:,}件"
        )
        if summary['B 対象ファイル数'] == 0:
            st.warning("Bの対象範囲に該当するファイルがありません（Bの全ラベルが比較対象外）。")
    st.dataframe(
        _for_display(diff_df).style.apply(_row_style_factory(diff_df), axis=1),
        width='stretch', hide_index=True,
        # 値は Y か空欄だけなので、見出し「機器符号候補」がちょうど収まる幅に固定する
        column_config={'機器符号候補': st.column_config.Column(width=100)},
    )

    download_done = st.session_state.get('cmp_download_done', False)
    downloaded = st.download_button(
        label="Excelをダウンロード", data=st.session_state['cmp_output'],
        file_name=OUTPUT_FILENAME,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="secondary" if download_done else "primary", key='cmp_download',
    )
    if downloaded and not download_done:
        st.session_state['cmp_download_done'] = True
        st.rerun()

    if st.button("🔄 新しい比較を開始", key="restart_button",
                 type="primary" if download_done else "secondary"):
        _clear_session_keys(keys=_RESULT_KEYS)
        _clear_region_state()
        st.session_state['uploader_version'] = st.session_state.get('uploader_version', 0) + 1
        st.rerun()


def main():
    """表示制御:
    - 比較結果表示中（locked）: 入力・オプション・領域一覧・領域の確認は表示するが操作不可。
      「新しい比較を開始」でのみ次のサイクルへ進める
    - 指定領域で比較する OFF: 「展開図-結線図比較」ボタンのみ
    - 指定領域で比較する ON: 「領域を検出」→「領域一覧」→「領域選択を完了」→
      「領域の確認」（必要な場合のみ）→「展開図-結線図比較」
    """
    _render_intro()
    locked = st.session_state.get('cmp_diff') is not None

    files_a, files_b, filter_mode = _render_upload_section(locked)
    if not files_a or not files_b:
        st.info("A・B 両方のDXFファイルをアップロードしてください")
        return
    st.success(f"A: {len(files_a)}個 / B: {len(files_b)}個のDXFファイルが選択されました")
    named_a, named_b = _named_files(files_a, files_b)

    st.subheader("オプション")
    region_mode, ready = _render_region_section(named_a, named_b, locked)

    if not locked and ready:
        no_region_selected = region_mode and not region_selection.global_checked_region_names()
        if st.button("展開図-結線図比較", type='primary', disabled=no_region_selected,
                     key='compare_run'):
            try:
                _run_comparison(named_a, named_b, filter_mode, region_mode)
                st.rerun()
            except Exception as e:
                _handle_error(e)

    _render_results_section()


if __name__ == '__main__':
    main()
