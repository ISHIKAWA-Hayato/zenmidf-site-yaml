"""inbox/ に置かれた駅写真を取り込む。GitHub Actions（deploy.yml）から毎回呼ばれる。

ファイル名: <会社番号2桁>-<駅ID>.<拡張子>   例: 32-takaoka.png / 06-sasabaru.jpg / 05-san_takamatsu.heic
  - 会社番号は data/ の会社フォルダ名の頭2桁（04 西日本・05 四国・06 九州・21～37 私鉄・三セク）
  - 駅IDは駅ページのURLの末尾（.html の前）と同じ
  - すでに写真がある駅は上書きしない（inbox/_skipped/ に理由付きで残す）。差し替えたいときは駅IDの後ろに ! を付ける
    例: 06-isahaya!.png
処理:
  1. 会社と駅を特定（駅データ data/<会社>/<駅ID>.yaml があること）
  2. スマホの回転情報どおりに向きを直し、4:3 から大きく外れていないか確かめる
  3. 800×600 の PNG にして、位置情報などのメタデータを付けずに data/<会社>/images/ に保存
     （駅データに 画像: があればそのファイルを差し替え、なければ images/<駅ID>.png を作って 画像: を書き足す）
  4. 取り込んだ元ファイルは inbox から消す
取り込めなかったものは inbox/_error/ に移し、理由を同名の .txt に書く（実行は失敗扱いにして知らせる）。
既存の写真があって上書きしなかったものは inbox/_skipped/ に移す（失敗扱いにはしない）。

使い方: python tools/import_images.py   （inbox が空なら何もしない）
"""
import os
import pathlib
import re
import shutil
import sys

import yaml
from PIL import Image, ImageOps

try:
    import pillow_heif  # iPhone の HEIC を読むため（なくても PNG/JPG は読める）
    pillow_heif.register_heif_opener()
except ImportError:
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA, INBOX = ROOT / "data", ROOT / "inbox"
ERROR = INBOX / "_error"
SKIPPED = INBOX / "_skipped"
SIZE = (800, 600)
ASPECT_TOLERANCE = 0.03  # 4:3 からのずれの許容（3%）
NAME = re.compile(r"^(\d{2})-([a-z0-9_][a-z0-9_-]*?)(!?)\.(png|jpe?g|heic|heif|webp)$", re.IGNORECASE)


class Skip(Exception):
    """既存の写真があるので上書きしない"""


def companies():
    """会社番号 → 会社フォルダ（_index.yaml があるフォルダの名前の頭2桁）"""
    out = {}
    for idx in DATA.rglob("_index.yaml"):
        m = re.match(r"^(\d{2})-", idx.parent.name)
        if m:
            out[m.group(1)] = idx.parent
    return out


def set_image_field(yml, value):
    """駅データに 画像: がなければ、画像説明:／駅注記:／設備: の前に1行足す（ほかの行の書き方は崩さない）"""
    text = yml.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r"^(画像説明|駅注記|設備):", line):
            lines.insert(i, f"画像: {value}\n")
            yml.write_text("".join(lines), encoding="utf-8")
            return
    raise ValueError("駅データに 設備: が見つからず、画像: を書き足せません")


def import_one(path, comp):
    m = NAME.match(path.name)
    if not m:
        raise ValueError("ファイル名が「会社番号2桁-駅ID.拡張子」（例 32-takaoka.png、英小文字）になっていません")
    code, sid, force = m.group(1), m.group(2).lower(), m.group(3) == "!"
    if code not in comp:
        raise ValueError(f"会社番号 {code} の会社はありません（対象: {', '.join(sorted(comp))}）")
    folder = comp[code]
    yml = folder / f"{sid}.yaml"
    if not yml.is_file():
        raise ValueError(f"{folder.relative_to(DATA).as_posix()} に駅ID「{sid}」の駅データがありません（駅ページURLの末尾と同じIDにしてください）")
    st = yaml.safe_load(yml.read_text(encoding="utf-8"))
    current = st.get("画像")
    if current and str(current).startswith("images/") and (folder / str(current)).is_file() and not force:
        raise Skip(f"すでに写真（{current}）があるので上書きしませんでした。差し替えるときは {code}-{sid}!{path.suffix} の名前で出してください")

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)  # 回転情報どおりに向きを直してから
        w, h = im.size
        if abs((w / h) / (4 / 3) - 1) > ASPECT_TOLERANCE:
            raise ValueError(f"縦横比が4:3ではありません（{w}×{h}）。4:3（横長）に切り抜いてから出してください")
        out = im.convert("RGB").resize(SIZE, Image.LANCZOS)  # 新しい画像として作るのでメタデータは付かない

    if current and str(current).startswith("images/"):
        rel = str(current)  # 既に写真がある駅は同じファイルを差し替える（例: san_takamatsu → images/takamatsu.png）
    else:
        rel = f"images/{sid}.png"
    dest = folder / rel
    existed = dest.is_file()
    dest.parent.mkdir(exist_ok=True)
    out.save(dest, "PNG")
    if not current:
        set_image_field(yml, rel)
    path.unlink()
    return f"{folder.relative_to(DATA).as_posix()}/{sid}（{st.get('駅名', '')}）{'差し替え' if existed else '新規'}"


def main():
    files = sorted(p for p in INBOX.glob("*") if p.is_file() and not p.name.startswith("."))
    if not files:
        print("inbox は空です")
        report([], [], [])
        return
    comp = companies()
    done, failed, skipped = [], [], []
    for p in files:
        try:
            done.append(import_one(p, comp))
        except Skip as e:
            SKIPPED.mkdir(exist_ok=True)
            shutil.move(str(p), SKIPPED / p.name)
            (SKIPPED / f"{p.name}.txt").write_text(f"{e}\n", encoding="utf-8")
            skipped.append(f"{p.name}: {e}")
        except Exception as e:  # 1枚の失敗で全体を止めない
            ERROR.mkdir(exist_ok=True)
            shutil.move(str(p), ERROR / p.name)
            (ERROR / f"{p.name}.txt").write_text(f"{e}\n", encoding="utf-8")
            failed.append(f"{p.name}: {e}")
    report(done, failed, skipped)


def report(done, failed, skipped):
    lines = [f"取り込み {len(done)} 枚、既存の写真があり上書きしなかった {len(skipped)} 枚、取り込めなかった {len(failed)} 枚"]
    lines += [f"- ○ {x}" for x in done] + [f"- − {x}" for x in skipped] + [f"- × {x}" for x in failed]
    print("\n".join(lines))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## 駅写真の取り込み\n" + "\n".join(lines) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"done={len(done)}\nerrors={len(failed)}\nskipped={len(skipped)}\n")
            f.write("names=" + "、".join(x.split("（")[0].rsplit("/", 1)[-1] for x in done)[:200] + "\n")


if __name__ == "__main__":
    main()
