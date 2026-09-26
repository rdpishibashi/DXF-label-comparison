"""Reference Designator（機器符号）分類ロジック（純粋なパターン判定のみ）。

`ref_designator.py`（DXF抽出パイプライン）から2026-07-26のモジュール分割で
切り出した。ezdxf・region_detector.py への依存を一切持たない自己完結モジュール
で、正規化済み文字列に対するパターン判定のみを行う（DXFファイルの読み込み・
図面枠検出・ラベル収集は `ref_designator.py` 側が担当する）。

この分割の動機: `region_detector.py`（矩形領域検出）の `_is_valid_name_candidate()`
が領域名候補の機器符号除外判定にこの分類ロジックを必要とする一方、旧
`ref_designator.py` は `region_detector.py`（`detect_drawing_frames`・
`assign_region_labels`）に依存していたため、両者は循環依存の関係にあった
（`region_detector.py` 側は関数内 `from . import ref_designator` の遅延importで
回避していたが、これは循環依存そのものを解消するものではなかった）。本ファイルは
`region_detector.py` に一切依存しないため、`region_detector.py` はこのファイルを
モジュールレベルで安全にimportできる（遅延import不要）。

**2026-07-30 用途の変更**: `ref_designator.py`（DXF抽出パイプライン）は判定
条件の簡素化により、除外・追加・確定パターンを使わなくなった（3つの候補
パターンに一致すれば全件そのまま出力する方式に変更）。本ファイルの除外・
追加・確定パターン一式は、`region_detector.py` の領域名候補フィルタ専用として
引き続き利用する（`classify_judgment_detailed()`/`matched_confirmed_category()`
を経由）。「未確定ラベル」UIでの連動採用・判断ログ向けの関数（旧
`sibling_key()`/`propagate_selection_all_files()`/`PATTERNS_VERSION`）は
その機能自体の廃止に伴い削除した。

reference_designator_candidates.xlsx（`Patterns` / `ExclusionPatterns` /
`ConfirmedPatterns` シート）を正としてパターン・除外・確定リストを実装する。
"""
import re
import unicodedata
from collections import Counter
from typing import Dict, List, Optional, Tuple


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
    'FAIL', 'FLOW', 'FREE', 'FUNCTION', 'HDMI', 'HOST', 'HOT', 'INPUT',
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
# 2b. 追加パターン（reference_deginator_pattern_added.txt、2026-07-26 追加確定）
# ============================================================
#
# ユーザー提供の速記記法（a=英大文字1字, n=数字1字, *=直前トークンの1回以上
# 繰り返し, .*=任意の0文字以上・カッコやハイフン等の記号を含む）で書かれた
# 「除外リスト」「機器符号リスト」を _compile_shorthand_pattern() で正規表現へ
# 変換して取り込む。EXCLUSION_*_CATEGORIES / CONFIRMED_PATTERN_CATEGORIES の
# ようなカテゴリー別（普通名詞・回路説明語・ユニット名…）の意味づけは根拠が
# 曖昧で困難だったため、本リストはカテゴリー分けせず原文の記法のまま
# フラットに保持する（2026-07-26 ユーザー指摘）。
#
# 判定は正規化済みラベル**全体**（括弧を含む）に対して行う（2026-07-26
# ユーザー確定）。既存の EXCLUSION_*_CATEGORIES / CONFIRMED_PATTERN_CATEGORIES
# の大半が判定用文字列（judgment＝括弧より前）を基準にするのと対照的。
#
# 優先順位（2026-07-26 ユーザー確定、`classify_judgment_detailed()` に実装）:
#   1) 候補形ゲート（CANDIDATE_PATTERN、judgment基準）— 不一致なら no_match
#   2) 既存の除外リスト（EXCLUSION_EXACT/REGEX_CATEGORIES、judgment基準）
#      — 一致すれば excluded。ADDED_DESIGNATOR_PATTERNS と同じ語幹が衝突しても
#      既存除外が優先される（例: MOT.* は追加機器符号だが MOTOR は普通名詞
#      除外のまま／PG.* は追加機器符号だが PGND は回路説明語除外のまま。
#      いずれも「英単語はそのまま除外、MOT1・PG1 等の記号形だけ救済」という
#      ユーザー判断）。
#   3) 追加の機器符号リスト（ADDED_DESIGNATOR_PATTERNS、ラベル全体基準）
#      — 一致すれば candidate かつ確定（自動採用）。4) の追加除外リストより
#      優先する（例: DC12A3 は DCn*.* 除外より DCnnan 機器符号が優先され、
#      機器符号として確定する。LS1/OS1 も同様に La.*/O.* 除外より
#      LS.*/OS.* 機器符号が優先される）。
#   4) 追加の除外リスト（ADDED_EXCLUSION_PATTERNS、ラベル全体基準）
#      — 一致すれば excluded（例 DC12, K1, O5, I9）。
#   5) いずれにも該当しなければ candidate（従来どおり
#      CONFIRMED_PATTERN_CATEGORIES で確定/未確定に分岐）。
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


# 追加除外パターン仕様（reference_deginator_pattern_added.txt の
# 「# 機器符号ではない（除外するパターン）」節、2026-07-26 追加確定）
_ADDED_EXCLUSION_SPECS = [
    'An*.*', 'ACn*V', 'AOn*', 'AWSINn*', 'Bn*.*', 'BBC', 'Cn*', 'CM.*',
    'DCn*.*', 'DIn*', 'DOn*', 'E-LANn*', 'ETC-JPn*', 'FREE.*', 'H.*', 'I.*',
    'J.*', 'Kn', 'Kn.*', 'La.*', 'O.*', 'OUTn*', 'PLD.*', 'Qn*', 'Sn*.*',
    'SC.*', 'SKn*', 'SXn*', 'SYn*', 'Tn', 'TP.*', 'TQ.*', 'Wn*', 'Wn*a*',
    'WESn*', 'WLn*', 'WLn*a*', 'Yn*', 'YOn*', 'Zn*',
]

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
ADDED_EXCLUSION_PATTERNS = [
    (f'added_excl:{spec}', _compile_shorthand_pattern(spec), spec)
    for spec in _ADDED_EXCLUSION_SPECS
]
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


def matched_added_exclusion_category(label: str) -> Optional[str]:
    """正規化済みラベル全体（括弧含む）が追加の除外パターン
    （ADDED_EXCLUSION_PATTERNS）のいずれかに一致すればカテゴリ名を返す
    （一致しなければ None）。
    """
    for name, rx, _spec in ADDED_EXCLUSION_PATTERNS:
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


def classify_judgment_detailed(
    judgment: str, label: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """判定用文字列（括弧より前）を分類し、(status, category) を返す。

    `label` は正規化済みラベル全体（括弧を含みうる）。省略時は `judgment` を
    代わりに使う（呼び出し側が括弧より前の文字列しか持たない場合の後方互換）。
    追加パターン（ADDED_DESIGNATOR_PATTERNS/ADDED_EXCLUSION_PATTERNS、
    2026-07-26）はこの `label`（ラベル全体）を基準に判定する。

    status は 'candidate' / 'excluded' / 'no_match'。
    - 'no_match': 3パターン（Patterns シート）のいずれにも一致しない文字列
      （説明文・記号・注記等、例 `(2/5)`）。category は常に None。
    - 'excluded': Patterns には一致したが、除外パターン（ExclusionPatterns シート、
      例 GND・TITLE・N24 等、または ADDED_EXCLUSION_PATTERNS）に該当したもの。
      明らかに Reference Designator ではないと確定しているため、候補にも
      未確定ラベルにも含めない。category は該当したカテゴリの名前。
    - 'candidate': Patterns に一致し、除外パターンにも該当しないもの（または
      ADDED_DESIGNATOR_PATTERNS に一致して除外より優先的に救済されたもの）。
      category は常に None。reference_designator_candidates.xlsx の
      RemainingUnclassified シートと同じ母集団（＝機器符号候補そのもの）で、
      「未確定ラベル」UI でのレビュー対象になる（2026-07-10、実データで
      RemainingUnclassified の中身を再確認して確定: GND/INPUT/TITLE 等は
      除外カテゴリが付与されており RemainingUnclassified には含まれない＝
      'excluded' は表示対象外が正しい）。

    判定順序（2026-07-26、ADDED_* 追加時に確定。本節冒頭コメント参照）:
    候補形ゲート → 既存除外（judgment基準）→ 追加機器符号（label基準、
    追加除外より優先）→ 追加除外（label基準）。
    """
    if not judgment or not CANDIDATE_PATTERN.match(judgment):
        return 'no_match', None
    for name, (words, _desc) in EXCLUSION_EXACT_CATEGORIES.items():
        if judgment in words:
            return 'excluded', name
    for name, rx, _desc in EXCLUSION_REGEX_CATEGORIES:
        if rx.match(judgment):
            return 'excluded', name
    full_label = label if label is not None else judgment
    if matched_added_designator_category(full_label) is not None:
        return 'candidate', None
    added_excl_category = matched_added_exclusion_category(full_label)
    if added_excl_category is not None:
        return 'excluded', added_excl_category
    return 'candidate', None


def _classify_judgment(judgment: str, label: Optional[str] = None) -> str:
    """`classify_judgment_detailed()` の status のみを返す簡易版。"""
    status, _category = classify_judgment_detailed(judgment, label)
    return status


def is_ref_designator_candidate(label: str) -> bool:
    """正規化済みラベル（表示用、括弧を含みうる）が機器符号（候補）かどうかを返す。

    判定は括弧より前の部分（既存パターン）とラベル全体（追加パターン）の
    双方に対して行う。呼び出し側は `normalize_label()` で正規化した文字列を
    渡すこと（内部では再正規化しない）。
    """
    return _classify_judgment(_judgment_text(label), label) == 'candidate'


def split_candidates(labels: List[str]) -> List[str]:
    """正規化済みラベルのリストから機器符号（候補）だけを抽出して返す。

    候補は Patterns（3パターン）に一致し、かつ除外パターンに該当しないもの
    （reference_designator_candidates.xlsx の RemainingUnclassified シートと
    同じ母集団）。除外パターン該当（GND・TITLE 等）・3パターン非一致
    （`(2/5)` 等の記号・注記）はいずれも結果に含めない。
    """
    return [label for label in labels
            if _classify_judgment(_judgment_text(label), label) == 'candidate']


def summarize_labels(labels: List[str]) -> List[Tuple[str, int]]:
    """ラベルリストを (ラベル, 個数) にカウントし、ラベル昇順で返す。"""
    counter = Counter(labels)
    return [(lbl, counter[lbl]) for lbl in sorted(counter.keys())]


# ============================================================
# 3. 確定パターン（機器符号（候補）のうち、レビュー不要で自動採用してよいもの）
# ============================================================
#
# 機器符号（候補）＝ is_ref_designator_candidate の中でも、確実に Reference
# Designator と判定してよい形をユーザーと確定したパターン（2026-07-10、
# CN/CN-IF/R(...)/VR(...) は2026-07-10 追加確定）。一致したラベルは
# 「未確定ラベル」UI でのレビューを経ずに最終出力へ自動採用する。
# A,B の除外は single_letter_digits_except_ab（単一英字+数字）のみに適用する
# （letters_digits_2or3 系には適用しない。A1/B12等は既存の
# terminal_row_letter_digit 除外パターンで確定パターン判定より前に除外される
# ため実害はない）。
#
# 各カテゴリの判定基準（第2要素）:
#   'judgment' … 括弧より前の判定用文字列（`_judgment_text()`）に対して判定
#                （通常のパターン・除外判定と同じ基準）
#   'full'     … 正規化済みラベル全体（括弧を含む）に対して判定
#                （R(...)/VR(...) のように括弧の中身自体を問う場合に使う）

CONFIRMED_PATTERN_CATEGORIES = [
    # より限定的なパターンを先に判定する（複数一致した場合、より具体的な
    # カテゴリ名が集計・表示に反映されるようにするため。確定/未確定の結果
    # 自体はどの順でも変わらない＝いずれか1つでも一致すれば確定）。
    ('cn_single_digit', 'judgment', re.compile(r'^CN[0-9]$'),
     'CN + 数字1桁'),
    ('cn_if_prefix', 'judgment', re.compile(r'^CN-IF.*$'),
     '"CN-IF" + 任意の文字'),
    ('r_paren_suffix', 'full', re.compile(r'^R[0-9]+\(.*\)$'),
     'R + 数字繰り返し + "(" + 任意の文字 + ")"'),
    ('vr_paren_suffix', 'full', re.compile(r'^VR[0-9]+\(.*\)$'),
     'VR + 数字繰り返し + "(" + 任意の文字 + ")"'),
    ('letters_digits_2or3', 'judgment', re.compile(r'^[A-Z]+[0-9]{2,3}$'),
     '英大文字繰り返し + 数字2桁または3桁'),
    ('letters_digits_2or3_letter', 'judgment', re.compile(r'^[A-Z]+[0-9]{2,3}[A-Z]$'),
     '英大文字繰り返し + 数字2桁または3桁 + 英大文字1字'),
    ('hyphen_letters_digits_notail', 'judgment', re.compile(r'^[A-Z]+-[A-Z]+[0-9]+$'),
     '英大文字繰り返し + ハイフン + 英大文字繰り返し + 数字繰り返し（末尾に続きなし）'),
    ('single_letter_digits_except_ab', 'judgment', re.compile(r'^[C-Z][0-9]+$'),
     'A,B以外の英大文字1字 + 数字の繰り返し'),
]


def matched_confirmed_category(label: str) -> Optional[str]:
    """正規化済みラベル（括弧を含みうる）が確定パターンのいずれかに一致すれば
    カテゴリ名を、一致しなければ None を返す。

    従来の `CONFIRMED_PATTERN_CATEGORIES` を先に判定し（カテゴリ名の後方互換を
    保つ。例: `CN3` は追加パターンの `added_desig:CN.*` にも一致するが、既存の
    `cn_single_digit` を優先して返す）、一致しなければ追加の機器符号パターン
    （ADDED_DESIGNATOR_PATTERNS、ラベル全体基準）を判定する（2026-07-26）。
    いずれか一方にでも一致すれば「確定（自動採用）」である点は変わらない
    （判定順序が影響するのはカテゴリ名の表示のみ）。`CONFIRMED_PATTERN_CATEGORIES`
    の大半は括弧より前の判定用文字列（judgment）に対して判定するが、括弧の
    中身自体を問うパターン（`r_paren_suffix`/`vr_paren_suffix`）はラベル全体に
    対して判定する（`CONFIRMED_PATTERN_CATEGORIES` の判定基準参照）。
    """
    judgment = _judgment_text(label)
    for name, basis, rx, _desc in CONFIRMED_PATTERN_CATEGORIES:
        target = label if basis == 'full' else judgment
        if rx.match(target):
            return name
    return matched_added_designator_category(label)


def is_confirmed_designator(label: str) -> bool:
    """正規化済みラベルが機器符号（候補）であり、かつ確定パターンにも一致するか。

    True の場合、「未確定ラベル」UI でのレビューを経ずに最終出力へ自動採用してよい。
    """
    judgment = _judgment_text(label)
    if _classify_judgment(judgment, label) != 'candidate':
        return False
    return matched_confirmed_category(label) is not None
