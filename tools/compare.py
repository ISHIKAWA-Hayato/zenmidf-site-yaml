"""元ページと生成ページの<main>を「ブラウザでの見た目」の単位で比較する。

使い方: python tools/compare.py <元のmainsフォルダ> <生成したdistの会社フォルダ>
- ブラウザと同じ解釈をする html5lib で読み、表示に影響しない空白・改行・
  段落末尾の<br>をならしてから比べる
"""
import sys, re, pathlib, difflib
import html5lib

BLOCK = "p|div|h2|h3|h4|table|thead|tbody|tr|th|td|ul|li|details|summary|label|input|main|br"
KEEP = ("class", "href", "src", "alt", "id", "type", "placeholder", "for")


def ser(el):
    tag = el.tag.split("}")[-1]
    attrs = "".join(f' {k}="{el.get(k)}"' for k in KEEP if el.get(k) is not None)
    out = f"<{tag}{attrs}>" + (el.text or "")
    for ch in el:
        out += ser(ch) + (ch.tail or "")
    return out + f"</{tag}>"


def normalize(main_inner):
    frag = html5lib.parseFragment(main_inner, container="main", namespaceHTMLElements=False)
    s = (frag.text or "") + "".join(ser(c) + (c.tail or "") for c in frag)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(<span[^>]*>) ", r" \1", s)  # span内側の端の空白は外側と同じ扱い
    s = re.sub(r" (</span>)", r"\1 ", s)
    s = re.sub(r" +", " ", s)
    s = re.sub(rf" ?(</?(?:{BLOCK})\b[^>]*>) ?", r"\1", s)  # ブロック境界の空白は表示されない
    s = s.replace("</br>", "")
    s = re.sub(r"(<br>)+</p>", "</p>", s)  # 段落末尾の<br>は表示されない
    return re.sub(r"(?=<(?:h2|h3|h4|p|table|tr|li|details)\b)", "\n", s).strip()


def main_of(html):
    return html[html.index("<main>") + 6: html.index("</main>")]


def run(orig_dir, gen_dir):
    ok, ng = [], []
    for f in sorted(pathlib.Path(orig_dir).glob("*.html")):
        g = pathlib.Path(gen_dir) / f.name
        if not g.exists():
            print(f"□ 生成なし: {f.name}")
            continue
        src = f.read_text(encoding="utf-8")
        a = normalize(main_of(src) if "<main>" in src else src)  # 元はmainだけでもページ丸ごとでもよい
        b = normalize(main_of(g.read_text(encoding="utf-8")))
        if a == b:
            ok.append(f.name)
        else:
            ng.append(f.name)
            print(f"■ 差分あり: {f.name}")
            for l in difflib.unified_diff(a.splitlines(), b.splitlines(), "元", "生成", lineterm="", n=1):
                print("   ", l)
    print(f"\n一致 {len(ok)} / {len(ok) + len(ng)}")
    return ng


if __name__ == "__main__":
    run(*sys.argv[1:3])
