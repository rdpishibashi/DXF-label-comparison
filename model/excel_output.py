"""比較結果の Excel 出力モジュール。

シート構成: Summary → 比較結果。
- Summary: 項目・値（指定領域での比較時は 領域名・項目・値）。数値は3桁区切り（#,##0）
- 比較結果: [領域名]・機器符号候補・ラベル・区分・A個数・B個数。表示スタイル区分
  （青=Aのみ／緑=Bのみ／黄=両方だが個数不一致／無色=両方かつ個数一致）ごとに
  行全体を色分けする（`model.compare_labels.row_style()`）
"""
import io
import numbers

import pandas as pd

from model.compare_labels import (
    DIFF_COLUMNS, REGION_DIFF_COLUMNS, blank_repeated_column, row_style, ROW_STYLE_COLORS,
)

SUMMARY_SHEET = 'Summary'
DIFF_SHEET = '比較結果'
NUMBER_FORMAT = '#,##0'


def create_compare_excel_output(diff_df: pd.DataFrame, summary: dict) -> bytes:
    """通常の比較結果の Excel ファイルを bytes で返す（Summary は 項目・値 の2列）。"""
    summary_rows = [{'項目': k, '値': v} for k, v in summary.items()]
    return _create_excel_output(diff_df, summary_rows, DIFF_COLUMNS)


def create_region_compare_excel_output(diff_df: pd.DataFrame, summary_rows: list) -> bytes:
    """指定領域での比較結果の Excel ファイルを bytes で返す。

    比較結果シートの先頭に『領域名』列（`REGION_DIFF_COLUMNS`）を持ち、Summary シートは
    領域名・項目・値の3列（`build_region_summary_rows()` の戻り値をそのまま渡す）。
    """
    return _create_excel_output(diff_df, summary_rows, REGION_DIFF_COLUMNS)


def _create_excel_output(diff_df: pd.DataFrame, summary_rows: list, diff_columns: list) -> bytes:
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
        _write_diff_sheet(workbook, writer, diff_df, diff_columns, header_fmt,
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


def _write_diff_sheet(workbook, writer, diff_df: pd.DataFrame, columns: list, header_fmt,
                      row_formats, row_number_formats):
    ws = workbook.add_worksheet(DIFF_SHEET)
    writer.sheets[DIFF_SHEET] = ws

    if '領域名' in columns:
        # 同じ領域名が連続する行では2行目以降を空欄にする（Summary シートと
        # 同じ「見出し1回＋空欄」レイアウト、ユーザー指定）
        diff_df = blank_repeated_column(diff_df, '領域名')

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

    widths = {'領域名': 25, '機器符号候補': 12, 'ラベル': 30, '区分': 12, 'A個数': 10, 'B個数': 10}
    for col_idx, col_name in enumerate(columns):
        ws.set_column(col_idx, col_idx, widths[col_name])
    ws.freeze_panes(1, 0)
    last_row = len(diff_df)
    if last_row > 0:
        ws.autofilter(0, 0, last_row, len(columns) - 1)
