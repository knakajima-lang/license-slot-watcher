#!/usr/bin/env python3
"""
警視庁 学科試験（教習所卒業者）空き枠ウォッチャー

- 指定した試験場・月の空き状況を予約サイトの公開カレンダーAPIから取得
- 「新しい月の予約枠の公開」「警視庁HPでの公開日の告知」「満席だった日に空きが出た（キャンセル）」を検知して LINE に通知
- 見張る月は「今月〜約3か月先」を毎回自動で決めるので、月が変わっても止めるまで続きます
- 予約は行いません（通知を見て自分で予約する前提）

標準ライブラリのみで動作します。
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ===== 設定（環境変数で上書き可） =====
API_URL = "https://license-test-tokyo-prd-police-pref-api.tokyo-madoguchi-yoyaku.com/calgetres"
SITE_URL = "https://license-renew.tokyo-madoguchi-yoyaku.com/police-pref-tokyo/index_000.html"
NOTICE_URL = "https://www.keishicho.metro.tokyo.lg.jp/menkyo/menkyo/web.html"

# 11 = 教習所卒業等（マイナ免許証なし）／ 61 = 教習所卒業等（マイナ免許証あり）
COURSE_CODE = os.environ.get("COURSE_CODE", "11")
PLACES = {"270": "府中", "280": "鮫洲", "250": "江東"}
PLACE_CODES = [p for p in os.environ.get("PLACE_CODES", "270,280,250").split(",") if p]
# 空なら「今月〜92日後の月」を自動で対象にする（学科試験の予約は90日先まで）
MONTHS_ENV = [m for m in os.environ.get("MONTHS", "").split(",") if m]

STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

ERROR_ALERT_AFTER = 6  # 連続失敗がこの回数に達したら1度だけ通知

JST = timezone(timedelta(hours=9))
WEEK = "月火水木金土日"


def now_jst():
    return datetime.now(JST)


def target_months():
    if MONTHS_ENV:
        return MONTHS_ENV
    today = now_jst().date()
    end = today + timedelta(days=92)
    y, m, out = today.year, today.month, []
    while (y, m) <= (end.year, end.month):
        out.append(f"{y}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def http_get_json(url, params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{q}",
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": "https://license-test.tokyo-madoguchi-yoyaku.com",
            "Referer": "https://license-test.tokyo-madoguchi-yoyaku.com/",
            "User-Agent": "Mozilla/5.0 (personal slot watcher; low frequency)",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_month(place, month):
    """returns (published: bool, {YYYYMMDD: remaining})"""
    data = http_get_json(
        API_URL,
        {"date": month, "coursecode": COURSE_CODE, "placecode": place, "user": "pub"},
    )
    if data.get("code") != "A0001":
        return False, {}
    days = {}
    for row in data.get("body") or []:
        d = str(row.get("date", ""))
        rem = max(0, int(row.get("capacity", 0)) - int(row.get("reservation", 0)))
        days[d] = days.get(d, 0) + rem
    return len(days) > 0, days


def extract_notice_lines(html_text):
    """告知ページの本文から『〇月分／〇月以降』と『公開/受付/予約』を含む文を抜き出す"""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", html_text)
    s = html.unescape(re.sub(r"<[^>]+>", "\n", s))
    out = []
    for line in s.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if 5 <= len(line) <= 300 and re.search(r"[0-9０-９]+月(分|以降)", line) \
                and any(k in line for k in ("公開", "受付", "予約")):
            if line not in out:
                out.append(line)
    return out


def fetch_notice_lines():
    """警視庁『学科試験の完全予約制について』ページの予約月に関する文。取得失敗時は None"""
    try:
        req = urllib.request.Request(NOTICE_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return extract_notice_lines(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        print("告知ページ取得失敗:", e, file=sys.stderr)
        return None


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def fmt_date(d):
    dt = datetime.strptime(d, "%Y%m%d")
    return f"{dt.month}/{dt.day}({WEEK[dt.weekday()]})"


def send_line(text):
    """友だち全員に一斉送信（broadcast）。無料プランは月200通（人数分カウント）"""
    if not LINE_TOKEN:
        print("[LINE未設定] 送信内容:\n" + text)
        return
    body = json.dumps({"messages": [{"type": "text", "text": text[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/broadcast",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("LINE送信:", r.status)
    except urllib.error.HTTPError as e:
        # 月の送信上限超過（429）などでも状態保存は続ける
        print("LINE送信失敗:", e.code, e.read()[:300], file=sys.stderr)


def diff(prev_days, cur_days, published_before, cutoff):
    """新しく空いた日（前回0 or 無し → 今回>0）を返す"""
    opened = []
    for d, rem in sorted(cur_days.items()):
        if d < cutoff or rem <= 0:
            continue
        if prev_days.get(d, 0) <= 0:
            opened.append((d, rem))
    return opened


def run(test=False):
    st = load_state()
    first_run = not st.get("days")
    prev = st.get("days", {})          # "place|YYYYMMDD" -> remaining
    published = set(st.get("published", []))  # "place|YYYYMM"
    cutoff = (now_jst() + timedelta(days=1)).strftime("%Y%m%d")  # 明日以降のみ対象

    months = target_months()
    cur, errors = {}, []
    new_release, opened = [], []
    for place in PLACE_CODES:
        for month in months:
            key_m = f"{place}|{month}"
            try:
                is_pub, days = fetch_month(place, month)
            except Exception as e:
                errors.append(f"{PLACES.get(place, place)} {month}: {e}")
                # 失敗時は前回値を維持（誤検知防止）
                for k, v in prev.items():
                    if k.startswith(place + "|" + month):
                        cur[k] = v
                continue
            if is_pub and key_m not in published:
                published.add(key_m)
                if not first_run:
                    total = sum(v for d, v in days.items() if d >= cutoff)
                    new_release.append((month, place, total))
            pdays = {k.split("|")[1]: v for k, v in prev.items() if k.startswith(place + "|" + month)}
            for d, v in days.items():
                cur[f"{place}|{d}"] = v
            # 前回から公開済みの月だけ「新たに空いた日」を判定（初公開時は公開通知でまとめて知らせる）
            if not first_run and key_m in st.get("published", []):
                for d, rem in diff(pdays, days, True, cutoff):
                    opened.append((place, d, rem))

    # 警視庁HPのお知らせ（新しい月の公開日時の事前告知）
    notice_lines = fetch_notice_lines()
    # 抜き出し方を変えたので、旧バージョンの保存内容は基準として使わない（誤通知防止）
    prev_notice = st.get("notice_lines") if st.get("notice_ver") == 2 else None
    new_notice = []
    if notice_lines is not None and prev_notice is not None:
        new_notice = [l for l in notice_lines if l not in prev_notice]

    # ---- 通知文 ----
    lines = []
    if new_notice:
        lines.append("📢 警視庁HPに予約枠の公開についてお知らせが出ました")
        for l in new_notice[:3]:
            lines.append(f"「{l[:150]}」")
        lines.append(NOTICE_URL)
    slot = []
    for month in sorted({r[0] for r in new_release}):
        if slot:
            slot.append("")
        slot.append(f"🆕 {int(month[4:])}月の学科試験 予約枠が公開されました")
        for mo, place, total in new_release:
            if mo == month:
                slot.append(f"・{PLACES.get(place, place)}：空き合計 {total}名")
    if opened:
        if slot:
            slot.append("")
        slot.append("✅ 空きが出ました（キャンセル等）")
        for place, d, rem in opened[:20]:
            slot.append(f"・{PLACES.get(place, place)} {fmt_date(d)} 残り{rem}名")
        if len(opened) > 20:
            slot.append(f"…ほか{len(opened) - 20}件")
    if slot:
        slot += ["", "予約はこちら（早い者勝ち）", SITE_URL]
        lines += ([""] if lines else []) + slot

    fail_count = st.get("fail_count", 0) + 1 if errors and len(errors) == len(PLACE_CODES) * len(months) else 0
    if fail_count == ERROR_ALERT_AFTER:
        lines = ["⚠️ 空き枠の取得に連続で失敗しています。仕組みが止まっている可能性があります。", errors[0][:300]] + ([""] + lines if lines else [])

    if test:
        summary = ["【テスト】空き枠ウォッチャーは動いています"]
        for place in PLACE_CODES:
            for month in months:
                ds = {k.split("|")[1]: v for k, v in cur.items() if k.startswith(place + "|" + month)}
                avail = sum(v for d, v in ds.items() if d >= cutoff)
                state = "未公開" if f"{place}|{month}" not in published else f"空き合計 {avail}名"
                summary.append(f"・{PLACES.get(place, place)} {int(month[4:])}月：{state}")
        if notice_lines is None:
            summary.append("・警視庁HP：取得できませんでした")
        elif any("公開しておりません" in l for l in notice_lines):
            summary.append("・警視庁HP：次の月の公開日はまだ告知なし（監視中）")
        else:
            summary.append("・警視庁HP：" + (notice_lines[0][:100] if notice_lines else "公開日の記載なし（監視中）"))
        if errors:
            summary.append("取得エラー: " + errors[0][:200])
        send_line("\n".join(summary))
    elif lines:
        send_line("\n".join(lines))
    else:
        print("変化なし", now_jst().isoformat(), "errors:", errors)

    st.update({
        "days": cur,
        "published": sorted(published),
        "notice_lines": notice_lines if notice_lines is not None else prev_notice,
        "notice_ver": 2 if (notice_lines is not None or prev_notice is not None) else st.get("notice_ver"),
        "fail_count": fail_count,
        "last_run": now_jst().isoformat(),
    })
    save_state(st)
    for e in errors:
        print("ERROR", e, file=sys.stderr)


if __name__ == "__main__":
    run(test="--test" in sys.argv)
