"""DF（公開スプレッドシート）と駅データYAMLを突き合わせ、食い違いを報告する。

新規実装や大幅変更のときに手元で走らせる道具。YAMLもシートも書き換えない。

準備: スプレッドシートを「ファイル → ダウンロード → Microsoft Excel (.xlsx)」で保存し、
      リポジトリ直下に zenmidf.xlsx として置く（.gitignore 済み。古い写しを残さないこと）
      pip install openpyxl

使い方:
  python tools/check_df.py                    # data/ の全駅
  python tools/check_df.py --changed          # origin/main から変えた駅だけ（未コミット分も含む）
  python tools/check_df.py --changed HEAD~3   # 比較元を指定
  python tools/check_df.py -s kanzaki -s san_takamatsu   # 駅IDで指定
  python tools/check_df.py -c 06-Kyushu       # 会社フォルダで指定
  python tools/check_df.py --missing          # DFにあってYAMLのない駅も一覧にする（未実装の洗い出し）
  python tools/check_df.py --xlsx 別の場所.xlsx

照合の単位は「駅 × DFのシート」。YAML側の設備の対応は各社 _index.yaml の アイコン規則 を使う
（みどりの窓口=枠1、ＡＭＶ=枠2、ＭＶ=枠3、ＰＯＳ=枠4、高機能券売機=枠5）。
設備を数えるかどうかも一覧のアイコン判定（build.py の in_service）と同じ。
"""
import argparse
import collections
import pathlib
import re
import subprocess
import sys
import unicodedata

import openpyxl
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build import in_service, models_in_service  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
# DFの「所属会社」→ 駅データのフォルダ（data/ からの相対）
COMPANY = {
    "東海": "03-Central-Japan", "西日本": "04-West-Japan", "四国": "05-Shikoku", "九州": "06-Kyushu",
    "伊豆急行": "99-Others/21-Izukyu", "WILLER TRAINS": "99-Others/22-WILLER-TRAINS",
    "IRいしかわ鉄道": "99-Others/31-IR-Ishikawa", "あいの風とやま鉄道": "99-Others/32-Ainokaze-Toyama",
    "えちごトキめき鉄道": "99-Others/33-Echigo-Tokimeki", "土佐くろしお鉄道": "99-Others/34-Tosa-Kuroshio",
    "のと鉄道": "99-Others/35-Noto", "ハピラインふくい": "99-Others/36-Hapiline-Fukui",
    "北越急行": "99-Others/37-Hokuetsu-Kyuko",
}
# DFがその会社を載せていないシート（照合しない）。私鉄・三セクの自社POSはDFの対象外
SKIP_SHEETS = {f: {"ＰＯＳ"} for f in COMPANY.values() if f.startswith("99-Others/")}
SHEET_SLOT = {"みどりの窓口": 1, "ＡＭＶ": 2, "ＭＶ": 3, "ＰＯＳ": 4, "高機能券売機": 5}
HOURS_COL = {"みどりの窓口": "出札営業時間", "ＡＭＶ": "端末営業時間", "ＭＶ": "端末営業時間", "ＰＯＳ": "出札営業時間"}
UNSTAFFED = {"遠隔管理（駅社員不在）", "駅員無配置駅"}


# ---------- 正規化 ----------
def nfkc(s):
    return unicodedata.normalize("NFKC", str(s or ""))


def norm_name(s):
    s = re.sub(r"\s+", "", nfkc(s))
    s = re.sub(r"【[^】]*】", "", s)
    s2 = re.sub(r"\([^)]*\)$", "", s)  # 末尾の（在来線）などだけ外す。先頭の（讃）は残す
    return s2 or s


TIME = r"(?:\d{1,2}:\d{2}|始発|最終)"


def bare(name):
    return re.sub(r"^\([^)]*\)", "", name) or name


def hour_parts(s):
    """時刻の範囲（6:30~15:00、始発~最終 など）だけを取り出す。後ろの説明文や ? * は無視"""
    s = re.sub(r"[?*\s]", "", nfkc(s).replace("〜", "~").replace("～", "~"))
    return set(re.findall(TIME + "~" + TIME, s))


def models(s):
    s = nfkc(s).upper().replace("併設", "").replace("(仮称)", "")
    return {p.strip() for p in re.split(r"[\n/、,]", s) if p.strip() and p.strip() not in ("NONE", "#", "-")}


def to_int(v):
    m = re.match(r"^\s*(\d+)", nfkc(v))
    return int(m.group(1)) if m else None


def short(s, n=60):
    s = re.sub(r"\s+", " ", nfkc(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- YAML 側 ----------
def places(fac):
    return fac.get("場所") or [fac]


def is_log(pl):
    return "摘要" in (pl.get("列") or [])


def lines_of(pl):
    return [x for x in pl.get("説明", []) + [x for c in pl.get("内容", []) for x in c.get("説明", [])] if isinstance(x, dict)]


def rows_of(pl):
    return [r for r in pl.get("端末", []) + [r for c in pl.get("内容", []) for r in c.get("端末", [])] if isinstance(r, dict)]


def match_pattern(name, pattern):
    return name.startswith(pattern[:-1]) if pattern.endswith("*") else name == pattern


class Station:
    def __init__(self, company, sid, data):
        self.company, self.id, self.data = company, sid, data
        self.name = data.get("駅名", "")
        self.form = data.get("形態", "")
        self.facs = [f for f in data.get("設備") or [] if isinstance(f, dict) and f.get("名称")]

    def matched(self, patterns, models=None):
        """設備名が合う設備。models があれば、その機種の端末がある設備だけ（一覧アイコンの規則と同じ）"""
        return [f for f in self.facs if in_service(f) and any(match_pattern(f["名称"], p) for p in patterns)
                and (not models or any(match_pattern(m, mp) for m in models_in_service(f) for mp in models))]

    @staticmethod
    def detail(facs):
        hours, ops, mdl, count = set(), set(), set(), []
        for f in facs:
            for pl in places(f):
                if not isinstance(pl, dict) or is_log(pl) or not in_service({"場所": [pl]}):
                    continue  # 撤去情報の表、営業時間「-」の場所（裏マルスなど）は数えない
                for x in lines_of(pl):
                    for k in ("営業時間", "時間"):
                        if k in x and str(x[k]).strip() not in ("-", ""):
                            hours |= hour_parts(x[k])
                    if "オペレーター対応時間" in x:
                        ops |= hour_parts(x["オペレーター対応時間"])
                for r in rows_of(pl):
                    m = r.get("機種")
                    if m and str(m) != "#":
                        mdl |= models(m)
                        count.append((nfkc(m).upper(), nfkc(r.get("位置", ""))))
        return hours, ops, mdl, count

    def gate_hours(self):
        for f in self.facs:
            if f["名称"] == "改札窓口":
                return [str(x.get("営業時間", x.get("時間"))).strip() for pl in places(f) if isinstance(pl, dict)
                        for x in lines_of(pl) if "営業時間" in x or "時間" in x]
        return []


def load_yaml(companies):
    stations, rules, indexed = {}, {}, collections.defaultdict(set)
    for folder in companies:
        idx = yaml.safe_load((DATA / folder / "_index.yaml").read_text(encoding="utf-8"))
        rules[folder] = idx.get("アイコン規則", [])
        text = (DATA / folder / "_index.yaml").read_text(encoding="utf-8")
        for p in sorted((DATA / folder).glob("*.yaml")):
            if p.name.startswith("_"):
                continue
            st = Station(folder, p.stem, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
            stations[(folder, p.stem)] = st
            if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(p.stem), text):
                indexed[folder].add(p.stem)
    return stations, rules, indexed


def slot_patterns(rules, slot):
    """アイコン規則のうち、その枠の (設備名, 除外する設備名, 機種) を集める"""
    pats, unless, mfilter = [], [], None
    for r in rules:
        if r[0] == slot:
            pats += r[2]
            unless += r[3] if len(r) > 3 else []
            if len(r) > 4:
                mfilter = (mfilter or []) + r[4]
    return pats, unless, mfilter


# ---------- DF 側 ----------
def load_df(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = collections.defaultdict(list)  # sheet -> [dict]
    for sheet in list(SHEET_SLOT) + ["廃止済", "掲載外駅"]:
        if sheet not in wb.sheetnames:
            print(f"注意: シート「{sheet}」がありません", file=sys.stderr)
            continue
        rows = list(wb[sheet].iter_rows(values_only=True))
        head = [str(h).strip() if h is not None else "" for h in rows[1]]
        prev = None
        for r in rows[2:]:
            # 結合セルの2段目（三島【北口】、松田【南口】など）は No・所属会社などが空で読める。上の行から引き継ぐ
            if prev and len(r) >= 7 and not r[1] and r[2] is None and r[6] \
                    and norm_name(r[6]) == norm_name(prev[6]):
                r = tuple(prev[i] if i in (1, 2, 3, 4, 5) else v for i, v in enumerate(r))
            if len(r) < 7 or not r[1] or not re.match(r"^\d-", str(r[1]).strip()):
                prev = None
                continue
            prev = r
            d = {h: r[i] for i, h in enumerate(head) if h and i < len(r)}
            d["_no"] = str(r[1]).strip()
            if d.get("所属会社") in COMPANY:
                out[sheet].append(d)
    return out


# ---------- 照合 ----------
def main():
    ap = argparse.ArgumentParser(description="DFスプレッドシートと駅データYAMLの照合")
    ap.add_argument("--xlsx", default=str(ROOT / "zenmidf.xlsx"))
    ap.add_argument("-s", "--station", action="append", default=[], help="駅ID（複数可）")
    ap.add_argument("-c", "--company", action="append", default=[], help="会社フォルダ（例 06-Kyushu）")
    ap.add_argument("--changed", nargs="?", const="origin/main", help="比較元から変更・追加した駅だけ")
    ap.add_argument("--missing", action="store_true", help="DFにあってYAMLのない駅も表示")
    a = ap.parse_args()

    companies = [c for c in sorted(COMPANY.values()) if (DATA / c).is_dir()
                 and (not a.company or any(c == x or c.startswith(x.rstrip("/") + "/") for x in a.company))]
    stations, rules, indexed = load_yaml(companies)
    df = load_df(a.xlsx)

    # 駅名 → 駅（同名が複数なら一覧に載っている方を優先）
    by_name = collections.defaultdict(list)
    tours = {}
    for (folder, sid), st in stations.items():
        by_name[(folder, norm_name(st.name))].append(st)
        for f in st.facs:
            if "ツアー" in f["名称"]:
                tours[(folder, norm_name(f["名称"]))] = (st, f["名称"])

    by_bare = collections.defaultdict(list)  # 先頭の（讃）（臨）などを外した名前
    for (folder, n), sts in by_name.items():
        by_bare[(folder, bare(n))] += sts

    def resolve(folder, name):
        key = (folder, norm_name(name))
        if key in tours:
            return tours[key][0], [tours[key][1]]
        cands = by_name.get(key) or by_bare.get((folder, bare(key[1])), [])
        if len(cands) > 1:
            cands = [c for c in cands if c.id in indexed[folder]] or cands
        return (cands[0], None) if len(cands) == 1 else (None, None)

    # 対象駅
    targets = set(stations)
    if a.station:
        targets = {k for k in targets if k[1] in a.station}
    if a.changed:
        try:
            out = subprocess.run(["git", "diff", "--name-only", a.changed, "--", "data/"], cwd=ROOT,
                                 capture_output=True, text=True, encoding="utf-8", check=True).stdout
        except subprocess.CalledProcessError as e:
            sys.exit(f"git diff に失敗しました: {e.stderr.strip()}")
        changed = set()
        for line in out.splitlines():
            p = pathlib.PurePosixPath(line)
            if p.suffix == ".yaml" and not p.name.startswith("_"):
                changed.add((p.parent.relative_to("data").as_posix(), p.stem))
        # 一覧に新しく載った駅（比較元の _index.yaml と駅IDの集合を比べる）
        for folder in companies:
            old = subprocess.run(["git", "show", f"{a.changed}:data/{folder}/_index.yaml"], cwd=ROOT,
                                 capture_output=True, text=True, encoding="utf-8").stdout
            new = (DATA / folder / "_index.yaml").read_text(encoding="utf-8")
            ids = lambda t: {w for w in re.findall(r"[a-z0-9_][a-z0-9_-]*", t) if (folder, w) in stations}  # noqa: E731
            changed |= {(folder, w) for w in ids(new) - ids(old)}
        targets &= changed
        print(f"比較元 {a.changed} から変更・追加した駅: {len(targets)} 駅")

    issues = collections.defaultdict(list)  # (folder,sid) -> [(区分, 文)]
    seen = collections.defaultdict(set)     # (folder,sid) -> {sheet}
    missing = collections.defaultdict(list)

    for sheet, slot in SHEET_SLOT.items():
        per_station = collections.defaultdict(list)
        for d in df.get(sheet, []):
            folder = COMPANY[d["所属会社"]]
            if folder not in companies:
                continue
            st, only = resolve(folder, d.get("駅名"))
            if not st:
                missing[(folder, norm_name(d.get("駅名")))].append(f"{sheet}（No.{d['_no']}）")
                continue
            per_station[(st, tuple(only) if only else None)].append(d)

        for (st, only), ds in per_station.items():
            key = (st.company, st.id)
            seen[key].add(sheet)
            if key not in targets or sheet in SKIP_SHEETS.get(st.company, ()):
                continue
            pats, unless, mfilter = (list(only), [], None) if only else slot_patterns(rules[st.company], slot)
            if not pats:
                continue
            label = only[0] if only else sheet
            facs = st.matched(pats, mfilter)
            note = "／".join(short(d.get("備考") or d.get("休止時間・備考") or "", 70) for d in ds if d.get("備考") or d.get("休止時間・備考"))
            note = f"　DF備考: {note}" if note else ""
            if not facs:
                if not st.matched(unless):
                    issues[key].append(("設備", f"DF「{sheet}」にあるが、YAMLに対応する設備（{'・'.join(pats)}）がない{note}"))
                continue
            hours, ops, mdl, rows = Station.detail(facs)

            # 機種
            if slot != 5:
                dm = set().union(*(models(d.get("マルス機種") or d.get("POS機種")) for d in ds))
                lack = sorted(m for m in dm - mdl
                              if not (m == "感熱POS" and any("POS" in y for y in mdl)))  # DFは感熱POSで総称
                if lack:
                    issues[key].append(("機種", f"[{label}] DFの機種 {'・'.join(lack)} がYAMLにない（YAML: {'・'.join(sorted(mdl)) or 'なし'}）{note}"))
            # 端末数
            nums = [to_int(d.get("端末数")) for d in ds]
            if slot != 5 and nums and all(n is not None for n in nums):
                if slot == 1:
                    cnt = [m for m, pos in rows if "POS" not in m and "内蔵" not in pos]
                elif slot == 4:
                    cnt = [m for m, pos in rows if "POS" in m]
                else:
                    cnt = rows
                if sum(nums) != len(cnt):
                    issues[key].append(("端末数", f"[{label}] DF {sum(nums)} 台 ／ YAML {len(cnt)} 台{note}"))
            # 営業時間
            col = HOURS_COL.get(sheet)
            for d in ds:
                v = d.get(col) if col else None
                if v is None or nfkc(v).strip() in ("-", "", "同左", "不明"):
                    continue
                hp = hour_parts(v)
                if hp and not hp <= hours:
                    q = "（DF側は?付き）" if "?" in nfkc(v) else ""
                    issues[key].append(("営業時間", f"[{label}] DF「{short(v)}」{q} ／ YAML「{'、'.join(sorted(hours)) or '時刻の記載なし（不明など）'}」"))
                if sheet == "ＡＭＶ" and d.get("オペレーター対応時間"):
                    op = hour_parts(d["オペレーター対応時間"])
                    if op and ops and not op <= ops:
                        issues[key].append(("営業時間", f"[ＡＭＶ] オペレーター DF「{short(d['オペレーター対応時間'])}」 ／ YAML「{'、'.join(sorted(ops))}」"))
            # 営業形態
            for d in ds:
                fm = nfkc(d.get("営業形態")).replace("?", "").strip()
                if fm and fm not in nfkc(st.form):
                    issues[key].append(("形態", f"[{label}] DF「{fm}」 ／ YAML「{st.form}」"))
                    break
            # 駅員配置（高機能券売機シートのみ）
            if slot == 5:
                g = st.gate_hours()
                for d in ds:
                    v = nfkc(d.get("駅員配置")).strip()
                    if not v or not g:
                        continue
                    yaml_unstaffed = all(h == "-" for h in g)
                    df_unstaffed = v in UNSTAFFED
                    df_staffed = bool(re.search(r"\d|始発|終日", v))
                    if yaml_unstaffed and df_staffed:
                        issues[key].append(("駅員配置", f"DFは駅員配置「{v}」だが、YAMLの改札窓口は営業時間「-」（無人）"))
                    elif df_unstaffed and not yaml_unstaffed:
                        issues[key].append(("駅員配置", f"DFは「{v}」だが、YAMLの改札窓口に営業時間「{'、'.join(g)}」がある"))

    # YAMLにあってDFにない設備
    for key in targets:
        st = stations[key]
        for sheet, slot in SHEET_SLOT.items():
            pats, _, mfilter = slot_patterns(rules[st.company], slot)
            if not pats or sheet in seen[key] or sheet in SKIP_SHEETS.get(st.company, ()):
                continue
            facs = st.matched(pats, mfilter)
            if facs:
                issues[key].append(("設備", f"YAMLに「{'・'.join(f['名称'] for f in facs)}」があるが、DF「{sheet}」に載っていない"))

    # 廃止済・掲載外なのにYAMLで営業中
    for sheet in ("廃止済", "掲載外駅"):
        for d in df.get(sheet, []):
            folder = COMPANY[d["所属会社"]]
            if folder not in companies:
                continue
            st, only = resolve(folder, d.get("駅名"))
            if not st or (st.company, st.id) not in targets:
                continue
            if sheet == "廃止済":
                slot, why = 1, f"最終営業日 {nfkc(d.get('最終営業日'))[:10]}"
                if "みどりの窓口" in seen[(st.company, st.id)]:
                    continue
            else:
                reason = nfkc(d.get("掲載終了事由"))
                slot = 4 if "POS" in reason else 1 if "出札" in reason else None
                why = f"{reason}（{nfkc(d.get('掲載終了年月日'))}）"
                if slot is None or {1: "みどりの窓口", 4: "ＰＯＳ"}[slot] in seen[(st.company, st.id)]:
                    continue
            pats, _, mfilter = slot_patterns(rules[st.company], slot)
            facs = st.matched(only or pats, None if only else mfilter)
            if facs:
                issues[(st.company, st.id)].append(("廃止", f"DF「{sheet}」に載っている（{why}）が、YAMLに「{'・'.join(f['名称'] for f in facs)}」が営業中で残っている"))

    # 出力
    total = collections.Counter()
    for key in sorted(issues):
        st = stations[key]
        print(f"\n== {key[0]}/{key[1]}  {st.name}")
        for kind, msg in issues[key]:
            total[kind] += 1
            print(f"  ・{kind}: {msg}")
    print(f"\n照合した駅: {len(targets)} ／ 食い違いのある駅: {len(issues)}")
    if total:
        print("内訳: " + "、".join(f"{k} {v}" for k, v in total.most_common()))

    if a.missing:
        ms = {k: v for k, v in missing.items() if not a.company or k[0] in a.company}
        print(f"\n== DFにあってYAMLのない駅（会社フォルダ別）: {len(ms)}")
        for (folder, name), where in sorted(ms.items()):
            print(f"  {folder}  {name}  … {'、'.join(where)}")
    elif missing:
        print(f"（DFにあってYAMLのない駅が {len(missing)} 件。--missing で一覧）")


if __name__ == "__main__":
    main()
