"""国土数値情報「鉄道」（N02）の駅データから、駅データの駅IDごとの座標表 data/_coords.csv を作る。

使い方: python tools/make_coords.py <N02-xx_GML.zip または N02-xx_Station.geojson>
  - 会社（駅データの「会社」と N02 の「運営会社」）と駅名で突き合わせる
  - 1つの駅に複数のホーム（路線）があるときは、その平均の位置
  - それでも決まらない駅（同じ会社に同名の駅が離れて2つある、会社が変わって名前が他社と重なる など）は、
    一覧（_index.yaml）で隣にある駅の座標に一番近い候補を採る
  - 突き合わせられなかった駅は、表の最後に一覧する（Googleマップで調べて手で書き足す）
  - 表の「照合」が「手入力」の行は、作り直しても上書きしない
  - 駅ページのない駅（JR全6社と data/99-Others の会社の、国土数値情報にある駅すべて）も「駅ID」を空にして載せる。
    写真がページのない駅で撮られたとき、近くのページのある駅に誤って付かないようにするため（GAS は「未実装」に回す）
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
COLUMNS = ["会社フォルダ", "駅ID", "駅名", "緯度", "経度", "照合", "同名の駅", "路線"]  # 同名の駅＝全国でその駅名がある場所の数（会社を問わず。共用駅は1か所）
NOPAGE = "国土数値情報（ページなし）"   # 駅ページのない駅の行の「照合」

# 国土数値情報の版が会社の移管より古いときの付け替え：(運営会社, 路線, 起点の駅, 終点の駅, [(境目の駅, 移管先の会社フォルダ), ...])
# 起点〜終点の間の駅を、境目の駅ごとに移管先へ付け替える（境目の駅からその先が次の会社）。起点・終点の駅は元の会社にも残す
# 2024年3月：北陸線 敦賀〜大聖寺 → ハピラインふくい、大聖寺〜金沢 → IRいしかわ鉄道
TRANSFERS = [("西日本旅客鉄道", "北陸線", "敦賀", "金沢",
              [("敦賀", "99-Others/36-Hapiline-Fukui"), ("大聖寺", "99-Others/31-IR-Ishikawa")])]

# 座標表に全駅を載せる会社：(会社フォルダ, 国土数値情報の運営会社名)。data/99-Others の会社は駅データの「会社」から足す
JR = [("01-Hokkaido", "北海道旅客鉄道"), ("02-East-Japan", "東日本旅客鉄道"), ("03-Central-Japan", "東海旅客鉄道"),
      ("04-West-Japan", "西日本旅客鉄道"), ("05-Shikoku", "四国旅客鉄道"), ("06-Kyushu", "九州旅客鉄道")]
SPREAD_KM = 3.0      # 同じ会社・同じ駅名のホームがこれ以上離れていたら別の駅とみなす
NEIGHBOR_KM = 40.0   # 隣の駅からこれ以上離れた候補は採らない


def norm_company(s):
    return re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", str(s or ""))).lower()


def norm_station(s):
    s = unicodedata.normalize("NFKC", str(s or "")).strip()
    s = re.sub(r"^\([^)]*\)|^[^)]{1,2}\)", "", s)  # 先頭の（讃）（臨）や 臨） を外す
    s = re.sub(r"\([^)]*\)$", "", s)              # 末尾の（在来線）などを外す
    s = re.sub(r"駅$", "", s)
    return s.replace("ヶ", "ケ").replace("ヵ", "ケ").replace("祗", "祇").replace(" ", "")   # 祗/祇 は表記ゆれ（下祇園）


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
            if "駅" not in line:
                continue   # 路線の間の注記（HTML:）
            ids = [e if isinstance(e, str) else e.get("駅") for e in line["駅"]]
            for i, sid in enumerate(ids):
                if not sid:
                    continue
                for j in (i - 1, i + 1):
                    if 0 <= j < len(ids) and ids[j]:
                        out[(folder, sid)].add((folder, ids[j]))
    return out


def target_companies():
    """(会社フォルダ, 運営会社名の候補[]) の一覧。JR 6社と、data/99-Others にある会社"""
    out = [(f, [n]) for f, n in JR]
    for idx in sorted((DATA / "99-Others").glob("*/_index.yaml")):
        d = yaml.safe_load(idx.read_text(encoding="utf-8"))
        names = {d.get("会社名", "")}
        names |= set(re.findall(r"（([^）]+)）", d.get("会社名", "")))   # 「WILLER TRAINS（京都丹後鉄道）」の括弧内も候補に
        for yml in idx.parent.glob("*.yaml"):
            if not yml.name.startswith("_"):
                names.add((yaml.safe_load(yml.read_text(encoding="utf-8")) or {}).get("会社", ""))
                break
        names = {re.sub(r"（[^）]*）", "", n).strip() for n in names} | names
        out.append((idx.parent.relative_to(DATA).as_posix(), sorted(n for n in names if n)))
    return out


def transfer_plan(features):
    """付け替えの基準になる駅の位置を国土数値情報から拾い、駅 → [移管先] を返す関数を作る"""
    plans = []
    for company, line, start, end, splits in TRANSFERS:
        pos = defaultdict(list)
        for f in features:
            p = f["properties"]
            if p.get("N02_004") == company and p.get("N02_003") == line and f.get("geometry"):
                pos[norm_station(p.get("N02_005"))].append(centroid(f["geometry"]))
        avg = {n: (sum(q[0] for q in v) / len(v), sum(q[1] for q in v) / len(v)) for n, v in pos.items()}
        if norm_station(start) not in avg or norm_station(end) not in avg:
            continue   # この版には基準の駅がない（移管後の版など）→ 付け替えない
        a, b = avg[norm_station(start)], avg[norm_station(end)]

        def t(q, a=a, b=b):   # 起点→終点の向きに沿った位置（0＝起点、1＝終点）
            vx, vy = b[1] - a[1], b[0] - a[0]
            return ((q[1] - a[1]) * vx + (q[0] - a[0]) * vy) / (vx * vx + vy * vy)
        cuts = [(t(avg[norm_station(s)]) if norm_station(s) in avg else None, folder) for s, folder in splits]
        plans.append((company, line, norm_station(start), norm_station(end), t, cuts))

    def dest(company, line, name, q):
        for c, l, start, end, t, cuts in plans:
            if company != c or line != l:
                continue
            x = t(q)
            if name in (start, end):
                # 起点・終点は元の会社に残し、隣り合う移管先にも載せる
                return [None, cuts[0][1] if name == start else cuts[-1][1]]
            if not (0 < x < 1):
                return [None]
            folder = None
            for cx, f in cuts:
                if cx is not None and x >= cx - 1e-9:
                    folder = f
            return [folder]
        return [None]
    return dest


def company_matches(n02_company, names):
    c = norm_company(n02_company)
    return any(norm_company(n) and (norm_company(n) == c or norm_company(n) in c or c in norm_company(n)) for n in names)


def main(src):
    gj = load_n02(src)
    targets = target_companies()
    every = defaultdict(list)                            # (会社フォルダ, 駅名) → [(位置, 元の駅名, 路線)]（全駅を載せる会社だけ）
    by_company = defaultdict(lambda: defaultdict(list))  # 会社 → 駅名 → [位置]
    by_name = defaultdict(list)                          # 駅名 → [(会社, 位置)]
    dest = transfer_plan(gj["features"])
    yaml_company = {}
    for folder, names in targets:
        d = DATA / folder
        for yml in (sorted(d.glob("*.yaml")) if d.is_dir() else []):
            if not yml.name.startswith("_"):
                yaml_company[folder] = (yaml.safe_load(yml.read_text(encoding="utf-8")) or {}).get("会社", "")
                break
    for f in gj["features"]:
        p = f["properties"]
        company, name = p.get("N02_004"), p.get("N02_005")
        if not company or not name or not f.get("geometry"):
            continue
        pos = centroid(f["geometry"])
        line = p.get("N02_003") or ""
        by_name[norm_station(name)].append((company, pos))
        for moved in dest(company, line, norm_station(name), pos):
            if moved is None:   # 元の会社のまま
                by_company[norm_company(company)][norm_station(name)].append(pos)
                for folder, names in targets:
                    if company_matches(company, names):
                        every[(folder, norm_station(name))].append((pos, name, line))
                        break
            else:               # 移管先の会社の駅として扱う（路線名は移管先の会社名にする）
                by_company[norm_company(yaml_company.get(moved, moved))][norm_station(name)].append(pos)
                every[(moved, norm_station(name))].append((pos, name, "旧" + line))

    keep = {}
    if OUT.exists():
        with OUT.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("照合") in ("手入力", "除外"):   # 除外＝ページのない駅の行を載せない（改名前の名前が残っている など）
                    keep[(r["会社フォルダ"], r["駅ID"] or "名:" + r["駅名"])] = r

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

    # 駅ページのない駅：全駅を載せる会社の駅のうち、同じ会社フォルダの同名のページの駅（3km 以内）に当たらないもの
    pages = defaultdict(list)
    for r in rows:
        if r["緯度"]:
            pages[(r["会社フォルダ"], norm_station(r["駅名"]))].append((float(r["緯度"]), float(r["経度"])))
    unlocated = {(r["会社フォルダ"], norm_station(r["駅名"])) for r in rows if r["駅ID"] and not r["緯度"]}
    added, suspect = 0, []
    page_pts = [(r, (float(r["緯度"]), float(r["経度"]))) for r in rows if r["緯度"]]
    for (folder, name), items in sorted(every.items()):
        groups = []
        for pos, raw, line in items:
            for g in groups:
                if km(g["c"], pos) <= SPREAD_KM:
                    g["p"].append(pos); g["l"].add(line)
                    g["c"] = (sum(p[0] for p in g["p"]) / len(g["p"]), sum(p[1] for p in g["p"]) / len(g["p"]))
                    break
            else:
                groups.append({"c": pos, "p": [pos], "l": {line}, "raw": raw})
        for g in groups:
            if any(km(g["c"], p) <= SPREAD_KM for p in pages.get((folder, name), [])):
                continue   # ページのある駅と同じ駅
            if (folder, name) in unlocated:
                continue   # 同じ名前のページの駅の座標がまだ決まっていない（どちらがその駅か分からないので載せない）
            if ("名:" + g["raw"]) in {k[1] for k in keep if k[0] == folder}:
                continue   # 手入力の行がある
            rows.append({"会社フォルダ": folder, "駅ID": "", "駅名": g["raw"], "緯度": f"{g['c'][0]:.6f}",
                         "経度": f"{g['c'][1]:.6f}", "照合": NOPAGE, "路線": "・".join(sorted(l for l in g["l"] if l))})
            added += 1
            # 同じ会社のページのある駅のすぐ近く（300m 以内）にある名前違いの駅は、改名などで突き合わせ損ねた疑い
            for r, p in page_pts:
                if r["会社フォルダ"] == folder and km(g["c"], p) <= 0.3:
                    suspect.append(f"{folder}: ページなし「{g['raw']}」が ページあり「{r['駅名']}」（{r['駅ID']}）から {km(g['c'], p) * 1000:.0f}m")
    for k, r in keep.items():
        if k[1].startswith("名:"):
            rows.append(r)

    # 全国で同じ駅名がいくつの場所にあるか（OCR だけで駅を決めてよいかの判断に使う）
    for r in rows:
        n = len(cluster([pos for _, pos in by_name.get(norm_station(r["駅名"]), [])]))
        r["同名の駅"] = str(max(n, 1))

    rows.sort(key=lambda r: (r["会社フォルダ"], r["駅ID"] == "", r["駅ID"], r["駅名"]))
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    filled = sum(1 for r in rows if r["緯度"] and r["駅ID"])
    print(f"{OUT.relative_to(ROOT)}: ページのある駅 {sum(1 for r in rows if r['駅ID'])} 駅中 {filled} 駅に座標（手入力 {len(keep)}）、"
          f"ページのない駅 {added} 駅")
    for folder, names in targets:
        n = sum(1 for r in rows if r["会社フォルダ"] == folder)
        print(f"  {folder}: {n} 駅" + ("" if n else f"  ← 国土数値情報に見つからない（運営会社名の候補: {'、'.join(names)}）"))
    if suspect:
        print(f"\n改名などで突き合わせ損ねた疑いのある駅 {len(suspect)}（同じ駅なら、ページのない駅の行の「照合」を「除外」にする）:")
        for t in suspect:
            print("  " + t)
    if todo:
        print(f"\n手で座標を入れる必要がある駅 {len(todo)}（緯度・経度を書き、照合を「手入力」にする）:")
        for t in todo:
            print("  " + t)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
