"""準備中の会社（_index.yaml に「準備中: true」）の駅データを、本番のページから作り直す。

使い方: python tools/sync_live.py <本番のページのフォルダ（fetch_live.py の出力）>
- FTP で手書きしている会社を YAML へ移す準備期間に使う。準備期間は本番（FTP）が正なので、
  駅データ（data/<会社フォルダ>/*.yaml と _index.yaml）は本番のページから毎回作り直す（手で直さない）
- 一覧のアイコン規則は、いまの _index.yaml のものを使う。本番の一覧と食い違うアイコンはそのまま残す（keep）
- 駅写真（images/）には触らない
- 最後に、作り直した駅データから生成したページを本番のページと比べ、食い違いを表示する
"""
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PY = sys.executable
FLAG_LINE = "準備中: true   # FTP からの移行の準備期間。本番には上げず、毎日本番のページと照合する"


def staging_companies():
    out = []
    for idx in sorted(DATA.rglob("_index.yaml")):
        d = yaml.safe_load(idx.read_text(encoding="utf-8"))
        if d.get("準備中"):
            out.append((idx.parent, d))
    return out


def main(live):
    live = pathlib.Path(live)
    companies = staging_companies()
    if not companies:
        print("準備中の会社はありません")
        return
    # 1. 駅データを作り直す（全社分を先に。一覧は他社の駅データを参照するので、そのあとでまとめて）
    for folder, idx in companies:
        src = live / folder.relative_to(DATA)
        if not (src / idx["ファイル名"]).is_file():
            sys.exit(f"{folder.relative_to(DATA)}: 本番のページ（{src}）がありません。先に fetch_live.py を実行してください")
        for f in folder.glob("*.yaml"):
            if not f.name.startswith("_"):
                f.unlink()
        subprocess.run([PY, str(ROOT / "tools/html2yaml.py"), str(src), str(folder)], check=True)
    # 2. 一覧を作り直す
    for folder, idx in companies:
        src = live / folder.relative_to(DATA)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
            yaml.dump(idx["アイコン規則"], fh, allow_unicode=True)
            rules = fh.name
        r = subprocess.run([PY, str(ROOT / "tools/index2yaml.py"), str(src / idx["ファイル名"]), str(folder),
                            str(idx["会社コード"]), rules, "keep"], check=True, capture_output=True, text=True)
        print(f"{folder.relative_to(DATA)} の一覧: " + " ／ ".join(l for l in r.stdout.splitlines() if l.startswith("■")))
        p = folder / "_index.yaml"
        s = p.read_text(encoding="utf-8")
        s = re.sub(r"^(会社コード: \d+)$", r"\1\n" + FLAG_LINE, s, count=1, flags=re.M)
        p.write_text(s, encoding="utf-8")
    # 3. 生成して本番のページと比べる
    subprocess.run([PY, str(ROOT / "tools/build.py")], check=True, capture_output=True)
    for folder, idx in companies:
        rel = folder.relative_to(DATA)
        r = subprocess.run([PY, str(ROOT / "tools/compare.py"), str(live / rel), str(ROOT / "dist/staging/station-individual" / rel)],
                           capture_output=True, text=True)
        diffs = [l.removeprefix("■ 差分あり: ") for l in r.stdout.splitlines() if l.startswith("■ 差分あり")]
        print(f"{rel}: {r.stdout.strip().splitlines()[-1]}" + (f"（食い違い: {' '.join(diffs)}）" if diffs else ""))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
