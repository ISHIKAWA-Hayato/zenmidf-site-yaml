"""国土数値情報「鉄道」（N02）の駅データから、駅データの駅IDごとの座標表 data/_coords.csv を作る。

使い方: python tools/make_coords.py <N02-xx_GML.zip または N02-xx_Station.geojson>
  - 会社（駅データの「会社」と N02 の「運営会社」）と駅名で突き合わせる
  - 1つの駅に複数のホーム（路線）があるときは、その平均の位置
  - それでも決まらない駅（同じ会社に同名の駅が離れて2つある、会社が変わって名前が他社と重なる など）は、
    一覧（_index.yaml）で隣にある駅の座標に一番近い候補を採る
  - 突き合わせられなかった駅は、表の最後に一覧する（Googleマップで調べて手で書き足す）
  - 表の「照合」が「手入力」の行は、作り直しても上書きしない
座標表は、写真の位置情報から駅を当てる（GAS）ための stations.json を build.py が作るときに使う。
"""
import csv
import io
import json
import math
import pathlib
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "_coords.csv"
COLUMNS = ["会社フォルダ", "駅ID", "駅名", "緯度", "経度", "照合", "同名の駅"]  # 同名の駅＝全国でその駅名がある場所の数（会社を問わず。共用駅は1か所）
SPREAD_KM = 3.0      # 同じ会社・同じ駅名のホームがこれ以上離れていたら別の駅とみなす
NEIGHBOR_KM = 40.0   # 隣の駅からこれ以上離れた候補は採らない


def norm_company(s):
    return re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", str(s or ""))).lower()


def norm_station(s):
    s = unicodedata.normalize("NFKC", str(s or "")).strip()
    s = re.sub(r"^\([^)]*\)|^[^)]{1,2}\)", "", s)  # 先頭の（讃）（臨）や 臨） を外す
    s = re.sub(r"\([^)]*\)$", "", s)              # 末尾の（在来線）などを外す
    s = re.sub(r"駅$", "", s)
    return s.replace("ヶ", "ケ").replace("ヵ", "ケ").replace(" ", "")


def km(a, b):
    dy = (a[0] - b[0]) * 111.0
    dx = (a[1] - b[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def load_n02(path):
    path = pathlib.Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if re.search(r"Station\.geojson$", n, re.I)]
            if not names:
                sys.exit("zip の中に *_Station.geojson が見つかりません（GeoJSON を含む版をダウンロードしてください）")
            names.sort(key=lambda n: ("UTF-8" not in n, n))
            return json.load(io.TextIOWrapper(z.open(names[0]), encoding="utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))


def centroid(geom):
    pts = []

    def walk(c):
        if isinstance(c[0], (int, float)):
            pts.append(c)
        else:
            for x in c:
                walk(x)
    walk(geom["coordinates"])
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))  # (緯度, 経度)


def cluster(points):
    """近い（SPREAD_KM 以内の）位置をまとめ、まとまりごとの平均位置を返す"""
    groups = []
    for p in points:
        for g in groups:
            if km(g[0], p) <= SPREAD_KM:
                g.append(p)
                break
        else:
            groups.append([p])
    return [(sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g)) for g in groups]


def neighbor_map():
    """(会社フォルダ, 駅ID) → 一覧の路線で前後に並ぶ駅（同じ会社フォルダの駅だけ）"""
    out = defaultdict(set)
    for idx in DATA.rglob("_index.yaml"):
        folder = idx.parent.relative_to(DATA).as_posix()
        d = yaml.safe_load(idx.read_text(encoding="utf-8"))
        lines = d.get("路線") or [l for a in d.get("エリア", []) for l in a["路線"]]
        for line in lines:
            ids = [e if isinstance(e, str) else e.get("駅") for e in line["駅"]]
            for i, sid in enumerate(ids):
                if not sid:
                    continue
                for j in (i - 1, i + 1):
                    if 0 <= j < len(ids) and ids[j]:
                        out[(folder, sid)].add((folder, ids[j]))
    return out


def main(src):
    gj = load_n02(src)
    by_company = defaultdict(lambda: defaultdict(list))  # 会社 → 駅名 → [位置]
    by_name = defaultdict(list)                          # 駅名 → [(会社, 位置)]
    for f in gj["features"]:
        p = f["properties"]
        company, name = p.get("N02_004"), p.get("N02_005")
        if not company or not name or not f.get("geometry"):
            continue
        pos = centroid(f["geometry"])
        by_company[norm_company(company)][norm_station(name)].append(pos)
        by_name[norm_station(name)].append((company, pos))

    keep = {}
    if OUT.exists():
        with OUT.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("照合") == "手入力":
                    keep[(r["会社フォルダ"], r["駅ID"])] = r

    rows, todo, pending = [], [], {}
    for yml in sorted(DATA.rglob("*.yaml")):
        if yml.name.startswith("_"):
            continue
        folder = yml.parent.relative_to(DATA).as_posix()
        st = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        key = (folder, yml.stem)
        if key in keep:
            rows.append(keep[key])
            continue
        name = norm_station(st.get("駅名"))
        company = norm_company(st.get("会社"))
        cands = [c for c in by_company if c == company or company in c or c in company]
        pts = [pos for c in cands for pos in by_company[c].get(name, [])]
        how = "会社と駅名"
        # 会社で見つからないときは全国の同名の駅を候補にして、2回目（隣の駅との距離）で確かめる
        # （改名・会社の移管で、別の会社の同名の駅を拾わないように。例: 江北＝旧肥前山口と東京の江北）
        lat = lon = None
        if pts:
            lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        if not pts or max(km((lat, lon), p) for p in pts) > SPREAD_KM:
            # 2回目（隣の駅との距離）に回す。候補は会社が合うもの、なければ全国の同名の駅
            pending[key] = (st.get("駅名"), [(st.get("会社"), p) for p in pts] or list(by_name.get(name, [])))
            rows.append({"会社フォルダ": folder, "駅ID": yml.stem, "駅名": st.get("駅名"), "緯度": "", "経度": "", "照合": "未照合"})
            continue
        rows.append({"会社フォルダ": folder, "駅ID": yml.stem, "駅名": st.get("駅名"),
                     "緯度": f"{lat:.6f}", "経度": f"{lon:.6f}", "照合": how})

    # 2回目：一覧で隣にある駅の座標に一番近い候補を採る（決まった駅がまた隣の基準になるので、変わらなくなるまで繰り返す）
    neighbors = neighbor_map()
    byrow = {(r["会社フォルダ"], r["駅ID"]): r for r in rows}

    def located():
        return {k: (float(r["緯度"]), float(r["経度"])) for k, r in byrow.items() if r["緯度"]}

    def by_neighbors():
        changed = True
        while changed:
            changed = False
            loc = located()
            for key in [k for k in pending if not byrow[k]["緯度"]]:
                near = [loc[n] for n in neighbors.get(key, ()) if n in loc]
                clusters = cluster([p for _, p in pending[key][1]])
                if not near or not clusters:
                    continue
                ref = (sum(p[0] for p in near) / len(near), sum(p[1] for p in near) / len(near))
                ranked = sorted(clusters, key=lambda c: km(ref, c))
                d0 = km(ref, ranked[0])
                if d0 <= NEIGHBOR_KM and (len(ranked) == 1 or km(ref, ranked[1]) > 2 * d0):
                    r = byrow[key]
                    r["緯度"], r["経度"], r["照合"] = f"{ranked[0][0]:.6f}", f"{ranked[0][1]:.6f}", "駅名＋隣の駅"
                    changed = True

    by_neighbors()
    # 基準になる隣の駅がない（会社ごとデータにない）ときは、JR の同名の駅が全国に1か所だけならそれを採る
    # （JR から移管された三セクの駅。JR 以外の同名の駅は採らない）
    for key in [k for k in pending if not byrow[k]["緯度"]]:
        jr = cluster([p for c, p in pending[key][1] if "旅客鉄道" in str(c)])
        if len(jr) == 1:
            r = byrow[key]
            r["緯度"], r["経度"], r["照合"] = f"{jr[0][0]:.6f}", f"{jr[0][1]:.6f}", "駅名（JRの同名駅）"
    by_neighbors()
    for key in [k for k in pending if not byrow[k]["緯度"]]:
        label, cands = pending[key]
        n = len(cluster([p for _, p in cands]))
        todo.append(f"{key[0]}/{key[1]}（{label}）: " + ("見つからない" if not cands else f"同名の駅が {n} か所あり決まらない"))

    # 全国で同じ駅名がいくつの場所にあるか（OCR だけで駅を決めてよいかの判断に使う）
    for r in rows:
        n = len(cluster([pos for _, pos in by_name.get(norm_station(r["駅名"]), [])]))
        r["同名の駅"] = str(max(n, 1))

    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    filled = sum(1 for r in rows if r["緯度"])
    print(f"{OUT.relative_to(ROOT)}: {len(rows)} 駅中 {filled} 駅に座標（手入力 {len(keep)}）")
    if todo:
        print(f"\n手で座標を入れる必要がある駅 {len(todo)}（緯度・経度を書き、照合を「手入力」にする）:")
        for t in todo:
            print("  " + t)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
