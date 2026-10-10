"""
領域検出（矩形領域抽出）の設定ファイル

複数プロジェクトで共有するファイル（primary: DXF-extract-labels。
DXF-label-comparison にバイト一致のコピーがあり、同プロジェクトの
`tests/regression/spec/test_shared_files_identical_to_extract_labels.py`
が一致を検証する）。設定値の変更は primary で行い、コピーへそのまま伝播すること。

2026-09-10、「領域検出の詳細設定」UI（st.form によるフォーム）を撤去し、
DXF-diff-manager の config.py 方式（UIウィジェットではなくこのファイルの
編集で設定を変える）に統一した（ユーザー指示）。各アプリはこのファイルの
`region_detection_config` から `view/region_selection.build_region_cfg()` で
`region_cfg` を組み立てる。

**意図的に `extraction_config` は定義しない**: `model/extract_labels.py` は
`from config import extraction_config` を試み、失敗（ImportError）した場合のみ
内部フォールバック定義（`DRAWING_NUMBER_PATTERN` 等）を使う設計になっている
（DXF-diff-manager 等、他プロジェクトとバイト一致を保つ共有ファイルのため
`extract_labels.py` 自体は変更しない）。このファイルに `extraction_config` を
書き足すと、図番抽出パターン等の挙動が意図せず変わってしまう。ここでは
領域検出の設定のみを扱い、`extraction_config` には触れない
（DXF-extract-labels `tests/unit/test_config_region_cfg.py` で固定）。
"""


class RegionDetectionConfig:
    """領域検出（矩形領域抽出）のパラメータ。

    旧「領域検出の詳細設定」フォームが公開していた11設定。各値の直上のコメントは、
    そのフォームの該当フィールドが表示していた説明文（help）をそのまま記載し、
    あわせて既定値・単位が分かるようにしている。値を変えると次回の「領域を検出」
    実行時から反映される（アプリの再起動は不要。Streamlit はモジュールを都度
    再読み込みしないため、Streamlit サーバー自体の再起動が必要な場合がある
    — `~/.claude/skills/streamlit` スキル参照）。
    """

    # 図面全体を囲む枠の線の太さ（lineweight）
    # 既定値: 100
    FRAME_LINEWEIGHT = 100

    # 矩形領域の境界線の太さ（lineweight）
    # 既定値: 25
    REGION_LINEWEIGHT = 25

    # 接続点（円）が境界線からこの座標距離以内なら「境界上」とみなします。
    # 縦ギャップ上に接続点がある場合の橋渡し除外にも使用します。
    # 既定値: 0.05（DXF座標単位）
    CONNECTION_POINT_MARGIN = 0.05

    # 矩形領域の境界線の色（既定: 2 = 黄）。AutoCAD カラーインデックス(ACI)。
    # 既定値: 2（黄）
    REGION_COLOR = 2

    # 指定すると、名称候補がこの文字列のいずれかで始まる領域は、面積比の閾値を
    # 満たさなくても抽出対象になります（前方一致。全角/半角は区別しません）。
    # 空欄なら従来通り面積比のみで判定します。
    # 既定値: ()（空＝面積比のみで判定）
    NAME_FILTER_PREFIXES = ()

    # これらの語を含むラベルを名称候補から除外します
    # 既定値: ('NOTE', '☆')
    NAME_EXCLUDE_TERMS = ('NOTE', '☆', 'ACCESSORY CABLE', 'FLAT CABLE')

    # 1つの閉領域が単独でこの面積比（四捨五入した整数%）以上のとき抽出対象とします
    # 既定値: 5（= 5%。図面枠面積比）
    AREA_RATIO_PERCENT = 5

    # 同じ名称の複数ピースを合算したとき、この面積比（四捨五入した整数%）以上なら
    # 抽出対象とします。第1図面で成立した名称は他図面でも面積不問で抽出します。
    # 既定値: 10（= 10%。図面枠面積比）
    GROUP_AREA_RATIO_PERCENT = 10

    # 下端境界線からこの座標距離以内のラベルを名称候補とします
    # 既定値: 10（DXF座標単位）
    NAME_MAX_DIST = 10

    # この距離未満（境界線分上）のラベルは名称候補から除外します
    # 既定値: 1（DXF座標単位）
    NAME_MIN_DIST = 1

    # 英字がこの文字数以上のラベルのみ名称候補とします
    # 既定値: 3（文字）
    NAME_MIN_LETTERS = 3

    # ── 上記11設定以外の詳細パラメータの上書き（通常は変更不要）──
    #
    # `model/region_detector.py` の `DEFAULT_REGION_CONFIG` は、上記11設定以外にも
    # アルゴリズム内部のパラメータ（面探索の座標マージン `face_snap`、共線結合の
    # 許容誤差 `merge_level_tol`、ギャップ橋渡しの方針 `bridge_vertical_gaps` /
    # `bridge_horizontal_gaps` 等）を持つ。これらは通常のユーザー操作では変更が
    # 不要なため専用フィールドを設けていないが、将来必要になった場合のために
    # `DEFAULT_REGION_CONFIG` のキーをそのまま上書きできる辞書を用意しておく
    # （既定は空 — アルゴリズム内部値は `DEFAULT_REGION_CONFIG` のまま変更しない）。
    # 例: {'face_snap': 0.2, 'connection_point_threshold': 2}
    ADVANCED_OVERRIDES = {}


# 設定クラスのインスタンスを作成（簡単にアクセスできるように）
region_detection_config = RegionDetectionConfig()
