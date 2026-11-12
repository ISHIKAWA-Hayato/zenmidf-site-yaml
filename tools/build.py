"""駅データYAMLからサイトのHTMLを生成する。

使い方: python tools/build.py   （data/ と templates/ を読み、dist/ に書き出す）
- 駅ページ: data/<会社フォルダ>/<駅>.yaml → dist/station-individual/<会社フォルダ>/<駅>.html
- 会社一覧: data/<会社フォルダ>/_index.yaml → 一覧ページ（アイコンは駅データとアイコン規則から自動判定）
  他社の駅は {他社駅: <会社フォルダ>/<駅ID>} と書くと、その会社の駅データ・アイコン規則から自動で出る
- 駅写真: data/<会社フォルダ>/images/ → dist/station-individual/<会社フォルダ>/images/ にそのままコピー
- 駅の一覧: dist/station-individual/stations.json（会社番号・駅ID・駅名・よみ・ローマ字・座標・全国の同名駅の数）。
  共有ドライブの写真から駅を当てる GAS が読む。座標は data/_coords.csv（tools/make_coords.py で作る）
- 会社はグループの下にも置ける: data/99-Others/<会社フォルダ>/ → dist/station-individual/99-Others/<会社フォルダ>/
  グループの入口ページは data/<グループ>/_portal.yaml から作る（配下の会社を並べる）
- トップ階層のページ: pages/**/*.html → dist/top/ （ホーム・About US・エラーページなど。本文はHTMLのまま）
  HTML以外のファイル（pages/images/ の画像など）はそのままコピーする
  「<!-- メニュー -->」と書いた所に site.yaml のメニューが入る（ホームは「<!-- メニュー（ホーム以外） -->」）
"""
import csv
import json
import pathlib
import posixpath
import re
import shutil
import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA, TPL, DIST = ROOT / "data", ROOT / "templates", ROOT / "dist"


def in_service(fac):
    """一覧のアイコン判定に数える設備か。
    数えない場所：営業時間が「-」（裏マルスなど）／場所名が「#」、または営業時間も端末も未記入の雛形のまま"""
    places = fac.get("場所") or [fac]

    def counts(pl):
        lines = pl.get("説明", []) + [x for c in pl.get("内容", []) for x in c.get("説明", [])]
        hours = [str(x.get("営業時間", x.get("時間", ""))).strip() for x in lines if "営業時間" in x or "時間" in x]
        rows = pl.get("端末", []) + [r for c in pl.get("内容", []) for r in c.get("端末", [])]
        if hours and all(h == "-" for h in hours):
            return False
        blank_hours = not hours or all(h in ("～", "", "#") for h in hours)
        blank_rows = not rows or all(str(r.get("機種", "#")) == "#" for r in rows)
        if pl.get("名称") == "#" or (blank_hours and blank_rows):
            return False
        return True

    return any(counts(pl) for pl in places)


def models_in_service(fac):
    """数える場所（in_service と同じ基準）にある端末の機種。撤去情報の表は除く"""
    out = []
    for pl in fac.get("場所") or [fac]:
        if not isinstance(pl, dict) or "摘要" in (pl.get("列") or []) or not in_service({"場所": [pl]}):
            continue
        rows = pl.get("端末", []) + [r for c in pl.get("内容", []) for r in c.get("端末", [])]
        out += [str(r.get("機種", "")) for r in rows if isinstance(r, dict)]
    return out


def derive_icons(st, rules):
    """アイコン規則 [[枠番号, アイコン, [設備名 ...], [除外する設備名 ...](省略可), [機種 ...](省略可)], ...] から6枠を決める。
    設備名・機種の末尾 * は前方一致。機種を書いた規則は、その設備にその機種の端末がある場合だけ当てはまる"""
    facs = [f for f in st["設備"] if in_service(f)]

    def match(name, pattern):
        return name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern

    def hit(pattern, models=None):
        return any(match(f["名称"], pattern) and (not models or any(match(m, mp) for m in models_in_service(f) for mp in models))
                   for f in facs)

    icons = ["none"] * 6
    for rule in rules:
        slot, icon, patterns = rule[:3]
        unless = rule[3] if len(rule) > 3 else []  # この設備がある駅では出さない
        models = rule[4] if len(rule) > 4 else None  # この機種の端末がある設備だけ数える
        if any(hit(p, models) for p in patterns) and not any(hit(p) for p in unless):
            icons[slot - 1] = icon
    return icons


def page_ctx(rel, site):
    """階層の深さに応じた相対パスと、グループのパンくず"""
    depth = len(rel.parts) + 1  # station-individual/ の分
    group = []
    if len(rel.parts) > 1:
        portal = yaml.safe_load((DATA / rel.parent / "_portal.yaml").read_text(encoding="utf-8"))
        group = [(portal["名称"], f"{site['トップ']}/station-individual/{rel.parent.as_posix()}/{portal['ファイル名']}")]
    return {"root": "../" * depth, "si": "../" * (depth - 1), "group": group}


def load_company(folder):
    idx = yaml.safe_load((folder / "_index.yaml").read_text(encoding="utf-8"))
    stations = {}
    for f in sorted(folder.glob("*.yaml")):
        if not f.name.startswith("_"):
            stations[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))
    return idx, stations


def build_company(folder, env, site, everyone):
    """everyone: {会社フォルダ(data/からの相対): (一覧データ, {駅ID: 駅データ})}。他社駅の解決に使う"""
    rel = folder.relative_to(DATA)
    idx, stations = everyone[rel.as_posix()]
    ctx = page_ctx(rel, site)
    out = DIST / "station-individual" / rel
    out.mkdir(parents=True, exist_ok=True)
    if (folder / "images").is_dir():
        shutil.copytree(folder / "images", out / "images", dirs_exist_ok=True)

    t = env.get_template("station.html")
    for slug, st in stations.items():
        (out / f"{slug}.html").write_text(t.render(site=site, company=idx, st=st, **ctx), encoding="utf-8")

    def view(line):
        items = []
        for e in line["駅"]:
            if isinstance(e, str):
                e = {"駅": e}
            if "駅" in e:  # 駅データのある駅：表示名とアイコンはデータから（上書き指定があればそちら）
                st = stations[e["駅"]]
                items.append({"href": f"{e['駅']}.html", "name": e.get("表示名", st["駅名"]),
                              "icons": e.get("アイコン") or derive_icons(st, idx["アイコン規則"])})
            elif "他社駅" in e:  # 他社フォルダの駅：その会社の駅データとアイコン規則から
                other, sid = e["他社駅"].rsplit("/", 1)
                if other not in everyone or sid not in everyone[other][1]:
                    raise SystemExit(f"{rel.as_posix()}/_index.yaml の {line['名称']}: 他社駅「{e['他社駅']}」の駅データがありません")
                oidx, ost = everyone[other][0], everyone[other][1][sid]
                items.append({"href": posixpath.relpath(f"{other}/{sid}.html", rel.as_posix()),
                              "name": e.get("表示名", ost["駅名"]),
                              "icons": e.get("アイコン") or derive_icons(ost, oidx["アイコン規則"])})
            else:  # 駅データのないリンク：直接指定
                items.append({"href": e["リンク"], "name": e["表示名"], "icons": e["アイコン"]})
        return {**line, "stations": items}

    if "エリア" in idx:
        areas = [{**a, "lines": [view(l) for l in a["路線"]]} for a in idx["エリア"]]
        lines = None
    else:
        areas, lines = None, [view(l) for l in idx["路線"]]
    html = env.get_template("company.html").render(site=site, company=idx, lines=lines, areas=areas, **ctx)
    (out / idx["ファイル名"]).write_text(html, encoding="utf-8")
    return len(stations)


def build_portal(folder, env, site):
    """グループの入口ページ（例: 私鉄・三セク）。配下の会社をフォルダ名順に並べる"""
    portal = yaml.safe_load((folder / "_portal.yaml").read_text(encoding="utf-8"))
    rel = folder.relative_to(DATA)
    companies = []
    for sub in sorted(p for p in folder.iterdir() if (p / "_index.yaml").is_file()):
        idx = yaml.safe_load((sub / "_index.yaml").read_text(encoding="utf-8"))
        companies.append((f"{sub.name}/{idx['ファイル名']}", idx.get("入口表示名", idx["会社名"])))
    out = DIST / "station-individual" / rel
    out.mkdir(parents=True, exist_ok=True)
    html = env.get_template("portal.html").render(site=site, portal=portal, companies=companies, **page_ctx(rel, site))
    (out / portal["ファイル名"]).write_text(html, encoding="utf-8")
    return len(companies)


JR_NAMES = {"01-Hokkaido": "JR北海道", "02-East-Japan": "JR東日本", "03-Central-Japan": "JR東海",
            "04-West-Japan": "JR西日本", "05-Shikoku": "JR四国", "06-Kyushu": "JR九州"}


def write_station_list(everyone):
    """GAS（写真の駅の自動判定）が読む駅の一覧"""
    coords, dups, nopage = {}, {}, []
    path = DATA / "_coords.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                if not r.get("駅ID"):
                    if r.get("緯度") and r.get("経度") and r.get("照合") != "除外":
                        nopage.append(r)   # 駅ページのない駅（写真がその駅なら GAS が「未実装」に回す）
                    continue
                if r.get("緯度") and r.get("経度"):
                    coords[(r["会社フォルダ"], r["駅ID"])] = (float(r["緯度"]), float(r["経度"]))
                if (r.get("同名の駅") or "1").isdigit() and int(r.get("同名の駅") or 1) > 1:
                    dups[(r["会社フォルダ"], r["駅ID"])] = int(r["同名の駅"])
    out = []
    for folder, (idx, stations) in sorted(everyone.items()):
        code = re.match(r"^(\d{2})-", folder.rsplit("/", 1)[-1])
        if not code:
            continue
        for sid, st in stations.items():
            e = {"code": code.group(1), "folder": folder, "id": sid, "name": st.get("駅名", ""),
                 "yomi": st.get("よみ", ""), "roman": st.get("ローマ字", "")}
            if (folder, sid) in coords:
                e["lat"], e["lon"] = coords[(folder, sid)]
            if (folder, sid) in dups:
                e["dup"] = dups[(folder, sid)]  # 全国に同名の駅がある（位置情報なしの OCR だけでは決めない）
            out.append(e)
    names = {f: idx.get("会社名", f) for f, (idx, _) in everyone.items()}
    for r in nopage:
        folder = r["会社フォルダ"]
        e = {"code": folder.rsplit("/", 1)[-1][:2], "folder": folder, "id": "", "name": r["駅名"],
             "company": names.get(folder) or JR_NAMES.get(folder, folder), "line": r.get("路線", ""),
             "lat": float(r["緯度"]), "lon": float(r["経度"])}
        if (r.get("同名の駅") or "1").isdigit() and int(r.get("同名の駅") or 1) > 1:
            e["dup"] = int(r["同名の駅"])
        out.append(e)
    (DIST / "station-individual" / "stations.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(out) - len(nopage), sum(1 for e in out if "lat" in e and e["id"]), len(nopage)


def menu_html(site, skip_home=False):
    items = [(label, url) for label, url in site["メニュー"] if not (skip_home and url.rstrip("/") == site["トップ"].rstrip("/"))]
    return "<ul>\n" + "".join(f'<li><a href="{url}">{label}</a></li>\n' for label, url in items) + "</ul>"


def build_pages(site):
    """トップ階層のページ。本文はそのまま、メニューの目印だけ site.yaml のメニューに置き換える"""
    src = ROOT / "pages"
    marks = {"<!-- メニュー -->": menu_html(site), "<!-- メニュー（ホーム以外） -->": menu_html(site, skip_home=True)}
    n = 0
    for f in sorted(src.rglob("*.html")):
        html = f.read_text(encoding="utf-8")
        if sum(html.count(m) for m in marks) != 1:
            raise SystemExit(f"pages/{f.relative_to(src).as_posix()}: メニューの目印（<!-- メニュー -->）がちょうど1つ必要です")
        for m, menu in marks.items():
            html = html.replace(m, menu)
        out = DIST / "top" / f.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8", newline="\n")
        n += 1
    for f in sorted(p for p in src.rglob("*") if p.is_file() and p.suffix != ".html"):
        out = DIST / "top" / f.relative_to(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)
    return n


def main():
    site = yaml.safe_load((ROOT / "site.yaml").read_text(encoding="utf-8"))
    env = Environment(loader=FileSystemLoader(TPL), trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    folders = sorted(p.parent for p in DATA.rglob("_index.yaml"))
    everyone = {f.relative_to(DATA).as_posix(): load_company(f) for f in folders}
    for folder in folders:
        n = build_company(folder, env, site, everyone)
        print(f"{folder.relative_to(DATA).as_posix()}: 駅ページ {n} 枚 + 一覧 1 枚")
    for folder in sorted(p.parent for p in DATA.rglob("_portal.yaml")):
        n = build_portal(folder, env, site)
        print(f"{folder.relative_to(DATA).as_posix()}: 入口ページ（{n} 社）")
    n, located, nopage = write_station_list(everyone)
    print(f"stations.json: {n} 駅（座標あり {located}）＋ ページのない駅 {nopage}")
    if (ROOT / "pages").is_dir():
        print(f"トップ階層のページ: {build_pages(site)} 枚")


if __name__ == "__main__":
    main()
