/**
 * 共有ドライブの「受付」フォルダの駅写真を、GitHub の inbox/ にまとめて送る。
 * 送ったあとは GitHub Actions が 800×600 PNG にして駅データに反映し、本番にアップロードする。
 *
 * ファイル名: <会社番号2桁>-<駅ID>.<拡張子>  例: 32-takaoka.png / 06-sasabaru.jpg
 * この形になっていない写真（IMG_1234.JPG など）は、駅を自動で当てる:
 *   1. 写真の位置情報（ドライブが読み取った撮影地点）から一番近い駅
 *   2. 近い駅が複数あるときは、写真の文字を OCR で読んでその中から駅名を探す（駅舎の「○○駅」を優先）
 *   位置情報がない写真は自動では決めず「要確認」へ（OCR で読めた駅名を説明に書く。人が確認して名前を付ける）
 *   → 1駅に決まれば「06-isahaya.jpg」のように名前を付け直して送る（ファイルの説明に判定の根拠を書く）
 *   → 決まらなければ「要確認」フォルダへ移し、説明に候補を書く。名前を付けて「受付」に戻せば次の回で送られる
 *   JR と三セクで駅舎が同じ駅（高岡・富山など）は会社が決まらないので、必ず「要確認」になる
 *
 * 設定（スクリプトプロパティ。プロジェクトの設定 → スクリプト プロパティ）
 *   GITHUB_TOKEN      … このリポジトリだけ「Contents: 読み書き」できる fine-grained トークン
 *   GITHUB_REPO       … ISHIKAWA-Hayato/zenmidf-site-yaml
 *   INBOX_FOLDER_ID   … 共有ドライブの「受付」フォルダのID（URLの folders/ の後ろ）
 *   DONE_FOLDER_ID    … 「取り込み済み」フォルダのID
 *   ERROR_FOLDER_ID   … 「エラー」フォルダのID
 *   REVIEW_FOLDER_ID  … 「要確認」フォルダのID（自動判定で駅が決まらなかった写真）
 *   PENDING_FOLDER_ID … 「未実装」フォルダのID（任意。看板の駅名は読めたが、サイトにまだ駅ページがない駅の写真。
 *                        なければ「要確認」に入る。その会社・地域をYAML化したら、名前を付けて「受付」に戻す）
 * サービス: エディタ左の「サービス」＋ → 「Drive API」を追加（自動判定に使う）
 * 初回だけ setupTrigger() を実行すると、以後1時間おきに sendInbox() が動く。
 * 送るのはドライブの縮小画像（長い辺 800px）。1回の実行は4分半で区切り、残りは次の回に送る。
 */

const NAME_RULE = /^\d{2}-[a-z0-9_][a-z0-9_-]*!?\.(png|jpe?g|heic|heif|webp)$/i;   // 駅IDの後ろの ! は「既存の写真を差し替える」
const MAX_FILES_PER_RUN = 100;         // 1回のコミットに入れる上限
const MAX_IDENTIFY_PER_RUN = 30;       // 自動判定（OCR）は時間がかかるので1回の上限
const TIME_BUDGET_MS = 4.5 * 60 * 1000;    // GAS は1回6分で打ち切られるので、4分半でそこまでの分をコミットして終える
const IDENTIFY_BUDGET_MS = 2.5 * 60 * 1000;// 自動判定に使うのは最初の2分半まで（残りは次の回）
const SEND_SIZE = 800;                 // ドライブの縮小画像（長い辺 800px）を送る。取り込みで 800×600 にするので画質は変わらない
const MAX_BYTES = 40 * 1024 * 1024;    // 1枚の上限（GitHub API に送れる大きさの目安）
const STATIONS_URL = 'https://www.zenmidf.jp/station-individual/stations.json';
const GPS_RADIUS_M = 600;              // 撮影地点からこの距離以内の駅だけを候補にする
const GPS_MARGIN_M = 250;              // 一番近い駅との差がこれ以内の駅も候補に残す
const OCR_DIRECT_MAX = 2 * 1024 * 1024;// これより大きい写真・HEIC はドライブの縮小画像で OCR する

function sendInbox() {
  const started = Date.now();
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;     // 前回の実行がまだ動いている
  try {
    const p = PropertiesService.getScriptProperties().getProperties();
    const inbox = DriveApp.getFolderById(p.INBOX_FOLDER_ID);
    const done = DriveApp.getFolderById(p.DONE_FOLDER_ID);
    const error = DriveApp.getFolderById(p.ERROR_FOLDER_ID);
    const review = p.REVIEW_FOLDER_ID ? DriveApp.getFolderById(p.REVIEW_FOLDER_ID) : null;
    const pending = p.PENDING_FOLDER_ID ? DriveApp.getFolderById(p.PENDING_FOLDER_ID) : review;

    // 受付の中身を集める（名前の形が違うものは自動判定。同じ名前が複数あれば一番新しいものだけ送る）
    const byName = {};
    let stations = null, identified = 0;
    const it = inbox.getFiles();
    while (it.hasNext()) {
      const f = it.next();
      let name = f.getName().trim();
      if (f.getSize() > MAX_BYTES) {
        reject_(f, error, '画像が大きすぎます（40MBまで）');
        continue;
      }
      if (!NAME_RULE.test(name)) {
        if (!/^image\//.test(f.getMimeType())) {
          reject_(f, error, '画像ではありません');
          continue;
        }
        if (!review) {
          reject_(f, error, 'ファイル名が「会社番号2桁-駅ID.拡張子」（例 32-takaoka.png）になっていません');
          continue;
        }
        if (identified >= MAX_IDENTIFY_PER_RUN || Date.now() - started > IDENTIFY_BUDGET_MS) continue;   // 残りは次の回に
        identified++;
        if (!stations) stations = loadStations_();
        let r;
        try {
          r = identify_(f, stations);
        } catch (e) {
          r = { review: [], why: '自動判定でエラー: ' + e.message };
        }
        if (!r.decided) {
          if (r.notImplemented) {
            reject_(f, pending, '未実装の駅: ' + r.why +
                    '\n→ その駅のページができたら「会社番号-駅ID.拡張子」に名前を変えて「受付」に戻してください');
            continue;
          }
          const cands = (r.review || []).slice(0, 5).map(s => s.code + '-' + s.id + '（' + s.name + (s.d != null ? '・' + Math.round(s.d) + 'm' : '') + '）');
          reject_(f, review, '駅を決められませんでした: ' + r.why + (cands.length ? '\n候補: ' + cands.join(' / ') : '') +
                  '\n→ 正しい「会社番号-駅ID.拡張子」に名前を変えて「受付」に戻してください');
          continue;
        }
        const original = name;
        name = r.decided.code + '-' + r.decided.id + extension_(f);
        f.setName(name);
        f.setDescription('自動判定: ' + r.how + '（元の名前 ' + original + '）');
      }
      const key = name.toLowerCase().replace(/\.[^.]+$/, '');   // 拡張子違い（.png と .jpg）も同じ駅として扱う
      if (byName[key]) {
        const older = byName[key].getLastUpdated() < f.getLastUpdated() ? byName[key] : f;
        reject_(older, error, '同じ駅の写真がほかにもあったため、新しい方だけ送りました');
        if (older === byName[key]) byName[key] = f;
      } else {
        byName[key] = f;
      }
    }
    const files = Object.keys(byName).sort().slice(0, MAX_FILES_PER_RUN).map(k => byName[k]);
    if (files.length === 0) return;

    // GitHub に1回のコミットで送る（時間切れが近づいたら、そこまでに送れた分だけでコミットする）
    const gh = github_((p.GITHUB_TOKEN || '').trim(), (p.GITHUB_REPO || '').trim());  // 貼り付けた前後の空白・改行を除く
    const blobs = [], sent = [];
    for (const f of files) {
      if (sent.length && Date.now() - started > TIME_BUDGET_MS) break;   // 残りは次の回に
      const up = uploadImage_(f);
      blobs.push({
        path: 'inbox/' + f.getName().trim().replace(/\.[^.]+$/, up.ext),
        mode: '100644',
        type: 'blob',
        sha: gh('post', '/git/blobs', { content: Utilities.base64Encode(up.bytes), encoding: 'base64' }).sha,
      });
      sent.push(f);
    }
    const message = '駅写真を受付から送信（' + sent.length + '枚）: ' + sent.map(f => f.getName()).join('、');
    for (let attempt = 0; ; attempt++) {
      const head = gh('get', '/git/ref/heads/main').object.sha;
      const baseTree = gh('get', '/git/commits/' + head).tree.sha;
      const tree = gh('post', '/git/trees', { base_tree: baseTree, tree: blobs }).sha;
      const commit = gh('post', '/git/commits', { message: message, tree: tree, parents: [head] }).sha;
      try {
        gh('patch', '/git/refs/heads/main', { sha: commit, force: false });
        break;
      } catch (e) {
        if (attempt >= 2) throw e;      // ちょうど誰かが push した場合は取り直してやり直す
        Utilities.sleep(3000);
      }
    }

    // 送り終えたものは「取り込み済み」へ（日付を頭に付けて残す）
    const stamp = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyyMMdd-HHmm');
    sent.forEach(f => {
      f.setName(stamp + '_' + f.getName().trim());
      f.moveTo(done);
    });
  } finally {
    lock.releaseLock();
  }
}

// ---------- 駅の自動判定 ----------

/** サイトに上がっている駅の一覧（build.py が作る stations.json） */
function loadStations_() {
  const res = UrlFetchApp.fetch(STATIONS_URL, { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) throw new Error('駅の一覧（stations.json）を読めません: ' + res.getResponseCode());
  return JSON.parse(res.getContentText()).map(s => Object.assign(s, { bare: bare_(s.name) }));
}

/** 位置情報 → OCR の順に駅を当てる。{decided, how} か {review: 候補, why} を返す */
function identify_(file, stations) {
  let near = null;
  const meta = Drive.Files.get(file.getId(), { fields: 'imageMediaMetadata(location)', supportsAllDrives: true });
  const loc = meta.imageMediaMetadata && meta.imageMediaMetadata.location;
  if (loc && loc.latitude != null && loc.longitude != null) {
    const sorted = stations.filter(s => s.lat != null)
      .map(s => Object.assign({}, s, { d: distM_(loc.latitude, loc.longitude, s.lat, s.lon) }))
      .sort((a, b) => a.d - b.d);
    if (sorted.length && sorted[0].d <= GPS_RADIUS_M) {
      near = sorted.filter(s => s.d <= sorted[0].d + GPS_MARGIN_M);
      if (near.length === 1) return { decided: near[0], how: '位置情報（駅から' + Math.round(near[0].d) + 'm）' };
      const names = unique_(near.map(s => s.bare));
      if (names.length === 1) return { review: near, why: '会社が決まらない共用駅（' + names[0] + '）' };
    }
  }
  const text = ocr_(file);
  const pool = near || stations;
  const hasLoc = !!(loc && loc.latitude != null && loc.longitude != null);
  if (!text) return { review: pool === stations ? [] : pool, why: hasLoc ? '近くに駅がなく、文字も読み取れませんでした' : '位置情報がなく、文字も読み取れませんでした' };
  const hits = matchText_(text, pool);
  // 看板の「○○駅」がこちらにない駅の名前で、ほかの一致がローマ字・駅名だけ（弱い）なら、ページのない駅の写真とみなす
  // （例: 「城北公園通駅」の写真の広告の HIROSHIMA を広島と見なさない）
  const read = stationLike_(text).filter(n => !stations.some(s => s.bare + '駅' === n));
  if (!near && read.length && !hits.some(h => h.hit === h.bare + '駅')) {
    return { notImplemented: true, why: '看板の駅名「' + read.join('」「') + '」は読めましたが、サイトにまだ駅ページがありません' };
  }
  // 位置情報がない写真は OCR だけで決めない（読み違いで別の駅に付くのを防ぐ）。読めた駅名は候補として残し、人が確認する
  if (!hasLoc) {
    return { review: hits, why: '位置情報がないため自動では決めません' +
             (hits.length ? '（文字から読めた駅名: ' + unique_(hits.map(h => h.hit)).join('・') + '）' : '（文字から駅名は読み取れませんでした）') };
  }
  if (hits.length === 1 && !near && hits[0].dup > 1) {
    // 全国に同じ名前の駅があり（柏原・橋本など）、こちらにページがあるのはその1つだけ。位置情報がないので決めない
    return { review: hits, why: '「' + hits[0].bare + '」は全国に同名の駅が' + hits[0].dup + 'か所あり、位置情報がないため決められません' };
  }
  if (hits.length === 1) return { decided: hits[0], how: (near ? '位置情報＋' : '') + 'OCR「' + hits[0].hit + '」' };
  if (hits.length > 1) {
    const names = unique_(hits.map(s => s.bare));
    return { review: hits, why: names.length === 1 ? '会社が決まらない共用駅（' + names[0] + '）' : '駅名の候補が複数' };
  }
  return { review: near || [], why: '駅名が読み取れませんでした（読めた文字: ' + text.replace(/\s+/g, ' ').slice(0, 40) + '…）' };
}

/** OCR の文字から「○○駅」の形のものを拾う（漢字・かなの1〜8文字＋駅。重複は除く） */
function stationLike_(text) {
  const t = text.normalize('NFKC').replace(/[ \t]+/g, '');
  const found = [];
  const re = /([\u4E00-\u9FFF\u3041-\u309F\u30A1-\u30FFヶ々]{1,8})駅/g;
  let m;
  while ((m = re.exec(t)) !== null) {
    const name = m[1].replace(/^(JR|ＪＲ)/, '') + '駅';
    if (found.indexOf(name) < 0) found.push(name);
  }
  return found.slice(0, 3);
}

/** OCR で読めた文字から駅名を探す。「○○駅」> ローマ字 > 駅名だけ の順に強く見る
 *  - 漢字は前後に別の漢字・かなが続かないときだけ（「新秋津駅」から「秋津駅」を拾わない）
 *  - ローマ字は単語まるごと一致だけ（「SHIN-AKITSU」を Akitsu、「SAKAKITA」を Saka と見なさない） */
function matchText_(text, pool) {
  const t = text.normalize('NFKC').replace(/\s+/g, '').replace(/[ヶヵ]/g, 'ケ');
  const words = romanWords_(text);
  let best = 0, hits = [];
  pool.forEach(s => {
    let score = 0, hit = '';
    const roman = romanKey_(s.roman);
    if (s.bare && findWord_(t, s.bare + '駅', false)) { score = 3; hit = s.bare + '駅'; }
    else if (roman.length >= 3 && words.has(roman)) { score = 2; hit = s.roman; }
    else if (s.bare && s.bare.length >= 2 && findWord_(t, s.bare, true)) { score = 1; hit = s.bare; }
    if (!score) return;
    if (score > best) { best = score; hits = []; }
    if (score === best) hits.push(Object.assign({}, s, { hit: hit }));
  });
  // 「小倉」と「南小倉」のように、ほかの候補の駅名に含まれてしまう短い方は外す
  return hits.filter(h => !hits.some(o => o.bare !== h.bare && o.bare.indexOf(h.bare) >= 0));
}

const KANA_KANJI = /[\u4E00-\u9FFF\u3041-\u309F\u30A1-\u30FFケ々]/;
/** 文字列 t の中に w が、前（checkAfter なら後ろも）に漢字・かなが続かない形で出てくるか */
function findWord_(t, w, checkAfter) {
  for (let i = t.indexOf(w); i >= 0; i = t.indexOf(w, i + 1)) {
    const before = i > 0 ? t.charAt(i - 1) : '';
    const after = t.charAt(i + w.length);
    if (KANA_KANJI.test(before)) continue;
    if (checkAfter && KANA_KANJI.test(after)) continue;
    return true;
  }
  return false;
}

/** ローマ字の比較用：大文字・長音記号なし・ハイフンや空白なし（Iyo-Saijo → IYOSAIJO） */
function romanKey_(r) {
  return String(r || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toUpperCase().replace(/[^A-Z]/g, '');
}

/** OCR の文字の英単語（ハイフンでつながった語は1語）と、続けて並ぶ2〜3語をつなげたもの */
function romanWords_(text) {
  const tokens = text.normalize('NFKC').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toUpperCase()
    .split(/[^A-Z-]+/).map(w => w.replace(/-/g, '')).filter(w => w && ['JR', 'STATION', 'STA'].indexOf(w) < 0);
  const set = new Set();
  for (let i = 0; i < tokens.length; i++) {
    let w = '';
    for (let j = i; j < Math.min(i + 3, tokens.length); j++) { w += tokens[j]; set.add(w); }
  }
  return set;
}

/** ドライブの OCR（画像を一時的に Google ドキュメントに変換して文字を取り出す。一時ファイルはすぐ消す） */
function ocr_(file) {
  let blob;
  const mime = file.getMimeType();
  if (file.getSize() > OCR_DIRECT_MAX || !/^image\/(jpeg|png|gif)$/.test(mime)) {
    const meta = Drive.Files.get(file.getId(), { fields: 'thumbnailLink', supportsAllDrives: true });
    if (!meta.thumbnailLink) return '';
    const url = meta.thumbnailLink.replace(/=s\d+(-c)?$/, '') + '=s1600';
    const res = UrlFetchApp.fetch(url, { headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() }, muteHttpExceptions: true });
    if (res.getResponseCode() !== 200) return '';
    blob = res.getBlob().setName('ocr.jpg');
  } else {
    blob = file.getBlob();
  }
  let doc;
  for (let attempt = 0; ; attempt++) {
    try {
      doc = Drive.Files.create({ name: 'zenmidf-ocr-tmp', mimeType: MimeType.GOOGLE_DOCS }, blob, { ocrLanguage: 'ja' });
      break;
    } catch (e) {
      if (attempt >= 1) throw e;        // ドライブ側の一時的なエラー（Internal Error）は1回だけやり直す
      Utilities.sleep(2000);
    }
  }
  try {
    return DocumentApp.openById(doc.id).getBody().getText();
  } finally {
    Drive.Files.remove(doc.id);
  }
}

function bare_(name) {   // 駅名の比較用：先頭の（讃）（臨）・末尾の（在来線）・「駅」を外す
  return String(name || '').normalize('NFKC').trim()
    .replace(/^\([^)]*\)|^[^)]{1,2}\)/, '').replace(/\([^)]*\)$/, '').replace(/駅$/, '')
    .replace(/[ヶヵ]/g, 'ケ').replace(/\s+/g, '');
}

function extension_(file) {
  const m = file.getName().match(/\.(png|jpe?g|heic|heif|webp)$/i);
  if (m) return '.' + m[1].toLowerCase();
  return { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/heic': '.heic', 'image/heif': '.heif', 'image/webp': '.webp' }[file.getMimeType()] || '.jpg';
}

function distM_(lat1, lon1, lat2, lon2) {
  const dy = (lat1 - lat2) * 111000;
  const dx = (lon1 - lon2) * 111000 * Math.cos((lat1 + lat2) / 2 * Math.PI / 180);
  return Math.sqrt(dx * dx + dy * dy);
}

function unique_(a) { return a.filter((x, i) => a.indexOf(x) === i); }

// ---------- 送る画像 ----------

/** ドライブの縮小画像（長い辺 SEND_SIZE px の JPEG。向きは直っていて、位置情報などは付かない）を使う。
 *  元の写真をそのまま送るとリポジトリの履歴が重くなるため。縮小画像がまだ作られていなければ元の写真を送る */
function uploadImage_(file) {
  try {
    const meta = Drive.Files.get(file.getId(), { fields: 'thumbnailLink', supportsAllDrives: true });
    if (meta.thumbnailLink) {
      const url = meta.thumbnailLink.replace(/=s\d+(-c)?$/, '') + '=s' + SEND_SIZE;
      const res = UrlFetchApp.fetch(url, { headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() }, muteHttpExceptions: true });
      if (res.getResponseCode() === 200 && /^image\//.test(res.getHeaders()['Content-Type'] || res.getBlob().getContentType())) {
        return { bytes: res.getBlob().getBytes(), ext: '.jpg' };
      }
    }
  } catch (e) {
    // 縮小画像が取れないときは元の写真
  }
  return { bytes: file.getBlob().getBytes(), ext: extension_(file) };
}

// ---------- 共通 ----------

function reject_(file, folder, reason) {
  file.setDescription(reason);          // 移した先のフォルダで、ファイルの詳細（説明）に理由が出る
  file.moveTo(folder);
}

function github_(token, repo) {
  return function (method, path, body) {
    const res = UrlFetchApp.fetch('https://api.github.com/repos/' + repo + path, {
      method: method,
      headers: { Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' },
      contentType: 'application/json',
      payload: body ? JSON.stringify(body) : undefined,
      muteHttpExceptions: true,
    });
    const code = res.getResponseCode();
    if (code >= 300) throw new Error('GitHub API ' + method.toUpperCase() + ' ' + path + ' → ' + code + ': ' + res.getContentText().slice(0, 300));
    return JSON.parse(res.getContentText());
  };
}

/** 初回に1回だけ実行：1時間おきに sendInbox を動かす */
function setupTrigger() {
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'sendInbox').forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('sendInbox').timeBased().everyHours(1).create();
}

/** 「要確認」の写真の名前と理由（説明）を実行ログに一覧する。判定の調子を見るとき用 */
function listReview() {
  const p = PropertiesService.getScriptProperties().getProperties();
  const it = DriveApp.getFolderById(p.REVIEW_FOLDER_ID).getFiles();
  const lines = [];
  while (it.hasNext()) {
    const f = it.next();
    lines.push(f.getName() + '\n  ' + String(f.getDescription() || '').replace(/\n/g, '\n  '));
  }
  console.log('要確認 ' + lines.length + ' 枚\n' + lines.join('\n'));
}
