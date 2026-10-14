"""機器符号一覧（designator master）生成・索引モジュール

原本 Excel（手作業で作成された ENTRON-EX2_NFB_LIST.xls 等、BIFF形式の旧 .xls）
から「分類（シート名）/ 機器符号 / 用途」の1行1符号のマスターリストを作り、
機器符号判断パイプライン（`ref_designator.designator_mark()`）が参照する
HMACダイジェスト索引を提供する（v3.10.0）。

**公開リポジトリへは平文を置かない**: 符号・用途・分類はリポジトリ外
（`data/designator_master.xlsx`、`.gitignore`対象）にのみ存在する。パイプラインが
参照する `assets/designator_digests.txt`（git管理対象）には HMAC-SHA256
ダイジェストのみを記録し、鍵（`DESIGNATOR_MASTER_KEY`）を持たない限り
符号文字列を復元できない（符号空間が約91,000通りと小さいため、鍵無しの
単純ハッシュでは秘匿にならない。2026-10-08実測・ユーザー確認）。

**原本のブレ／揺れへの対応**（2026-10実測。詳細な件数は
`docs/REF_DESIGNATOR.md`「機器符号一覧」節を参照）:

- 符号セルが LF 区切りで複数行 → 行ごとに分離。用途が同数行なら1対1対応、
  そうでなければ用途を連結して全符号に割り当てる（`_assign_uses()`）
- `F006C,D,E` → F006C, F006D, F006E（`,` 区切り。先頭要素の英字サフィックスを
  後続要素にも適用）
- `CB361A/B` → CB361A, CB361B（`/` 区切り。`,` と同じ扱い）
- `F008A～K` → F008A〜F008K（`~` 範囲。終端要素の**末尾の英字1文字のみ**を
  範囲の終端として使う。`F004A～4I` のような終端が数字付きの変則形にも対応）
- 符号トークン内の空白は英字/数字/記号の位置を問わず一律除去する
  （`S 163`→`S163` だけでなく `ELB(CB) 251`→`ELB(CB)251` も扱うため。
  「英文字と数字の間」という原文の規則より広く取っている）
- 符号列のみ NFKC 正規化（全角→半角）。用途列は原本のまま保持する
- 取り消し線（セル全体 or リッチテキストの一部範囲）がある符号行は出力しない
- S系符号（`S`+数字）のうち、用途が空、または「F1用」「B2」のような
  枠取り表記（`^[FRB][0-9]用?$`）のものは「予備位置」として除外する
  （S系以外には適用しない。用途が実機記述のS系符号は残す）

**照合キーの正規化**（パイプラインでの DEF/CAN 判定に使う。v3.10.0新設）:

- 一覧側（`master_code_key()`）: NFKC正規化・空白除去に加え、`ELB(CB)001`の
  ような「本来の符号(別称)」形から別称部分を除去し`ELB001`とする
  （2026-10-08ユーザー決定。原本には全186件が`ELB(CB)nnn`形のみ存在し、
  実データのDXFラベル側には`ELB(CB)001`という原文表記は出現しないため、
  除去しないと一覧の約31%が照合に使われないまま死蔵する）
- ラベル側（`label_match_key()`）: `label_classifier.normalize_label()`
  → `_judgment_text()` の順で判定用文字列を取り出した**後に**空白を除去する。
  **この順序を逆にしてはいけない** — `_judgment_text()` はΩ記号の直前の
  空白をデリミタとして使うため（`'R0 2.2KΩ'` → `'R0'`）、先に空白を除去すると
  デリミタ自体が消えてΩ区切りが機能しなくなる

このモジュールは streamlit に依存しない（CLI・パイプラインの両方から
呼べるようにするため）。xlrd は旧BIFF形式の読み取りにのみ必要。
"""
import hashlib
import hmac
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from .label_classifier import normalize_label, _judgment_text


# ---------------------------------------------------------------------------
# 符号トークンの分解（`,` `/` `~` 展開・空白除去・NFKC正規化）
# ---------------------------------------------------------------------------

_COMMA_SLASH_RE = re.compile(r'[,/]')
_TILDE_CHARS = ('~', '〜')  # ASCII tilde（NFKC後）・WAVE DASH（NFKC非変換のため直接検出）
_WHITESPACE_RE = re.compile(r'\s+')


def expand_code_cell(token):
    """符号1個のトークンを、必要に応じて一連の符号へ分解する。

    NFKC正規化（全角→半角）・空白の一律除去を行った上で、`,`・`/`・`~` の
    区切りを検出し分解する。区切りが無ければ正規化後のトークンを1件のまま返す。

    戻り値: ``(符号リスト, 警告メッセージ or None)``。
    分解できない・想定外の形の場合は ``([正規化後のトークン], 警告文)`` を返す
    （落とさずそのまま1件として扱う。実データでは発生しないが、将来原本が
    更新された場合に欠落が黙って起きないようにするため）。
    """
    s = unicodedata.normalize('NFKC', token)
    s = _WHITESPACE_RE.sub('', s)
    if not s:
        return [], None

    has_tilde = any(ch in s for ch in _TILDE_CHARS)
    has_comma_slash = bool(_COMMA_SLASH_RE.search(s))

    if has_tilde and has_comma_slash:
        return [s], f'`~`と`,`/`/`が同時に含まれる未対応の形: {s!r}'

    if has_tilde:
        return _expand_tilde_range(s)

    if has_comma_slash:
        return _expand_comma_slash_list(s)

    return [s], None


def _expand_tilde_range(s):
    """`F008A~K` / `F004A~4I` のような `~` 範囲を展開する。

    終端側（`~` より後）は**末尾の英字1文字のみ**を範囲の終端として使う
    （`F004A~4I` の `4I` は末尾の `I` だけを使う。備考欄
    `R,S,T004A,4B,...,4I` の記法と整合する。2026-10-08ユーザー承認）。
    """
    head = tail = None
    for ch in _TILDE_CHARS:
        if ch in s:
            head, _, tail = s.partition(ch)
            break

    m_head = re.match(r'^(.*?)([A-Za-z])$', head)
    m_tail_letter = re.search(r'([A-Za-z])$', tail)
    if not m_head or not m_tail_letter:
        return [s], f'`~`範囲の形式が不明: {s!r}'

    base, start_letter = m_head.groups()
    end_letter = m_tail_letter.group(1)
    start_ord, end_ord = ord(start_letter.upper()), ord(end_letter.upper())
    if start_ord > end_ord:
        return [s], f'`~`範囲が逆順: {s!r}'

    return [f'{base}{chr(c)}' for c in range(start_ord, end_ord + 1)], None


def _expand_comma_slash_list(s):
    """`F006C,D,E` / `CB361A/B` のような `,`・`/` 区切りを展開する。

    先頭要素の末尾の英字サフィックス（`F006C` の `C`）を取り除いた部分を
    base とし、後続の各要素（`D`・`E` 等）に base を前置して復元する。
    """
    parts = _COMMA_SLASH_RE.split(s)
    first = parts[0]
    m = re.match(r'^(.*?)([A-Za-z]+)$', first)
    if not m:
        return [s], f'`,`/`/`区切りの先頭要素に英字サフィックスが見つからない: {s!r}'

    base = m.group(1)
    result = [first]
    for p in parts[1:]:
        if not re.fullmatch(r'[A-Za-z0-9]+', p):
            return [s], f'`,`/`/`区切りの要素が想定外の形式: {s!r}'
        result.append(f'{base}{p}')
    return result, None


# ---------------------------------------------------------------------------
# 用途の割り当て（符号セル・用途セルの行数の組み合わせ）
# ---------------------------------------------------------------------------

def assign_uses(code_lines, use_lines):
    """符号の行リストと用途の行リストから、各符号に割り当てる用途のリストを返す。

    組み合わせ（`docs/REF_DESIGNATOR.md`「機器符号一覧」の表を参照）:

    - 符号1行・用途0/1行 → そのまま（用途0行は空文字列）
    - 符号1行・用途2行以上 → 用途を半角スペースで連結して1件にする
      （折り返しの continuation であり別の用途ではない、2026-10-08ユーザー承認）
    - 符号複数行・用途0行 → 全符号に空の用途
    - 符号複数行・用途1行 → 全符号に同じ用途
    - 符号複数行・用途が符号と同数 → 行ごとに1対1対応
    - 符号複数行・用途が複数だが符号と不一致 → 用途を連結して全符号に割り当て、
      警告を返す（実データでは発生しないフォールバック経路）

    戻り値: ``(符号と同数の用途リスト, 警告メッセージ or None)``。
    """
    n = len(code_lines)
    use_lines = [u.strip() for u in use_lines if u.strip()]

    if n == 1:
        if len(use_lines) <= 1:
            return [use_lines[0] if use_lines else ''], None
        return [' '.join(use_lines)], None

    if not use_lines:
        return [''] * n, None
    if len(use_lines) == 1:
        return [use_lines[0]] * n, None
    if len(use_lines) == n:
        return list(use_lines), None

    combined = ' '.join(use_lines)
    warning = f'符号{n}行・用途{len(use_lines)}行で行数が不一致のため用途を連結: {combined!r}'
    return [combined] * n, warning


# ---------------------------------------------------------------------------
# 取り消し線（strikethrough）判定 — xlrd 固有の型に依存しない純粋関数
# ---------------------------------------------------------------------------

def split_struck_lines(text, struck_whole, struck_ranges=()):
    """``text`` を ``\\n`` で分割し、各行について取り消し線の有無を判定する。

    ``struck_whole`` が真ならセル全体が取り消し線（全行が取り消し線扱い）。
    ``struck_ranges`` は ``text`` 全体に対する文字オフセット範囲
    ``(start, end)``（半開区間）のリストで、範囲が重なる行を取り消し線扱いにする
    （リッチテキストの一部範囲だけが取り消し線のケース。実データでは未発生だが、
    合成データで検証する）。

    戻り値: ``[(行文字列, 取り消し線フラグ), ...]``。
    """
    lines = text.split('\n')
    if struck_whole:
        return [(ln, True) for ln in lines]

    result = []
    pos = 0
    for ln in lines:
        start, end = pos, pos + len(ln)
        struck = any(not (end <= r0 or start >= r1) for (r0, r1) in struck_ranges)
        result.append((ln, struck))
        pos = end + 1  # +1 は '\n' の分
    return result


# ---------------------------------------------------------------------------
# S系（予備位置）除外判定
# ---------------------------------------------------------------------------

_SPARE_CODE_PATTERN = re.compile(r'^S\d+$')
_SPARE_FRAME_USE_PATTERN = re.compile(r'^[FRB][0-9]用?$')


def is_spare_entry(code, use):
    """S系符号（``S``+数字）のうち、用途が空、または「F1用」「B2」のような
    枠取り表記のものを「予備位置」として除外対象と判定する。

    S系以外の符号には適用しない（例: 用途が空の `F023` は除外しない）。
    """
    if not _SPARE_CODE_PATTERN.match(code):
        return False
    use = (use or '').strip()
    if not use:
        return True
    return bool(_SPARE_FRAME_USE_PATTERN.match(use))


def spare_exclusion_reason(use):
    """`is_spare_entry()` が True を返したときの除外理由文字列を返す。"""
    use = (use or '').strip()
    return 'S系・用途空' if not use else 'S系・枠取り'


# ---------------------------------------------------------------------------
# マスターリスト構築（xlrd を使った原本読み取り）
# ---------------------------------------------------------------------------

@dataclass
class MasterRow:
    category: str
    code: str
    use: str
    sheet_row: int  # 原本シート上の行番号（1始まり）


@dataclass
class ExcludedRow:
    category: str
    sheet_row: int
    raw_code_cell: str
    code: str
    use: str
    reason: str  # '取り消し線' / 'S系・用途空' / 'S系・枠取り'


@dataclass
class WarningRow:
    category: str
    sheet_row: int
    raw_code_cell: str
    raw_use_cell: str
    message: str


@dataclass
class MasterResult:
    rows: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _cell_text(value):
    """xlrd のセル値を文字列へ正規化する（空セルは ''）。"""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if value == '':
        return ''
    return str(value)


def _cell_whole_struck(workbook, sheet, row, col):
    xf = workbook.xf_list[sheet.cell_xf_index(row, col)]
    return bool(workbook.font_list[xf.font_index].struck_out)


def _cell_struck_ranges(workbook, sheet, row, col):
    """リッチテキストの run のうち struck_out な範囲を (start, end) のリストで返す。

    xlrd の run リストは [(オフセット, フォントindex), ...] の形で、各runは
    「そのオフセットから次のrun（または文字列末尾）まで」そのフォントが適用される。
    """
    runs = sheet.rich_text_runlist_map.get((row, col))
    if not runs:
        return []
    text_len = len(_cell_text(sheet.cell_value(row, col)))
    ranges = []
    for i, (offset, font_idx) in enumerate(runs):
        end = runs[i + 1][0] if i + 1 < len(runs) else text_len
        if workbook.font_list[font_idx].struck_out:
            ranges.append((offset, end))
    return ranges


def build_designator_master(xls_path):
    """原本 Excel から機器符号マスターリストを構築する。

    各シートを分類とし、r2行目以降（r0=シートタイトル・r1=ヘッダ）の
    「符号」列（0列目）・「用途」列（1列目）を読む。「備考」列（2列目）は
    今回の出力対象外のため読まない。
    """
    import xlrd

    wb = xlrd.open_workbook(xls_path, formatting_info=True)

    rows = []
    excluded = []
    warnings = []
    per_category = {}
    struck_whole_count = 0
    struck_partial_count = 0

    for sheet in wb.sheets():
        category = sheet.name
        data_rows = 0

        for r in range(2, sheet.nrows):
            raw_code_cell = _cell_text(sheet.cell_value(r, 0)).strip()
            raw_use_cell = _cell_text(sheet.cell_value(r, 1)).strip() if sheet.ncols > 1 else ''
            if not raw_code_cell:
                continue
            data_rows += 1
            sheet_row = r + 1  # 1始まりの行番号（表示用）

            whole_struck = _cell_whole_struck(wb, sheet, r, 0)
            struck_ranges = _cell_struck_ranges(wb, sheet, r, 0)
            code_line_structs = split_struck_lines(raw_code_cell, whole_struck, struck_ranges)
            use_lines_all = raw_use_cell.split('\n') if raw_use_cell else []

            kept_code_lines = []
            for i, (line, struck) in enumerate(code_line_structs):
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                if struck:
                    if whole_struck:
                        struck_whole_count += 1
                    else:
                        struck_partial_count += 1
                    use_for_log = use_lines_all[i].strip() if i < len(use_lines_all) else ''
                    excluded.append(ExcludedRow(
                        category, sheet_row, raw_code_cell, line_stripped, use_for_log, '取り消し線'))
                    continue
                kept_code_lines.append(line_stripped)

            if not kept_code_lines:
                continue

            use_assignments, use_warning = assign_uses(kept_code_lines, use_lines_all)
            if use_warning:
                warnings.append(WarningRow(category, sheet_row, raw_code_cell, raw_use_cell, use_warning))

            for code_line, use in zip(kept_code_lines, use_assignments):
                codes, expand_warning = expand_code_cell(code_line)
                if expand_warning:
                    warnings.append(WarningRow(category, sheet_row, raw_code_cell, raw_use_cell, expand_warning))
                for code in codes:
                    if is_spare_entry(code, use):
                        excluded.append(ExcludedRow(
                            category, sheet_row, code_line, code, use, spare_exclusion_reason(use)))
                        continue
                    rows.append(MasterRow(category, code, use, sheet_row))

        per_category[category] = {
            'data_rows': data_rows,
            'final_count': sum(1 for row in rows if row.category == category),
            'excluded_count': sum(1 for e in excluded if e.category == category),
        }

    spare_empty = sum(1 for e in excluded if e.reason == 'S系・用途空')
    spare_frame = sum(1 for e in excluded if e.reason == 'S系・枠取り')
    stats = {
        'sheet_count': len(wb.sheets()),
        'struck_whole_excluded': struck_whole_count,
        'struck_partial_excluded': struck_partial_count,
        'raw_expanded_count': len(rows) + spare_empty + spare_frame,
        'spare_excluded_empty': spare_empty,
        'spare_excluded_frame': spare_frame,
        'spare_excluded_total': spare_empty + spare_frame,
        'final_count': len(rows),
        'warning_count': len(warnings),
        'per_category': per_category,
    }

    return MasterResult(rows=rows, excluded=excluded, warnings=warnings, stats=stats)


# ---------------------------------------------------------------------------
# 出力（Excel）— CSVはExcelで内容を確認できるため生成しない（2026-10-08）
# ---------------------------------------------------------------------------

def write_excel(result, path):
    """機器符号一覧を Excel に出力する（Summary / 機器符号一覧 / 除外 / 未処理 の4シート）。

    `未処理` シートは警告が0件でも必ず作成する（0件なら「0件」の1行を置く。
    欠落が黙って隠れないようにするため）。
    """
    import pandas as pd

    with pd.ExcelWriter(path, engine='xlsxwriter') as writer:
        workbook = writer.book
        header_format = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3', 'border': 1})

        _write_summary_sheet(writer, result, header_format)
        _write_master_list_sheet(writer, result, header_format)
        _write_excluded_sheet(writer, result, header_format)
        _write_unprocessed_sheet(writer, result, header_format)


def _write_header(ws, columns, header_format):
    for col_num, value in enumerate(columns):
        ws.write(0, col_num, value, header_format)
    ws.freeze_panes(1, 0)


def _write_summary_sheet(writer, result, header_format):
    import pandas as pd

    stats = result.stats
    summary_rows = []
    for category, counts in sorted(stats.get('per_category', {}).items()):
        summary_rows.append([
            category, counts['data_rows'], counts['final_count'], counts['excluded_count'],
        ])
    summary_rows.append(['合計', stats.get('sheet_count', 0),
                          stats.get('final_count', 0),
                          stats.get('spare_excluded_total', 0) + stats.get('struck_whole_excluded', 0)
                          + stats.get('struck_partial_excluded', 0)])

    columns = ('分類', '原本データ行数', '機器符号数', '除外数')
    df = pd.DataFrame(summary_rows, columns=list(columns))
    df.to_excel(writer, sheet_name='Summary', index=False)
    ws = writer.sheets['Summary']
    _write_header(ws, columns, header_format)
    ws.set_column('A:A', 24)
    ws.set_column('B:D', 14)


def _write_master_list_sheet(writer, result, header_format):
    import pandas as pd

    columns = ('分類', '機器符号', '用途')
    df = pd.DataFrame([(r.category, r.code, r.use) for r in result.rows], columns=list(columns))
    df.to_excel(writer, sheet_name='機器符号一覧', index=False)
    ws = writer.sheets['機器符号一覧']
    _write_header(ws, columns, header_format)
    ws.set_column('A:A', 22)
    ws.set_column('B:B', 16)
    ws.set_column('C:C', 50)
    ws.autofilter(0, 0, len(df), len(columns) - 1)


def _write_excluded_sheet(writer, result, header_format):
    import pandas as pd

    columns = ('分類', '行番号', '原文の符号セル', '機器符号', '用途', '除外理由')
    data = [(e.category, e.sheet_row, e.raw_code_cell, e.code, e.use, e.reason) for e in result.excluded]
    df = pd.DataFrame(data, columns=list(columns))
    df.to_excel(writer, sheet_name='除外', index=False)
    ws = writer.sheets['除外']
    _write_header(ws, columns, header_format)
    ws.set_column('A:A', 22)
    ws.set_column('B:B', 10)
    ws.set_column('C:C', 30)
    ws.set_column('D:D', 16)
    ws.set_column('E:E', 40)
    ws.set_column('F:F', 14)
    ws.autofilter(0, 0, len(df), len(columns) - 1)


def _write_unprocessed_sheet(writer, result, header_format):
    import pandas as pd

    columns = ('分類', '行番号', '原文の符号セル', '原文の用途セル', '警告内容')
    if result.warnings:
        data = [(w.category, w.sheet_row, w.raw_code_cell, w.raw_use_cell, w.message) for w in result.warnings]
    else:
        data = [('(0件)', '', '', '', '')]
    df = pd.DataFrame(data, columns=list(columns))
    df.to_excel(writer, sheet_name='未処理', index=False)
    ws = writer.sheets['未処理']
    _write_header(ws, columns, header_format)
    ws.set_column('A:A', 22)
    ws.set_column('B:B', 10)
    ws.set_column('C:C', 30)
    ws.set_column('D:D', 30)
    ws.set_column('E:E', 50)


# ---------------------------------------------------------------------------
# 照合キーの正規化（一覧側・ラベル側）
# ---------------------------------------------------------------------------

_PAREN_INFIX_RE = re.compile(r'^([A-Z]+)\(([A-Z]+)\)([0-9A-Za-z]*)$')


def master_code_key(code):
    """一覧側の符号を照合キーに正規化する。

    NFKC正規化・空白除去に加え、`ELB(CB)001`のような「符号(別称)nnn」形から
    別称部分`(CB)`を除去し`ELB001`とする（2026-10-08ユーザー決定、モジュール
    docstring参照）。この形以外は空白除去のみで変化しない。
    """
    s = unicodedata.normalize('NFKC', code)
    s = _WHITESPACE_RE.sub('', s)
    m = _PAREN_INFIX_RE.match(s)
    if m:
        return f'{m.group(1)}{m.group(3)}'
    return s


def label_match_key(label):
    """DXFから抽出したラベルを照合キーに正規化する。

    `normalize_label()`（NFKC+前後空白除去）→ `_judgment_text()`（括弧等の
    デリミタより前を判定用文字列として取り出す）の順に適用した**後に**空白を
    全除去する。この順序は固定——モジュール docstring の「照合キーの正規化」
    節を参照（Ω区切りの判定が空白の位置に依存するため、先に空白を除去すると
    壊れる）。
    """
    normalized = normalize_label(label)
    judgment = _judgment_text(normalized)
    return _WHITESPACE_RE.sub('', judgment)


# ---------------------------------------------------------------------------
# HMACダイジェスト（鍵付きハッシュ）— 一覧の平文を公開リポジトリに置かないため
# ---------------------------------------------------------------------------

# 鍵フィンガープリント算出用の固定プローブ文字列（符号ではない、ダイジェスト
# ファイルとは無関係の値を鍵で叩くだけ）。この文字列自体に意味はない。
_FINGERPRINT_PROBE = 'designator-master-key-fingerprint-probe'
_DIGEST_HEADER_PREFIX = '#'
_FINGERPRINT_LINE_PREFIX = '# key_fingerprint:'


def code_digest(key, secret):
    """照合キーを HMAC-SHA256 でダイジェスト化する（hex 64文字）。

    符号の総当たり空間は約91,000通りしかなく（2026-10-08実測、モジュール
    docstring参照）、鍵無しの単純ハッシュ（素のSHA-256等）では公開した時点で
    全列挙により復元できてしまう。鍵付き（HMAC）であることが必須。
    """
    return hmac.new(secret.encode('utf-8'), key.encode('utf-8'), hashlib.sha256).hexdigest()


def key_fingerprint(secret):
    """鍵の検査用フィンガープリント（先頭16桁）。鍵そのものではない。

    ダイジェストファイル生成時の鍵と実行時の鍵が異なると、DEF判定が
    1件も該当しないまま正常終了してしまう（黙った機能不全）。この値を
    ダイジェストファイルに記録しておき、実行時に再計算した値と比較することで
    「鍵が異なる」ことを検出できるようにする（鍵自体は復元できないので
    記録しても安全）。
    """
    return hmac.new(
        secret.encode('utf-8'), _FINGERPRINT_PROBE.encode('utf-8'), hashlib.sha256
    ).hexdigest()[:16]


def build_digest_lines(result, secret):
    """ダイジェストファイルの全行（ヘッダーコメント＋ソート済みダイジェスト）を返す。

    一覧側の正規化は `master_code_key()` で行う（`ELB(CB)005`→`ELB005`等の
    重複はここで自然に1件へ統合される）。
    """
    keys = sorted({master_code_key(r.code) for r in result.rows})
    digests = sorted(code_digest(k, secret) for k in keys)
    lines = [
        f'{_DIGEST_HEADER_PREFIX} designator master digest',
        f'{_DIGEST_HEADER_PREFIX} generated: {date.today().isoformat()}',
        f'{_DIGEST_HEADER_PREFIX} count: {len(digests)}',
        f'{_FINGERPRINT_LINE_PREFIX} {key_fingerprint(secret)}',
    ]
    lines.extend(digests)
    return lines


def write_digest_file(result, path, secret):
    """`build_digest_lines()` の内容をファイルへ書く（鍵そのものは書かない）。"""
    lines = build_digest_lines(result, secret)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


class DesignatorMasterIndex:
    """ロード済みの機器符号ダイジェスト索引。

    ダイジェスト（HMAC値）のみを保持し、平文の符号は一切保持しない。
    `contains()` は毎回 `label_match_key()` → `code_digest()` を計算して
    集合に対する所属判定を行う。
    """

    def __init__(self, digests, fingerprint, secret):
        self._digests = frozenset(digests)
        self.fingerprint = fingerprint
        self._secret = secret

    def __len__(self):
        return len(self._digests)

    def contains(self, label):
        """DXFラベル文字列が機器符号一覧に含まれるかどうかを判定する。"""
        key = label_match_key(label)
        if not key:
            return False
        return code_digest(key, self._secret) in self._digests


def load_index(path, secret):
    """ダイジェストファイルを読み、`DesignatorMasterIndex` を返す。

    次のいずれかに該当する場合は `None` を返す（2026-10-08ユーザー決定:
    鍵が使えない場合は従来動作〈CANのみ〉で継続し、呼び出し側が画面に
    注記を出す）:

    - `secret` が空（鍵未設定）
    - `path` のファイルが存在しない
    - ファイル中の鍵フィンガープリントが、現在の鍵から計算した値と一致しない
      （鍵がダイジェスト生成時と異なる＝鍵不一致）
    """
    if not secret:
        return None
    if not os.path.isfile(path):
        return None

    with open(path, encoding='utf-8') as f:
        raw_lines = [ln.strip() for ln in f if ln.strip()]

    stored_fingerprint = None
    digests = []
    for ln in raw_lines:
        if ln.startswith(_FINGERPRINT_LINE_PREFIX):
            stored_fingerprint = ln[len(_FINGERPRINT_LINE_PREFIX):].strip()
            continue
        if ln.startswith(_DIGEST_HEADER_PREFIX):
            continue
        digests.append(ln)

    if stored_fingerprint is None:
        return None
    if key_fingerprint(secret) != stored_fingerprint:
        return None

    return DesignatorMasterIndex(digests, stored_fingerprint, secret)


def resolve_secret(streamlit_secrets_path=None):
    """鍵（`DESIGNATOR_MASTER_KEY`）を解決する（CLI・streamlit非依存の経路用）。

    解決順: 環境変数 `DESIGNATOR_MASTER_KEY` → `.streamlit/secrets.toml`
    （`tomllib`、Python 3.11+ 標準ライブラリで直接パースする。streamlit
    自体への依存は作らない）。どちらにも無ければ `None` を返す。

    鍵の生成・表示は行わない（値を標準出力・ログに出す呼び出し元を作らない
    こと。2026-10-08、ユーザーへの引き継ぎ事項）。
    """
    key = os.environ.get('DESIGNATOR_MASTER_KEY')
    if key:
        return key

    if streamlit_secrets_path is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        streamlit_secrets_path = os.path.join(project_root, '.streamlit', 'secrets.toml')

    if not os.path.isfile(streamlit_secrets_path):
        return None

    try:
        import tomllib
    except ImportError:
        return None

    with open(streamlit_secrets_path, 'rb') as f:
        data = tomllib.load(f)
    value = data.get('DESIGNATOR_MASTER_KEY')
    return value if isinstance(value, str) and value else None
