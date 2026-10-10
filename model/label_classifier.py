"""ラベルの3分類（機器符号候補 / 領域名候補 / どちらでもない）を判定する
純粋な文字列判定モジュール。

`classify_label()` が唯一の判定関数で、次の2箇所が同じ関数を使う:
  - `ref_designator.py`（DXF抽出パイプライン）: Excelの「機器符号候補」列
    （`is_ref_designator_label()` = 分類が DESIGNATOR）
  - `region_detector.py`（矩形領域検出）: 領域名候補のフィルタ
    （分類が REGION_NAME のものだけを領域名候補にする）
ezdxf・region_detector.py への依存を一切持たない自己完結モジュールのため、
どちらからもモジュールレベルで安全に import できる（循環依存なし）。

旧名 `ref_designator_patterns.py`。2026-10-10、3分類の一元化に伴い改名し、
旧方式の判定（`classify_judgment_detailed`・確定パターン・追加除外パターン等）を
削除した。判定順と各語の分類は `classify_label()` の docstring を参照。

reference_designator_candidates.xlsx（`Patterns` / `ExclusionPatterns` シート）と
reference_deginator_pattern_added.txt（追加機器符号）を元にパターンを実装する。
"""
import re
import unicodedata
from typing import Optional


# ============================================================
# 1. Reference Designator パターン（Patterns シートが正）
# ============================================================

# (カテゴリ名, 正規表現, 説明) — reference_designator_candidates.xlsx の
# Patterns シートに由来する3カテゴリに、2026-09-10 ユーザー指示で3カテゴリ、
# 2026-09-11 ユーザー指示でさらに1カテゴリを追加した計7カテゴリ。
# CANDIDATE_PATTERN はこれらの OR で導出する（tools/reference_designator_
# analyzer.py 等、外部ツールが個別カテゴリ名を参照できるよう名前付きで公開する）。
#
# 2026-09-10 に追加した3カテゴリ（letters_digits_letters/
# letters_digits_letters_digits/letters_digits_hyphen_alnum2）は、既存の
# hyphen_letters_digits_any / letters_digits_any に文字列集合として完全に
# 包含される（長さ6以下の総当たりで確認済み）。そのためこれらを追加しても
# CANDIDATE_PATTERN が受理する文字列集合自体は変化しない——変わるのは
# `matched_pattern_name()` がより具体的なカテゴリ名を返すようになる点のみ。
# より限定的な（包含される側の）パターンを先に判定させるため、包含する
# 既存パターンより前に配置する。
#
# 2026-09-11 に追加した w_no_prefix は他カテゴリと異なり**前方一致**
# （末尾に `.*` を持つため、`^(?:...)$` の全体アンカー内で「'W No.' で始まれば
# 残りは何でもよい」という意味になる）。`_judgment_text()`（括弧/` **`より前）
# が既に切り出した文字列の形（英大文字+数字+記号のみ）を前提とする他カテゴリと
# 異なり、`W No.M06004` のように大文字小文字混在・ピリオドを含む実データの
# 表記をそのまま候補と認めるためのユーザー指定の例外。
PATTERN_CATEGORIES = [
    ('hyphen_letters_digits_any', re.compile(r'^[A-Z]+-[A-Z]+[0-9]+[A-Z0-9-]*$'),
     '英字繰返し-英字繰返し + 数字繰返し + 英数字/ハイフン任意(0可)'),
    ('letters_digits_letters', re.compile(r'^[A-Z]+[0-9]+[A-Z]+$'),
     '英字繰返し + 数字繰返し + 英字繰返し'),
    ('letters_digits_letters_digits', re.compile(r'^[A-Z]+[0-9]+[A-Z]+[0-9]+$'),
     '英字繰返し + 数字繰返し + 英字繰返し + 数字繰返し'),
    ('letters_digits_hyphen_alnum2', re.compile(r'^[A-Z]+[0-9]+-[A-Z0-9]+-[A-Z0-9]+$'),
     '英字繰返し + 数字繰返し - 英数字繰返し - 英数字繰返し'),
    ('letters_digits_any', re.compile(r'^[A-Z]+[0-9]+[A-Z0-9-]*$'),
     '英字繰返し + 数字繰返し + 英数字/ハイフン任意(0可)'),
    ('letters_only', re.compile(r'^[A-Z]+$'),
     '英字繰返しのみ'),
    ('w_no_prefix', re.compile(r'^W No\..*$'),
     '"W No." で始まる（前方一致。ケーブル/ワイヤ番号表記）'),
]
_PATTERN_CORE = '|'.join(rx.pattern[1:-1] for _n, rx, _d in PATTERN_CATEGORIES)
CANDIDATE_PATTERN = re.compile(r'^(?:%s)$' % _PATTERN_CORE)


def matched_pattern_name(judgment: str) -> Optional[str]:
    """判定用文字列（括弧より前）がどの候補パターンに一致したかを返す
    （一致しなければ None）。"""
    for name, rx, _desc in PATTERN_CATEGORIES:
        if rx.match(judgment):
            return name
    return None


# ============================================================
# 2. 除外パターン（ExclusionPatterns シートが正、2026-07-10 確定。
#    circuit_description の「+数字1桁許容」は 2026-07-10 追加確定。
#    wiring_digit_run（数字4桁以上連続）は 2026-07-11 追加確定）
# ============================================================

_COMMON_NOUNS = {
    'ABORT', 'ACCESSORY', 'ALARM', 'ANNEAL', 'ANODE', 'AUTO', 'AUTOSTART',
    'BRAKE', 'BUSY', 'BUZZER', 'BYPASS', 'CATHODE', 'CHAMBER', 'CHANGE',
    'CHILLER', 'CIRCUIT', 'CLOSE', 'COLD', 'CONTACT', 'CONTROL',
    'CONTROLLER', 'COVER', 'CPU', 'DATA', 'DETECT', 'DEVICENET', 'DRAIN',
    'ENABLE', 'ENCODER', 'ETHERCAT', 'ETHERNET', 'EXHAUST', 'EXTEND',
    'FAIL', 'FLAT', 'FLOW', 'FREE', 'FUNCTION', 'HDMI', 'HOST', 'HOT', 'INPUT',
    'INTELOCK', 'INTERFACE', 'INTERLOCK', 'KEYBOARD', 'KEYBORD', 'LABEL',
    'LINE', 'LOCK', 'MASTER', 'MODE', 'MODULE', 'MONITOR', 'MOTOR',
    'MOUSE', 'MOVE', 'NC', 'NEG', 'NETWORK', 'NO', 'NOTE', 'NPN', 'OPEN',
    'OUTPUT', 'PANEL', 'PARAMETER', 'PLC', 'PNP', 'POS', 'POSITION',
    'PRESET', 'PRESSURE', 'PULS', 'RDY', 'RECALL', 'RECEPTACLE', 'RELAY',
    'RELEASE', 'REMOTE', 'RESET', 'RETRACT', 'RUN', 'SELECT', 'SENSOR',
    'SERIAL', 'SERVICE', 'SET', 'SETTING', 'SHUTTER', 'SIGN', 'SLAVE',
    'SLOT', 'SPARE', 'START', 'STATAUS', 'STATUS', 'STO', 'STOP',
    'SWITCH', 'SYSTEM', 'TERMINAL', 'THERMOCOUPLE', 'TIME', 'TRIGGER',
    'USB', 'VGA', 'VIDEO', 'WATCHDOG', 'WATER', 'WIRING',
}

_CIRCUIT_DESCRIPTION = {
    'AC', 'ACIN', 'AG', 'AGND', 'AOUT', 'CLR', 'COM', 'DC', 'DCIN', 'FG',
    'GND', 'IN', 'LG', 'LOAD', 'MR', 'MRR', 'OFF', 'ON', 'OUT', 'PE',
    'PGND', 'POW', 'POWER', 'POWIN', 'PWR', 'RX', 'SG', 'TX', 'VAC',
    'VCC', 'VDC', 'YOUT', 'ZERO',
}
# circuit_description は完全一致に加え「キーワード+数字1桁」も除外対象とする
# （例 OUT2, IN1, COM3。回路のI/O端子番号としてよく使われる形。2026-07-10
# ユーザー指摘）。2桁以上は対象外（例 OUT12 は除外しない＝候補として残る）。
_CIRCUIT_DESCRIPTION_REGEX = re.compile(
    r'^(?:%s)[0-9]?$' % '|'.join(sorted(_CIRCUIT_DESCRIPTION, key=len, reverse=True))
)

_UNIT_NAMES = {
    'CASE', 'CTC', 'EFEM', 'FOUP', 'LA', 'LB', 'LINEA', 'LINEB', 'LL',
    'SH', 'SHIELD', 'TM',
}

_CABLE_COLORS = {
    'BK', 'BL', 'BLACK', 'BLK', 'BLU', 'BLUE', 'BR', 'BRN', 'BROWN', 'GN',
    'GNYE', 'GRAY', 'GREEN', 'GREY', 'GRN', 'GY', 'OR', 'ORANGE', 'PINK',
    'PK', 'PU', 'PURPLE', 'RD', 'RED', 'SB', 'VIOLET', 'VT', 'WH',
    'WHITE', 'YE', 'YELLOW',
}

_TITLEBLOCK_TERMS = {
    'ANGLE', 'APPROVED', 'APPRV', 'CHECK', 'CHECKED', 'DATE', 'DESIG',
    'DESIGNED', 'DRAW', 'DRAWN', 'FINISH', 'ISSUED', 'MARK', 'MATERIAL',
    'NAME', 'REMARKS', 'REV', 'REVISION', 'SCALE', 'SHEET', 'SIZE',
    'TITLE', 'TOLERANCES', 'UNIT', 'WEIGHT',
}
# スペース/ピリオドを含む語句（UNLESS NOTED, MFG No. 等）は CANDIDATE_PATTERN
# （英大文字・数字・ハイフンのみ）に元々一致しないため除外リストに含める必要は
# ない（候補にすらならない）。図面情報枠の構造的除外（フォーマットブロック
# 丸ごと除外）が第一防衛線であり、本リストは第二防衛線。

# (カテゴリ名 -> (完全一致セット, 説明))。
EXCLUSION_EXACT_CATEGORIES = {
    'common_nouns': (_COMMON_NOUNS, '端子/スイッチ等の機能説明語（普通名詞）'),
    'unit_names': (_UNIT_NAMES, 'ユニット/モジュール名'),
    'cable_colors': (_CABLE_COLORS, 'ケーブル色（JIS配線色略号）'),
    'titleblock_terms': (_TITLEBLOCK_TERMS, '図面情報枠内のタイトル項目'),
}

# (カテゴリ名, 正規表現, 説明)。
EXCLUSION_REGEX_CATEGORIES = [
    ('single_letter_position', re.compile(r'^[A-Z]$'),
     '図形枠外の位置記号（単一英大文字）'),
    ('trailing_sign', re.compile(r'.*[+-]$'),
     '末尾が + / - で終わる（電源端子）'),
    ('wire_gauge', re.compile(r'^AWG[0-9]*$'),
     'AWG（ケーブル線径表記）'),
    ('rack_prefix', re.compile(r'^RACK[0-9]*(-[0-9]+)?$'),
     'RACK*（ユニット名）'),
    ('drawing_number', re.compile(r'^[A-Z]{2}[0-9]{4}-[0-9]{3}(-[0-9]{2})?[A-Z]?$'),
     '図番（例 EE1234-500-01A、DE3527-553-05B）'),
    ('terminal_row_letter_digit', re.compile(r'^[AB][0-9]+$'),
     'A+1*/B+1*（機器端子の行番号）'),
    ('earth_terminal_digit', re.compile(r'^PE[0-9]+$'),
     'PE+1*（保護接地端子番号。例 PE1,PE2）'),
    ('phase_rail_letter_digit', re.compile(r'^[LNP][0-9]+[A-Z]*$'),
     'L/N/P+1*（相線 L1-L3・電源レール N24/P24 等。末尾の英大文字は0字以上許容、'
     '2026-07-10 英大文字繰り返しにも対応）'),
    ('io_signal_x_prefix', re.compile(r'^X[A-Z]+$'),
     'X+英字（PLC/内部信号名。例 XRST,XMCON,XPBON。X+数字は除外対象外）'),
    ('circuit_description', _CIRCUIT_DESCRIPTION_REGEX,
     '回路の説明（電源・接地・信号系統名）+数字1桁まで許容（例 OUT2,IN1,COM3）'),
    ('wiring_digit_run', re.compile(r'.*[0-9]{4,}'),
     '数字が4桁以上連続する配線ラベル（例 W1234, CN2345。ハイフン等で分断された'
     '数字は対象外。2026-07-11 ユーザー指定）'),
]


# ============================================================
# 2b. 追加の機器符号パターン（reference_deginator_pattern_added.txt、2026-07-26 追加確定）
# ============================================================
#
# ユーザー提供の速記記法（a=英大文字1字, n=数字1字, *=直前トークンの1回以上
# 繰り返し, .*=任意の0文字以上・カッコやハイフン等の記号を含む）で書かれた
# 「除外リスト」「機器符号リスト」を _compile_shorthand_pattern() で正規表現へ
# 変換して取り込む。EXCLUSION_*_CATEGORIES のようなカテゴリー別（普通名詞・回路説明語・ユニット名…）の意味づけは根拠が
# 曖昧で困難だったため、本リストはカテゴリー分けせず原文の記法のまま
# フラットに保持する（2026-07-26 ユーザー指摘）。
#
# 判定は正規化済みラベル**全体**（括弧を含む）に対して行う（2026-07-26
# ユーザー確定）。`classify_label()` では、英字のみの語が `CN.*`・`MC.*` 等の
# 追加機器符号に一致するかの判定にだけ使う（除外パターンの一覧は2026-10-10に廃止）。
#
# 元ファイルの冗長な重複エントリ（AMP.* の重複、ACTA.* に包含される ACTAa*、
# Fn*.* に包含される Fnnnaa.*）は除去済み（2026-07-26 ユーザー承認）。

def _compile_shorthand_pattern(spec: str) -> 're.Pattern[str]':
    """ユーザー記法（a/n/*/.*、英大文字・ハイフンは文字通り）を正規表現へ変換する。

    a: 英大文字1字（[A-Z]） / n: 数字1字（[0-9]） / *: 直前トークンを1回以上
    繰り返し（+） / .*: 任意の0文字以上（カッコ・ハイフン等の記号を含む、
    正規表現の .* そのもの） / それ以外の英大文字・ハイフンは文字通り一致。
    全体を ^...$ でアンカーする。
    """
    pieces = []  # [(atom, quantifier), ...]
    i = 0
    n = len(spec)
    while i < n:
        ch = spec[i]
        if ch == '.' and i + 1 < n and spec[i + 1] == '*':
            pieces.append(('.*', ''))
            i += 2
            continue
        if ch == '*':
            if not pieces:
                raise ValueError(f'"*" が文字列の先頭にあります: {spec!r}')
            prev_atom, _prev_quant = pieces[-1]
            pieces[-1] = (prev_atom, '+')
            i += 1
            continue
        if ch == 'a':
            atom = '[A-Z]'
        elif ch == 'n':
            atom = '[0-9]'
        else:
            atom = re.escape(ch)  # 英大文字・ハイフン等はそのまま文字通り一致
        pieces.append((atom, ''))
        i += 1
    body = ''.join(atom + quant for atom, quant in pieces)
    return re.compile(r'^%s$' % body)


# 追加機器符号パターン仕様（同ファイルの「# 機器符号」節。冗長エントリ
# （AMP.* の重複・ACTAa*・Fnnnaa.*）は除去済み。2026-07-26 追加確定）
_ADDED_DESIGNATOR_SPECS = [
    'APRn*.*', 'AACn.*', 'ACTA.*', 'ADC', 'AMP.*', 'BH.*', 'CB.*', 'CIR.*',
    'CN.*', 'CON.*', 'CYL.*', 'Dnnnan', 'DCnnan', 'DCPS.*', 'DGH.*', 'DIO.*',
    'DRP.*', 'Fn*.*', 'LS.*', 'MC.*', 'MFC.*', 'MFS.*', 'MOT.*', 'NFn*',
    'OS.*', 'PBa*', 'PFCn*', 'PG.*', 'PS.*', 'RF.*', 'RTM.*', 'RTS.*',
    'SAF.*', 'SB.*', 'SDAMP.*', 'SPD.*', 'SSR.*', 'SV.*', 'SW.*', 'TB.*',
    'TH.*', 'TMP.*', 'TSW.*', 'TUPS.*', 'TUTON.*',
]

# (カテゴリ名, 正規表現, 元の記法) — カテゴリ名は衝突しないよう仕様文字列を
# そのまま使う（意味づけによる分類をしない。本節冒頭コメント参照）。
ADDED_DESIGNATOR_PATTERNS = [
    (f'added_desig:{spec}', _compile_shorthand_pattern(spec), spec)
    for spec in _ADDED_DESIGNATOR_SPECS
]


def matched_added_designator_category(label: str) -> Optional[str]:
    """正規化済みラベル全体（括弧含む）が追加の機器符号パターン
    （ADDED_DESIGNATOR_PATTERNS）のいずれかに一致すればカテゴリ名を返す
    （一致しなければ None）。一致すれば追加除外パターンより優先して
    「機器符号（候補・確定）」となる（2026-07-26 ユーザー確定）。
    """
    for name, rx, _spec in ADDED_DESIGNATOR_PATTERNS:
        if rx.match(label):
            return name
    return None


def normalize_label(label: str) -> str:
    """NFKC正規化+前後空白除去した表示用ラベルを返す（括弧は保持）。"""
    if not label:
        return ''
    return unicodedata.normalize('NFKC', label).strip()


def _judgment_text(normalized_label: str) -> str:
    """判定用文字列を返す（括弧・` **`〈半角スペース+アスタリスク2個〉・
    「単位記号Ωを含む語の直前の空白」のうち最も左にある位置以降と、その
    直前の空白を除く）。例: 'R10(2.2K)' -> 'R10'、
    'FL1F1 ** (FL1F-H12RCE)' -> 'FL1F1'、'R0 2.2KΩ' -> 'R0'。

    3種のデリミタ（`(`・` **`・Ω語の直前の空白）のどれが先に現れるかは
    文字列ごとに異なるため、それぞれの出現位置を調べて最も早い（文字列中で
    より左にある）ものを採用する。

    **Ω区切り（2026-09-11 追加、ユーザー指示）**: 抵抗値等の単位記号 `Ω`
    を含む実データ表記（`'R0 2.2KΩ'`・`'R84 4.7KΩ'` 等、括弧を使わずスペース
    区切りで定数値が続く）に対応する。`Ω` の出現位置を探し、その手前に
    ある最も近いスペースをデリミタ位置とする（`rfind` で `Ω` より前を検索）。
    スペースが見つからない場合（`Ω` を含む語が文字列の先頭にある等）は
    このデリミタは適用しない。

    **末尾空白除去（2026-09-11 追加）**: デリミタの直前に空白がある場合
    （`'CB002 (15A)'` 等、`(` の前にスペースを挟む表記）、除去せずに残すと
    判定用文字列が `'CB002 '`（末尾スペース付き）になり、どの候補パターンにも
    一致しなくなる不具合があった（実データで多数確認: `CB002 (15A)`・
    `Q10 (Q2)`・`TMP (TMP-1003LM)` 等）。デリミタの有無に関わらず
    `.rstrip()` するため、デリミタが無い場合（呼び出し側が既に
    `normalize_label()` で前後空白除去済みの文字列を渡す想定）は実質的に
    何もしない安全な操作である。"""
    idx_paren = normalized_label.find('(')
    idx_star = normalized_label.find(' **')
    idx_omega_char = normalized_label.find('Ω')
    idx_omega_space = (
        normalized_label.rfind(' ', 0, idx_omega_char) if idx_omega_char >= 0 else -1)
    candidates = [i for i in (idx_paren, idx_star, idx_omega_space) if i >= 0]
    idx = min(candidates) if candidates else -1
    judgment = normalized_label[:idx] if idx >= 0 else normalized_label
    return judgment.rstrip()


# ============================================================
# 3. ラベルの3分類（機器符号 / 領域名 / どちらでもない）
# ============================================================
#
# 以前は「Excelの機器符号候補列」（`is_ref_designator_label`）と「領域名候補の
# フィルタ」（旧 `classify_judgment_detailed`）が別々の判定関数を持ち、同じラベルが
# 片方では機器符号候補、もう片方では領域名候補になる食い違いがあった
# （例: `CP004 (10A)`・`GND(M4)`）。2026-10-10、3分類を1つの関数で判定する形に
# 一元化した。ezdxf・region_detector に依存しない純粋な文字列判定のため、
# ref_designator.py（抽出パイプライン）からも region_detector.py からも
# モジュールレベルで import できる。

DESIGNATOR = 'designator'    # a: 機器符号候補（Excelの「機器符号候補」列 CAN）
REGION_NAME = 'region_name'  # b: 領域名候補
OTHER = 'other'              # c: どちらでもない

_LETTERS_ONLY = re.compile(r'^[A-Z]+$')
_SINGLE_LETTER = re.compile(r'^[A-Z]$')
_TRAILING_SIGN = re.compile(r'.*[+-]$')
# 英字のみ以外の候補パターン（letters_only・単独英字は別扱い）
_NON_LETTERS_CANDIDATE = re.compile(
    r'^(?:%s)$' % '|'.join(rx.pattern[1:-1] for n, rx, _d in PATTERN_CATEGORIES
                           if n != 'letters_only'))


# 機器符号の形に見えても機器符号候補にせず、領域名候補にするパターン（2026-10-10 ユーザー指定。
# 後日戻す可能性があるため、戻すときはこのタプルを空にする）。
# 「英字繰返し+数字繰返し+英字繰返し+数字繰返し」（例 `DC19A4`）。
NON_DESIGNATOR_PATTERNS = (
    re.compile(r'^[A-Z]+[0-9]+[A-Z]+[0-9]+$'),
)


# 機器符号候補にも領域名候補にもしない（OTHER）パターン（2026-10-10 ユーザー指定。後日戻す
# 可能性があるため、戻すときはこのタプルを空にする）。
# 「英大文字1字+数字1〜2桁」のうち、A・C・L・Q・U・F 以外の文字で始まるもの
# （例 `R10`・`N24`・`P24`・`D4`・`X05`・`M10`）。A・C・L・Q・U・F は従来どおり機器符号候補。
OTHER_PATTERNS = (
    re.compile(r'^[BDEGHIJKMNOPRSTVWXYZ][0-9]{1,2}$'),
)

# 上の形に一致しても、追加機器符号（`CN.*`・`DIO.*`・`CIR.*`・`MC.*` 等。コネクタ・端子台など）に
# 一致するものは機器符号のまま残す。ただし、この形そのものを定義している追加パターン
# （`Dnnnan`・`DCnnan`。例 `DC19A4`）は対象外（領域名候補にしたいという指定のため）。
_SHAPE_ADDED_SPECS = ('added_desig:Dnnnan', 'added_desig:DCnnan')


def _is_non_designator_shape(judgment: str) -> bool:
    if not any(rx.match(judgment) for rx in NON_DESIGNATOR_PATTERNS):
        return False
    added = matched_added_designator_category(judgment)
    return added is None or added in _SHAPE_ADDED_SPECS


def classify_label(text: str) -> str:
    """ラベルを `DESIGNATOR`（機器符号候補）/ `REGION_NAME`（領域名候補）/
    `OTHER`（どちらでもない）に分類する。呼び出し側での正規化は不要。

    判定用文字列は `_judgment_text()`（括弧・` **`・Ω語より前）で、判定順は次のとおり
    （最初に当たったもので決まる）:
      1. 空・英大文字1字・末尾が `+`/`-` → `OTHER`
      1a. `OTHER_PATTERNS`（A・C・L・Q・U・F 以外の英大文字1字+数字1〜2桁。例 `R10`・`N24`）→ `OTHER`
      1b. `NON_DESIGNATOR_PATTERNS`（英字+数字+英字+数字 の形。例 `DC19A4`）→ `REGION_NAME`
          （ただし `CN.*`・`DIO.*` 等の追加機器符号に一致するものは機器符号のまま）
      2. 英字のみ以外の候補パターン（`PATTERN_CATEGORIES`）に一致 → `DESIGNATOR`
         例 `CP004`・`SX01`・`THM01`・`R10`・`CN-IF2-1`・`RACK1`、`CP004 (10A)`
      3. 英字のみのとき:
         - 除外語（`EXCLUSION_EXACT_CATEGORIES`）のうち `unit_names` → `REGION_NAME`（例 `CTC`）
         - それ以外の除外語・除外パターン → `OTHER`（例 `GND`・`MOTOR`・`SYSTEM`・`XPID`）
         - 追加機器符号（`ADDED_DESIGNATOR_PATTERNS`、ラベル全体基準）に一致 →
           `DESIGNATOR`（例 `CNESCD`・`MCBHPB`）
         - それ以外 → `REGION_NAME`（例 `MFFX`・`FB`）
      4. 上記以外（複数語・記号入りなど）→ `REGION_NAME`（例 `SYSTEM I/F BOX`）
    """
    judgment = _judgment_text(normalize_label(text))
    if not judgment or _SINGLE_LETTER.match(judgment) or _TRAILING_SIGN.match(judgment):
        return OTHER
    if any(rx.match(judgment) for rx in OTHER_PATTERNS):
        return OTHER
    if _is_non_designator_shape(judgment):
        return REGION_NAME
    if _NON_LETTERS_CANDIDATE.match(judgment):
        return DESIGNATOR
    if _LETTERS_ONLY.match(judgment):
        for name, (words, _desc) in EXCLUSION_EXACT_CATEGORIES.items():
            if judgment in words:
                return REGION_NAME if name == 'unit_names' else OTHER
        for _name, rx, _desc in EXCLUSION_REGEX_CATEGORIES:
            if rx.match(judgment):
                return OTHER
        if matched_added_designator_category(judgment) is not None:
            return DESIGNATOR
        return REGION_NAME
    return REGION_NAME


def is_ref_designator_label(text: str) -> bool:
    """text が機器符号候補（Excelの「機器符号候補」列 CAN）か。"""
    return classify_label(text) == DESIGNATOR
