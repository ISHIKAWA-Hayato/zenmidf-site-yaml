"""本番サイト（zenmidf.jp）に出ている駅別ページを会社ごとに取ってくる。

使い方: python tools/fetch_live.py <出力フォルダ> <会社フォルダ> [<会社フォルダ> ...]
  例: python tools/fetch_live.py live 01-Hokkaido 02-East-Japan 03-Central-Japan
- 会社の一覧ページ（<会社フォルダ>/<会社フォルダを小文字にしたもの>.html）と、そこからリンクされている
  同じフォルダの駅ページを <出力フォルダ>/<会社フォルダ>/ に保存する
- 他社フォルダへのリンク（../02-East-Japan/... など）は取らない
- 本番に上がっているページと生成したページを compare.py で見比べるときに使う（01〜03 を FTP から移したときの元データもこれで取った）
"""
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://www.zenmidf.jp/station-individual/"
UA = "zenmidf-site-yaml/fetch_live (+https://github.com/ISHIKAWA-Hayato/zenmidf-site-yaml)"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 2:
                raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(2)


def main(out, folders):
    out = pathlib.Path(out)
    total = 0
    for folder in folders:
        index_name = folder.lower() + ".html"
        html = get(BASE + folder + "/" + index_name)
        if html is None:
            sys.exit(f"{folder}: 一覧ページ {index_name} が見つかりません")
        d = out / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / index_name).write_bytes(html)
        text = html.decode("utf-8", "replace")
        main_part = text[text.find("<main"):text.find("</main>")] if "<main" in text else text
        names = sorted({h for h in re.findall(r'href="([^"#?]+\.html)"', main_part) if "/" not in h and h != index_name})
        missing = []
        for i, name in enumerate(names, 1):
            body = get(BASE + folder + "/" + name)
            if body is None:
                missing.append(name)
                continue
            (d / name).write_bytes(body)
            time.sleep(0.2)   # サーバーに負担をかけない
        total += len(names) - len(missing)
        print(f"{folder}: 一覧 1 枚 + 駅ページ {len(names) - len(missing)} 枚" + (f"（リンク切れ {len(missing)}: {' '.join(missing)}）" if missing else ""))
    print(f"合計 駅ページ {total} 枚")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])
