"""比較結果の Excel 出力モジュール。

シート構成:
- 通常: Summary → All Labels diff
- 指定領域での比較: Summary → 領域ごとのシート（シート名＝領域名。Excel で使えない
  文字は '_' に置換・31文字に切り詰め・重複時は '(2)' 等を付与。`region_sheet_names()`）

- Summary: 項目・値（指定領域での比較時は 領域名・項目・値）。数値は3桁区切り（#,##0）
- 比較結果シート: 機器符号候補・ラベル・区分・A個数・B個数。表示スタイル区分
  （青=Aのみ／緑=Bのみ／黄=両方だが個数不一致／無色=両方かつ個数一致）ごとに
  行全体を色分けする（`model.compare_labels.row_style()`）
"""
import io
import numbers
import re

import pandas as pd

from model.compare_labels import DIFF_COLUMNS, row_style, ROW_STYLE_COLORS

SUMMARY_SHEET = 'Summary'
ALL_LABELS_SHEET = 'All Labels diff'
NUMBER_FORMAT = '#,##0'
_INVALID_SHEET_CHARS = re.compile(r'[\\/*?:\[\]]')
_MAX_SHEET_NAME = 31


def region_sheet_names(regions) -> dict:
    """{領域名: シート名} を返す（regions の順）。Excel のシート名に使えない文字
    （\\ / * ? : [ ]）は '_' に置換し、31文字に切り詰める。Summary や他の領域と
    （大文字小文字を区別せず）重複する場合は末尾に '(2)'・'(3)'… を付ける。"""
    used = {SUMMARY_SHEET.lower()}
    names = {}
    for region in regions:
        base = _INVALID_SHEET_CHARS.sub('_', str(region)).strip("'") or '_'
        name = base[:_MAX_SHEET_NAME]
        n = 2
        while name.lower() in used:
            suffix = f'({n})'
            name = base[:_MAX_SHEET_NAME - len(suffix)] + suffix
            n += 1
        used.add(name.lower())
        names[region] = name
    return names


def create_compare_excel_output(diff_df: pd.DataFrame, summary: dict) -> bytes:
    """通常の比較結果の Excel ファイルを bytes で返す（Summary は 項目・値 の2列、
    比較結果は 'All Labels diff' シート）。"""
    summary_rows = [{'項目': k, '値': v} for k, v in summary.items()]
    return _create_excel_output(summary_rows, [(ALL_LABELS_SHEET, diff_df)])


def create_region_compare_excel_output(diff_df: pd.DataFrame, summary_rows: list,
                                       regions: list) -> bytes:
    """指定領域での比較結果の Excel ファイルを bytes で返す。

    `diff_df` は `compare_labels_by_region()` の戻り値（『領域名』列付き）。
    `regions` の順に領域ごとのシート（シート名は `region_sheet_names()`、『領域名』列は
    持たない）を作る。該当ラベルが無い領域もヘッダーのみのシートとして出力する。
    Summary シートは領域名・項目・値の3列（`build_region_summary_rows()` の戻り値）。
    """
    names = region_sheet_names(regions)
    sheets = [(names[region], diff_df[diff_df['領域名'] == region]) for region in regions]
    return _create_excel_output(summary_rows, sheets)


def _create_excel_output(summary_rows: list, diff_sheets: list) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        workbook = writer.book

        header_fmt = workbook.add_format({
            'bold': True, 'bg_color': '#4472C4', 'font_color': 'white', 'border': 1,
        })
        number_fmt = workbook.add_format({'num_format': NUMBER_FORMAT})
        row_formats = {
            style_key: workbook.add_format({**(color or {}), 'border': 1})
            for style_key, color in ROW_STYLE_COLORS.items()
        }
        row_number_formats = {
            style_key: workbook.add_format(
                {**(color or {}), 'border': 1, 'num_format': NUMBER_FORMAT})
            for style_key, color in ROW_STYLE_COLORS.items()
        }

        _write_summary_sheet(workbook, writer, summary_rows, header_fmt, number_fmt)
        for sheet_name, df in diff_sheets:
            _write_diff_sheet(workbook, writer, sheet_name, df, header_fmt,
                              row_formats, row_number_formats)

    return output.getvalue()


def _is_number(value) -> bool:
    return isinstance(value, numbers.Number) and not isinstance(value, bool)


def _write_summary_sheet(workbook, writer, summary_rows: list, header_fmt, number_fmt):
    columns = list(summary_rows[0].keys()) if summary_rows else ['項目', '値']
    ws = workbook.add_worksheet(SUMMARY_SHEET)
    writer.sheets[SUMMARY_SHEET] = ws
    for col_idx, col_name in enumerate(columns):
        ws.write(0, col_idx, col_name, header_fmt)
    for row_idx, row in enumerate(summary_rows, start=1):
        for col_idx, col_name in enumerate(columns):
            value = row[col_name]
            if _is_number(value):
                ws.write_number(row_idx, col_idx, value, number_fmt)
            else:
                ws.write(row_idx, col_idx, value)
    if '領域名' in columns:
        ws.set_column(0, 0, 25)
        ws.set_column(1, 1, 22)
        ws.set_column(2, 2, 20)
    else:
        ws.set_column(0, 0, 22)
        ws.set_column(1, 1, 20)
    ws.freeze_panes(1, 0)


def _write_diff_sheet(workbook, writer, sheet_name, diff_df: pd.DataFrame, header_fmt,
                      row_formats, row_number_formats):
    columns = DIFF_COLUMNS
    ws = workbook.add_worksheet(sheet_name)
    writer.sheets[sheet_name] = ws

    for col_idx, col_name in enumerate(columns):
        ws.write(0, col_idx, col_name, header_fmt)

    kubun_idx = columns.index('区分')
    a_idx = columns.index('A個数')
    b_idx = columns.index('B個数')

    for row_idx, row in enumerate(diff_df[columns].itertuples(index=False), start=1):
        values = list(row)
        style = row_style(values[kubun_idx], values[a_idx], values[b_idx])
        fmt = row_formats[style]
        for col_idx, value in enumerate(values):
            if col_idx in (a_idx, b_idx):
                if pd.notna(value):
                    ws.write_number(row_idx, col_idx, int(value), row_number_formats[style])
                else:
                    ws.write_blank(row_idx, col_idx, None, fmt)
            elif value is None or (not isinstance(value, str) and pd.isna(value)):
                ws.write_blank(row_idx, col_idx, None, fmt)
            else:
                ws.write(row_idx, col_idx, value, fmt)

    widths = {'機器符号候補': 12, 'ラベル': 30, '区分': 12, 'A個数': 10, 'B個数': 10}
    for col_idx, col_name in enumerate(columns):
        ws.set_column(col_idx, col_idx, widths[col_name])
    ws.freeze_panes(1, 0)
    last_row = len(diff_df)
    if last_row > 0:
        ws.autofilter(0, 0, last_row, len(columns) - 1)
