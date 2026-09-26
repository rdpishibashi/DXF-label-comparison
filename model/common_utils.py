import os
import tempfile
import re
import unicodedata

def select_layout_result(doc, collect_from_layout, is_empty):
    """Model Space を優先し、`collect_from_layout(layout)` の結果が
    `is_empty(result)` を満たす（＝空）場合のみ Model 以外のレイアウトを
    順に試す共通ヘルパー。**同一レイアウト内で完結した収集結果のみを返す**
    （レイアウトをまたいで混在させない）。

    Model Space と Paper Space は完全に独立した座標系であり、一方の
    レイアウトで見つけた図面枠のbboxを、別レイアウトのラベル・図形に対する
    「枠内か」の判定に使うと、座標が対応せず正しい要素が誤って除外される
    （`EE6892-455B.dxf`: 図面枠はPaper Space〈`ICADSX Layout`〉のみに存在する
    一方、実際の機器符号ラベル`CB001`等はModel Spaceにあり、Paper Space
    由来の枠bboxでModel Spaceのラベルをフィルタすると大半が「枠外」と誤判定
    され出力から消える不具合が発生した。2026-07-14ユーザー報告。
    `region_detector.py`・`ref_designator.py`・`terminal_detector.py` の
    3モジュールに共通する設計方針のため、ここに集約する。2026-07-26、
    region_detector.py から common_utils.py へ移設——ジオメトリ・領域検出
    ロジックに依存しない汎用ヘルパーであり、3モジュールの共通依存先として
    ドメイン特化モジュールより common_utils.py の方が適切なため）。

    Args:
        doc: ezdxf Document
        collect_from_layout: layout を受け取り、その1レイアウトに閉じた
            収集結果を返す関数（呼び出し側で定義する。戻り値の型は自由）
        is_empty: collect_from_layout の戻り値を受け取り、「内容が空か」を
            判定する関数
    """
    result = collect_from_layout(doc.modelspace())
    if not is_empty(result):
        return result

    try:
        for layout in doc.layouts:
            if layout.name != 'Model':
                alt_result = collect_from_layout(layout)
                if not is_empty(alt_result):
                    return alt_result
    except Exception:
        pass

    return result

def is_invisible(e, check_layer=True):
    """DXFの`invisible`属性（グループコード60、1=非表示）が立っている
    エンティティ、または**エンティティが所属するレイヤーがオフ/フリーズ
    されている**エンティティかを返す。CADソフト上で「非表示」に設定された
    図形（紙面には一切表示されない）は、たとえDXFファイル中に座標・テキスト
    として存在していても、図面枠検出・ラベル収集・領域検出・端子検出の
    いずれの対象にもしてはならない（2026-09-10、ユーザー報告により
    `ref_designator.py`に追加。ULVAC標準の改版運用では、旧版のタイトル
    ブロックを削除せずinvisibleにして履歴として保持する例があるため、
    他の収集経路でも起こりうる不具合として2026-09-11、region_detector.py・
    terminal_detector.py・extract_labels.pyへも横展開し、この共通ヘルパーに
    集約した。詳細は`tests/regression/test_ref_designator.py`の
    invisible関連テストを参照）。

    呼び出し側は次の3箇所すべてでチェックする必要がある（`virtual_entities()`
    は親INSERTのinvisible属性を継承しないため、INSERT自身のチェックを
    省くと、INSERT自身がinvisibleでも展開後の中身は素通りしてしまう）:
      1. 直接配置エンティティ
      2. INSERT自身（invisibleなINSERTは中身ごと丸ごと除外する）
      3. `virtual_entities()`で展開した仮想エンティティ（親が可視でも
         個々の子エンティティにinvisibleが立っている場合があるため）

    レイヤー単位の非表示状態（2026-09-16、ユーザー報告により追加）:
    エンティティ自身の`invisible`属性が立っていなくても、そのエンティティが
    置かれたレイヤー自体が「オフ」または「フリーズ」されていれば、画面上
    ・印刷時ともに一切表示されない。ULVAC標準の改版運用では、旧版の
    タイトルブロックをエンティティ単位のinvisible属性ではなく、専用レイヤー
    ごとオフ/フリーズして非表示にする例があり、この場合は上記の`invisible`
    属性チェックだけでは検出できない（実データ`EE3273-039-90B.dxf`で確認:
    旧タイトルブロック一式が`off=True, frozen=True`のレイヤーに置かれており、
    現在は存在しない図番「EE3273-039-90A」が誤って抽出されていた）。
    `virtual_entities()`で展開した仮想エンティティも`.doc`経由で元のレイヤー
    テーブルを参照できるため、同じチェックで対応できる（`.layer`属性は
    展開後も元のレイヤー名を保持し、親INSERTのレイヤー状態を継承しない
    `invisible`属性とは異なる——仮想エンティティ自身のレイヤー参照だけで
    正しく判定できる）。レイヤーテーブルに存在しない・`.doc`が取得できない
    等の異常系は「非表示ではない」側にフォールバックする（誤って全除外に
    ならないよう保守的に扱う）。

    `check_layer=False`（2026-09-23追加）: レイヤー単位の判定をスキップし、
    エンティティ自身の`invisible`属性のみを見る。表示中のエンティティだけで
    図番・図面枠等の手がかりが1件も見つからない場合の**フォールバック探索**
    でのみ使う（唯一のタイトルブロックがoff/frozenレイヤーに置かれている
    図面では、レイヤー単位の判定を常時適用すると図番・タイトル・図面枠が
    一切検出できなくなるため。実データ`EE5322-455-02A.dxf`/`-18A.dxf`で
    確認、2026-09-16のレイヤー単位判定追加が原因の回帰）。既定の`True`では
    従来通りの判定を行う。
    """
    if bool(e.dxf.get('invisible', 0)):
        return True

    if not check_layer:
        return False

    layer_name = e.dxf.get('layer', None)
    doc = getattr(e, 'doc', None)
    if layer_name and doc is not None:
        try:
            if layer_name in doc.layers:
                layer = doc.layers.get(layer_name)
                if layer.is_off() or layer.is_frozen():
                    return True
        except Exception:
            pass

    return False


def save_uploadedfile(uploadedfile):
    """アップロードされたファイルを一時ディレクトリに保存する"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(uploadedfile.name)[1]) as f:
        f.write(uploadedfile.getbuffer())
        return f.name

def normalize_width(text):
    """全角の英数字・記号・スペースを半角に折り畳む（NFKC正規化）。

    手書き回路DXFには同じ語が半角(SYSTEM)と全角(ＳＹＳＴＥＭ)で混在するため、
    出力ファイルの集計・記録は半角へ統一する（ユーザー指定の仕様、2026-07-03）。
    NFKC はかな・漢字には影響せず、半角カナは全角カナへ正規化される。
    """
    if not text:
        return text
    return unicodedata.normalize('NFKC', text)

def filter_non_circuit_symbols(labels, debug=False):
    """
    機器符号フォーマットに一致しないラベルをフィルタリングする

    新しい機器符号フォーマット:
    - AA+ (例: CNCNT, FB)
    - A+N+ (例: R10, CN3, PSW1)
    - A+N+A+ (例: X14A, RMSS2A)
    - AA+([内容]) (例: FB(), MSS(MOTOR))
    - A+N+([内容]) (例: R10(2.2K), MSSA(+))
    - A+N+A+([内容]) (例: U23B(DAC))

    Args:
        labels: フィルタリング対象のラベルリスト
        debug: デバッグ情報を出力するかどうか

    Returns:
        tuple: (フィルタリング後のラベルリスト, 除外されたラベル数)
    """

    patterns = [
        # 英文字のみ（2文字以上）
        r'^[A-Za-z]{2,}$',

        # 英文字+数字
        r'^[A-Za-z]+\d+$',

        # 英文字+数字+英文字
        r'^[A-Za-z]+\d+[A-Za-z]+$',

        # 英文字のみ+括弧（オプション）
        r'^[A-Za-z]{2,}\([^)]*\)$',

        # 英文字+数字+括弧（オプション）
        r'^[A-Za-z]+\d+\([^)]*\)$',

        # 英文字+数字+英文字+括弧（オプション）
        r'^[A-Za-z]+\d+[A-Za-z]+\([^)]*\)$',
    ]

    filtered_labels = []
    excluded_count = 0

    for label in labels:
        # 全角表記の機器符号（例: ＣＮ１）も半角相当で判定する。
        # 返すラベル自体は加工しない（呼び出し元は元のテキストと突き合わせる）。
        target = normalize_width(label)
        is_match = False
        for pattern in patterns:
            if re.match(pattern, target):
                is_match = True
                break

        if is_match:
            filtered_labels.append(label)
            if debug:
                print(f"✓ 機器符号として認識: {label}")
        else:
            excluded_count += 1
            if debug:
                print(f"✗ 機器符号として除外: {label}")

    return filtered_labels, excluded_count

def process_circuit_symbol_labels(labels, filter_non_parts=False, validate_ref_designators=False, debug=False):
    """
    ラベルに対して機器符号処理を統合的に実行する

    Args:
        labels: 処理対象のラベルリスト
        filter_non_parts: 機器符号以外のラベルをフィルタリングするかどうか
        validate_ref_designators: 未使用（機器符号妥当性チェック機能は v1.6.0 で削除）。
            `model/extract_labels.py`（DXF-diff-manager とバイト一致コピーを維持する
            共有ファイル）がこの引数を渡し続けるため、シグネチャ互換のためだけに残す。
        debug: デバッグ情報を表示するかどうか

    Returns:
        dict: 処理結果を含む辞書
            - 'labels': 処理後のラベルリスト
            - 'filtered_count': フィルタリングで除外されたラベル数
            - 'invalid_ref_designators': 常に空リスト（機能削除済み、互換のため維持）
    """
    result = {
        'labels': labels.copy(),
        'filtered_count': 0,
        'invalid_ref_designators': []
    }

    # フィルタリング処理
    if filter_non_parts:
        filtered_labels, filtered_count = filter_non_circuit_symbols(labels, debug)
        result['labels'] = filtered_labels
        result['filtered_count'] = filtered_count

    return result
