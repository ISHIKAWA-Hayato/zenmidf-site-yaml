"""既存の駅ページHTMLを駅データYAMLへ逆変換する。

使い方: python tools/html2yaml.py <駅ページのフォルダ> <出力フォルダ> [対象ファイル名 ...]
- 引数のフォルダ内の駅ページ（<main>を含むHTML）を1駅1ファイルのYAMLにする
- 解釈できない書き方の行は「HTML:」としてそのまま残す（情報を落とさない）
"""
import sys
import re
import pathlib
import yaml
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

DEFAULT_IMAGE = re.compile(r"(\.\./)+images/station\.png")  # 階層の深さによらず共通画像
STD_COLS = ["位置", "番号", "機種", "発行箇所名"]
PRIORITY_LEGEND = [
    "Ｓ：必ず開設される端末",
    "Ａ-Ｃ：開設優先順位が比較的高い端末",
    "Ｄ-Ｆ：開設優先順位が比較的低い端末",
    "Ｇ：休眠・不使用端末",
    "Ｚ：開設優先順位が不明な端末",
]
ABSENT_INTROS = ("以下の時間帯は駅社員が不在となります。", "以下の時間帯は係員が不在となります。")  # 旧・新の表示どちらも 社員不在 に
PAUSE_INTRO = "以下の時間帯は休止となります。"
OPERATOR_PREFIX = "オペレーター対応時間 "
MARS_ICONS = {"設置": "mars-install", "撤去": "mars-remove"}


def clean(s):
    # 全角スペースは意味を持つことがあるので残す
    return re.sub(r"[ \t\r\n]+", " ", s).strip(" \t\r\n")


class Flow(dict):
    """YAMLで1行（{ ... }）に書き出す辞書"""


yaml.add_representer(
    str,
    lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None),
)
yaml.add_representer(
    Flow, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v.items(), flow_style=True)
)


def main_of(html):
    return html[html.index("<main>") + 6: html.index("</main>")]


def split_lines(p):
    """<p>の中身を<br>で区切り、行ごとの部品リストにする"""
    lines, cur = [], []
    for node in p.children:
        if isinstance(node, Tag) and node.name == "br":
            lines.append(cur)
            cur = []
        else:
            cur.append(node)
    lines.append(cur)
    out = []
    for parts in lines:
        segs = []
        for n in parts:
            if isinstance(n, Comment):
                continue   # HTML のコメント（編集者のメモ）は表示されないので落とす
            if isinstance(n, NavigableString):
                if clean(n):
                    segs.append(("text", clean(n), n))
            elif n.name == "span":
                segs.append((n.get("class", [""])[0], n, n))
            else:
                segs.append((n.name, n, n))
        out.append(segs)
    while out and not out[-1]:
        out.pop()
    return out


def span_text(span):
    """span内の文字列。span内の<br>は改行として残す"""
    buf = []
    for n in span.children:
        if isinstance(n, Tag) and n.name == "br":
            buf.append("\x00")
        else:
            buf.append(str(n))
    return "\n".join(clean(x) for x in "".join(buf).split("\x00"))


def parse_lines(p):
    lines = split_lines(p)
    items, i = [], 0
    while i < len(lines):
        segs = lines[i]
        kinds = [k for k, _, _ in segs]
        if not segs:
            items.append({"空行": True})
            i += 1
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        # 「以下の時間帯は…」＋次の行の時間 → 社員不在 / 休止
        if kinds == ["text"] and segs[0][1] in ABSENT_INTROS + (PAUSE_INTRO,) and nxt and [k for k, _, _ in nxt] == ["business-hour-text"]:
            key = "社員不在" if segs[0][1] in ABSENT_INTROS else "休止"
            items.append({key: span_text(nxt[0][1])})
            i += 2
            continue
        if kinds == ["text", "business-hour-text"]:
            label, val = segs[0][1], span_text(segs[1][1])
            items.append({"営業時間": val} if label == "営業時間" else Flow({"区分": label, "時間": val}))
        elif kinds == ["text", "business-hour-text", "unknown-text"]:
            items.append(Flow({"区分": segs[0][1], "時間": span_text(segs[1][1]), "補足": span_text(segs[2][1])}))
        elif kinds == ["business-hour-text", "unknown-text"]:
            items.append(Flow({"時間": span_text(segs[0][1]), "補足": span_text(segs[1][1])}))
        elif kinds == ["text", "unknown-text"] and segs[0][1] == "営業時間" and span_text(segs[1][1]) == "不明":
            items.append({"営業時間": "不明"})
        elif kinds == ["business-hour-text"]:
            items.append({"時間": span_text(segs[0][1])})
        elif kinds == ["amv-operation-hour-text"] and span_text(segs[0][1]).startswith(OPERATOR_PREFIX):
            items.append({"オペレーター対応時間": span_text(segs[0][1])[len(OPERATOR_PREFIX):]})
        elif kinds == ["unknown-text"]:
            items.append({"注記": span_text(segs[0][1])})
        elif kinds == ["text"]:
            text = segs[0][1]
            if text in ABSENT_INTROS:   # 「駅社員が不在」は「係員が不在」に統一（簡易委託駅も含むため）
                text = ABSENT_INTROS[1]
            items.append({"文": text})
        elif kinds == ["text", "a"] and segs[0][1].endswith("は") and clean(segs[1][1].get_text()) == "こちら":
            # 「JR西日本糸魚川駅は<a href=…>こちら</a>」→ 他社ページへの案内
            items.append(Flow({"他社ページ": segs[0][1][:-1], "リンク": segs[1][1]["href"]}))
        else:
            raw = "".join(str(n) for _, _, n in segs)
            items.append({"HTML": clean(raw)})
        i += 1
    return items


def cell_value(td, col):
    imgs = td.find_all("img")
    if col == "摘要" and len(imgs) == 1 and not clean(td.get_text()):
        alt = imgs[0].get("alt")
        assert MARS_ICONS.get(alt) and MARS_ICONS[alt] in imgs[0]["src"], str(td)
        return alt
    for k in td.find_all(string=lambda t: isinstance(t, Comment)):
        k.extract()   # 編集者のメモ（表示されない）は落とす
    kids = [k for k in td.children if not (isinstance(k, NavigableString) and not clean(k))]
    if len(kids) == 1 and isinstance(kids[0], Tag) and kids[0].name == "span" and kids[0].get("class") == ["unknown-text"]:
        return Flow({"薄字": clean(kids[0].get_text())})
    assert not td.find(True), str(td)
    return clean(td.get_text())


def parse_table(table):
    cols = [re.sub(r"[\s　]+", "", th.get_text()) for th in table.select("thead th")]
    rows = []
    body_rows = table.select("tbody tr") or table.select("thead tr")[1:]   # 見出しの中に行を書いてしまったページもある
    for tr in body_rows:
        if not tr.find_all("td"):
            continue   # 中身のない行（表示されない）
        vals = [cell_value(td, c) for c, td in zip(cols, tr.find_all("td"))]
        assert len(vals) == len(cols), str(tr)
        rows.append(Flow({c: v for c, v in zip(cols, vals) if v != "-"}))
    preface = clean("".join(table.find_all(string=True, recursive=False)))
    meta = {}
    if table.get("class"):
        meta["表クラス"] = " ".join(table["class"])
    if cols[:4] == STD_COLS and cols[4:] in ([], ["優先"]):
        if len(cols) == 5 and not any("優先" in r for r in rows):
            meta["優先列"] = True
    else:
        meta["列"] = cols
    default_preface = cols[0] != "摘要"
    if bool(preface) != default_preface:
        meta["前書き"] = bool(preface)
    if preface:
        assert preface == "◆端末設置状況", preface
    return rows, meta


def is_priority_legend(p):
    return [x["文"] for x in parse_lines(p) if "文" in x] == PRIORITY_LEGEND


def finish_place(place):
    """場所の中身の並びが「説明→端末」の基本形なら短い書き方に、そうでなければ『内容』で順番どおりに持つ"""
    seq = place.pop("_seq")
    kinds = [k for k, _ in seq]
    if kinds in ([], ["説明"], ["端末"], ["説明", "端末"]):
        for k, v in seq:
            if k == "説明":
                place["説明"] = v
            else:
                rows, meta = v
                place["端末"] = rows
                place.update(meta)
    else:
        content = []
        for k, v in seq:
            if k == "説明":
                content.append({"説明": v})
            else:
                rows, meta = v
                content.append({"端末": rows, **meta})
        place["内容"] = content


def _split_br(p):
    segs, cur = [], []
    for n in p.children:
        if isinstance(n, Tag) and n.name == "br":
            segs.append(cur)
            cur = []
        elif not isinstance(n, Comment):
            cur.append(n)
    segs.append(cur)
    return [s for s in segs if clean("".join(str(n) for n in s))]


def heading_name(h):
    """見出しの名前（後ろの <span class="unknown-text">【個札不可】</span> は除く）"""
    return clean("".join(n.get_text() if isinstance(n, Tag) else str(n) for n in h.children
                         if not (isinstance(n, Comment) or (isinstance(n, Tag) and n.get("class") == ["unknown-text"]))))


def heading_note(h):
    sp = h.find("span", class_="unknown-text")
    return clean(sp.get_text()) if sp else None


def convert(main_html):
    soup = BeautifulSoup(main_html, "html.parser")
    c = soup.select_one(".station-container")
    kids = [k for k in c.children if isinstance(k, Tag)]
    h2, h4, aff, imgp = kids[:4]
    m = re.fullmatch(r"(.*?)[　 ]+(.*)", clean(h4.get_text()))
    kana, roman = (m.group(1), m.group(2)) if m else (clean(h4.get_text()), "")   # ローマ字のない駅もある（池田）
    aff_lines = [clean("".join(str(n) for n in seg)) for seg in _split_br(aff)]
    parts = [clean(x) for x in BeautifulSoup(aff_lines[0], "html.parser").get_text().split("／", 3)]
    company, branch, district, kind = parts
    st = {"駅名": clean(h2.get_text()), "よみ": kana, "ローマ字": roman, "会社": company, "支社": branch}
    if not roman:
        del st["ローマ字"]
    if district:
        st["管轄"] = district
    st["形態"] = kind
    if len(aff_lines) > 1:   # 所属の行の下の注記（「※駅名と発行箇所名で表記が異なります。」など）
        st["所属注記"] = "<br />".join(aff_lines[1:])
    img = imgp.find("img")
    if not DEFAULT_IMAGE.fullmatch(img["src"]):
        st["画像"] = img["src"]
        if img["alt"] != f"{st['駅名']}駅の画像":
            st["画像説明"] = img["alt"]
    elif img["alt"] != "駅の画像":
        st["画像説明"] = img["alt"]
    # 画像の段落に一緒に書かれた案内（「JR西日本○○駅は<a>こちら</a>」など）は駅注記へ
    img.extract()
    image_notes = parse_lines(imgp) if clean(imgp.get_text()) else []
    plain = [x for x in image_notes if not x.get("空行")]
    if len(plain) == 1 and "文" in plain[0]:   # 画像の下の一行（「写真提供：○○」など）は画像の段落に残す
        st["画像注記"] = plain[0]["文"]
        image_notes = []

    facilities, fac, place = [], None, None
    head = {"_seq": []}  # 最初の設備より前（駅全体への注記）
    places = [head]
    for el in kids[4:]:
        if el.name == "h3":
            fac = {"名称": heading_name(el), "_seq": []}
            if heading_note(el):
                fac["名称注記"] = heading_note(el)
            facilities.append(fac)
            place = fac
            places.append(fac)
        elif el.name == "h4":
            place = {"名称": heading_name(el), "_seq": []}
            if heading_note(el):
                place["名称注記"] = heading_note(el)
            fac.setdefault("場所", []).append(place)
            places.append(place)
        elif el.name == "p":
            target = place or head
            if target.get("名称") == "優先順位について" and is_priority_legend(el):
                target["_legend"] = True
                continue
            target["_seq"].append(("説明", parse_lines(el)))
        elif el.name == "table":
            (place or head)["_seq"].append(("端末", parse_table(el)))
        else:
            raise ValueError(f"未対応の要素: {el.name}")
    for pl in places:
        finish_place(pl)
    for f in facilities:
        if "場所" in f:
            f["場所"] = [{"定型文": "優先順位について"} if p.pop("_legend", False) else p for p in f["場所"]]
    if head.get("説明") or head.get("内容") or head.get("端末"):
        assert set(head) == {"説明"}, head
    if image_notes or head.get("説明"):
        st["駅注記"] = image_notes + head.get("説明", [])
    st["設備"] = facilities
    return st


def dump(data):
    return yaml.dump(data, allow_unicode=True, sort_keys=False, width=200)


def main(src, dst, names=None):
    dst = pathlib.Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    files = [pathlib.Path(src) / n for n in names] if names else sorted(pathlib.Path(src).glob("*.html"))
    done, failed = 0, []
    for f in files:
        html = f.read_text(encoding="utf-8")
        if not names and 'id="station-search"' in html:
            continue  # 会社一覧ページ（index2yaml の担当）
        try:
            data = convert(main_of(html) if "<main>" in html else html)
        except Exception as e:   # 解釈できないページは飛ばして最後に一覧する（1駅の失敗で全部を止めない）
            failed.append((f.name, f"{type(e).__name__}: {str(e)[:160]}"))
            continue
        if not names and data["駅名"] == "xx":
            continue  # 雛形
        done += 1
        (dst / (f.stem + ".yaml")).write_text(dump(data), encoding="utf-8")
    print(f"変換 {done} 駅 → {dst}" + (f"、変換できなかった駅 {len(failed)}" if failed else ""))
    for name, why in failed:
        print(f"  ✗ {name}: {why}")
    return failed


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:] or None)
