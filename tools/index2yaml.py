"""既存の会社一覧ページから _index.yaml を作る。

使い方: python tools/index2yaml.py <一覧HTML> <その会社の駅データフォルダ> <会社コード> <アイコン規則YAML> [keep]
- keep を付けると、アイコンが駅データからの自動判定と食い違う駅は、今の一覧のアイコンを上書き指定として残す
  （FTP からの移行の準備期間は本番と同じ見た目を保つため。付けなければ駅データに合わせる）
- 駅データがある駅は駅IDだけを書く（表示名・アイコンは駅データから自動）
- 自動で決まるものと今の一覧が食い違う場合は、今の一覧の値を上書き指定として残し、食い違いを表示する
- 他社フォルダの駅で駅データがあるものは {他社駅: <会社フォルダ>/<駅ID>}（表示名・アイコンはその駅のデータから）
- 駅データがないリンク（未作成の会社・リンク切れ）はリンク・表示名・アイコンを直接書く
"""
import sys, re, pathlib, posixpath, yaml
from bs4 import BeautifulSoup, Tag

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from html2yaml import clean, dump, Flow  # noqa: E402
from build import derive_icons  # noqa: E402


class FlowList(list):
    pass


yaml.add_representer(FlowList, lambda d, v: d.represent_sequence("tag:yaml.org,2002:seq", v, flow_style=True))


def icons_of(li):
    return [re.search(r"svg/(.*?)\.svg", i["src"]).group(1) for i in li.select(".station-icons-container img")]


def main(src, data_dir, code, rules_file, keep=None):
    keep = keep == "keep"
    html = pathlib.Path(src).read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    data_dir = pathlib.Path(data_dir)
    data_root = pathlib.Path(__file__).resolve().parent.parent / "data"
    here = data_dir.resolve().relative_to(data_root).as_posix()
    rules = yaml.safe_load(pathlib.Path(rules_file).read_text(encoding="utf-8"))
    stations = {f.stem: yaml.safe_load(f.read_text(encoding="utf-8")) for f in data_dir.glob("*.yaml") if not f.name.startswith("_")}
    containers = soup.select("main > .station-container")
    c = containers[0]
    legend = [FlowList([re.search(r"svg/(.*?)\.svg", tr.find("img")["src"]).group(1), tr.find("img")["alt"],
                        clean(tr.find("strong").get_text())]) for tr in c.select(".legend-table tbody tr")]
    report = {"アイコン食い違い": [], "表示名食い違い": [], "データなしのリンク": []}

    def line_of(sec):
        h3 = sec.find("h3")
        line = {"名称": clean(h3.get_text())}
        if h3.get("id"):
            line["ID"] = h3["id"]   # ページ内リンク（#saikyo-line など）の行き先
        ul = sec.find("ul")
        for note in sec.find_all("p", recursive=False):
            before = ul is None or any(x is ul for x in note.find_all_next("ul", limit=1))
            text = "".join(str(x) for x in note.contents).strip()
            line["注記" if before else "後注記"] = clean(text) if not note.find(True) else text
        items = []
        for li in sec.select("ul > li"):
            href, name, icons = li.a["href"], clean(li.span.get_text()), icons_of(li)
            slug = href.removesuffix(".html")
            if "/" not in href and slug in stations:
                st = stations[slug]
                e = {"駅": slug}
                if name != st["駅名"]:
                    e["表示名"] = name
                    report["表示名食い違い"].append(f"{slug}: 一覧「{name}」／駅ページ「{st['駅名']}」")
                derived = derive_icons(st, rules)
                if derived != icons:  # 一覧はデータに合わせる（変わる箇所を報告）。keep なら今の一覧のまま残す
                    report["アイコン食い違い"].append(f"{line['名称']} {slug}: 一覧{icons} → データから{derived}")
                    if keep:
                        e["アイコン"] = FlowList(icons)
                items.append(slug if len(e) == 1 else Flow(e))
            elif href.startswith("../") and (data_root / (tgt := posixpath.normpath(posixpath.join(here, href)))).with_suffix(".yaml").is_file():
                other, sid = tgt.rsplit("/", 1)
                sid = sid.removesuffix(".html")
                ost = yaml.safe_load((data_root / other / f"{sid}.yaml").read_text(encoding="utf-8"))
                oidx = yaml.safe_load((data_root / other / "_index.yaml").read_text(encoding="utf-8"))
                e = {"他社駅": f"{other}/{sid}"}
                if name != ost["駅名"]:
                    e["表示名"] = name
                derived = derive_icons(ost, oidx["アイコン規則"])
                if derived != icons:
                    report["アイコン食い違い"].append(f"{line['名称']} {other}/{sid}: 一覧{icons} → データから{derived}")
                    if keep:
                        e["アイコン"] = FlowList(icons)
                items.append(Flow(e))
            else:
                if not href.startswith("../"):
                    report["データなしのリンク"].append(f"{line['名称']}: {href}（{name}）")
                items.append({"リンク": href, "表示名": name, "アイコン": FlowList(icons)})
        line["駅"] = FlowList(items) if all(isinstance(x, str) for x in items) else items
        return line

    body, area = [], None
    for k in c.children:
        if not isinstance(k, Tag):
            continue
        if k.name == "h2" and k.get("id"):
            area = {"ID": k["id"], "名称": clean(k.get_text()), "路線": []}
            body.append(area)
        elif k.name == "details" and "train-line-section" in k.get("class", []):
            (area["路線"] if area else body).append(line_of(k))
        elif k.name == "p" and (body or area):   # 路線と路線の間の注記（「◆赤羽線は埼京線を参照」など）
            (area["路線"] if area else body).append({"HTML": str(k)})
    head = soup.head
    data = {
        "会社名": clean(c.find("h2").get_text()),
        "会社コード": int(code),
        "ファイル名": pathlib.Path(src).name,
        "説明": head.find("meta", attrs={"name": "description"})["content"],
        "絞り込み例": c.select_one("#station-search")["placeholder"].removeprefix("例："),
        "一覧クラス": c.select_one("details.train-line-section ul")["class"][0],
        "凡例": legend,
        "アイコン規則": rules,
    }
    rules = [FlowList(r) for r in rules]
    data["アイコン規則"] = rules
    extra = "\n".join(str(x) for x in containers[1:])
    if area:
        data["エリア"] = body
    else:
        data["路線"] = body
    if extra:
        data["付記HTML"] = extra
    (data_dir / "_index.yaml").write_text(dump(data), encoding="utf-8")
    return report


if __name__ == "__main__":
    rep = main(*sys.argv[1:6])
    for k, v in rep.items():
        print(f"■ {k}: {len(v)}")
        for x in v:
            print("   ", x)
