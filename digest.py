"""
VLA / Physical AI Daily Digest
- arxiv RSS + 기업 블로그 RSS 수집
- Claude API(Sonnet 5)로 Medium 스타일 한국어 요약
- Gmail로 매일 발송

2026-07-16 개정 내역
  1. 모델 claude-sonnet-4-6 -> claude-sonnet-5
  2. adaptive thinking 대응: content[0].text 직접 접근 제거, type=="text" 블록만 추출
  3. rank 호출 max_tokens 100 -> 4000 (thinking이 예산을 함께 소비하므로 헤드룸 필요)
  4. 트렌드를 별도 API 호출로 분리 (출력 절단 시 항상 트렌드가 먼저 희생되던 문제 해결)
  5. stop_reason / usage 로깅 + 절단 감지 가드
  6. 카드 생성은 스트리밍 사용 (출력 16K 초과 시 타임아웃 방지 권장사항)
"""

import os
import re
import smtplib
import feedparser
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SENDER_EMAIL      = os.environ["SENDER_EMAIL"]
SENDER_PASSWORD   = os.environ["SENDER_APP_PASSWORD"]
RECIPIENT_EMAIL   = os.environ["RECIPIENT_EMAIL"]

# ---- 모델 설정 -------------------------------------------------------------
# claude-sonnet-5 는 날짜 없는 형식이지만 evergreen 포인터가 아니라 고정 스냅샷.
# 모델이 조용히 바뀌는 일은 없음.
MODEL_RANK      = "claude-sonnet-5"
MODEL_SUMMARIZE = "claude-sonnet-5"

# Sonnet 5 새 토크나이저는 같은 텍스트에 ~30% 더 많은 토큰을 사용.
# 한국어 + HTML 이라 논문 1편당 대략 1200~2000 토큰으로 잡고 여유 있게 설정.
MAX_TOKENS_RANK   = 4000    # thinking + 텍스트 합산 한도
MAX_TOKENS_CARDS  = 32000   # 16K 초과 가능 -> 스트리밍 필수
MAX_TOKENS_TRENDS = 8000

MAX_FETCH = 30
MAX_ITEMS = 10
DAYS_BACK = 1

RSS_SOURCES = [
    {"name": "arxiv: Robotics",         "url": "https://arxiv.org/rss/cs.RO", "type": "arxiv"},
    {"name": "arxiv: Machine Learning", "url": "https://arxiv.org/rss/cs.LG", "type": "arxiv"},
    {"name": "arxiv: Computer Vision",  "url": "https://arxiv.org/rss/cs.CV", "type": "arxiv"},
    {"name": "arxiv: AI",               "url": "https://arxiv.org/rss/cs.AI", "type": "arxiv"},
    {"name": "NVIDIA Technical Blog",   "url": "https://developer.nvidia.com/blog/feed/", "type": "blog"},
    {"name": "Google DeepMind Blog",    "url": "https://deepmind.google/blog/rss.xml", "type": "blog"},
    {"name": "Meta AI Blog",            "url": "https://ai.meta.com/blog/rss/", "type": "blog"},
    {"name": "Hugging Face Blog",       "url": "https://huggingface.co/blog/feed.xml", "type": "blog"},
    {"name": "Papers With Code",        "url": "https://paperswithcode.com/rss", "type": "blog"},
]

INCLUDE_KEYWORDS = [
    "vision language action", "VLA", "embodied", "physical ai", "physical intelligence",
    "diffusion policy", "flow matching", "action tokenization", "action chunking",
    "world model", "foundation model", "imitation learning", "behavior cloning",
    "manipulation policy", "robot learning", "GR00T", "Isaac Lab", "Isaac Gym",
    "pi0", "openpi", "octo", "openvla", "rt-2", "rt2", "palm-e", "palme",
    "aloha", "act policy", "sim-to-real", "sim2real", "dexterous manipulation",
    "language conditioned", "language-conditioned", "multi-modal", "multimodal robot",
]

EXCLUDE_KEYWORDS = [
    "actuator design", "gripper design", "mechanical design",
    "sensor calibration", "hardware prototype", "circuit", "pcb", "microcontroller",
]


# ---------------------------------------------------------------------------
# Claude API 헬퍼
# ---------------------------------------------------------------------------

def extract_text(msg) -> str:
    """Sonnet 5는 adaptive thinking이 기본 on이므로 content[0]이 thinking 블록일 수 있다.
    반드시 type == "text" 블록만 골라서 이어붙여야 한다.
    (기존 코드의 msg.content[0].text 는 Sonnet 5에서 깨짐)
    """
    return "".join(
        block.text for block in msg.content if getattr(block, "type", None) == "text"
    ).strip()


def check_truncation(msg, label: str) -> bool:
    """stop_reason과 토큰 사용량을 로깅하고 절단 여부를 반환.
    max_tokens 절단을 조용히 넘기면 잘린 뉴스레터가 그대로 발송된다.
    """
    usage = getattr(msg, "usage", None)
    in_tok  = getattr(usage, "input_tokens", "?") if usage else "?"
    out_tok = getattr(usage, "output_tokens", "?") if usage else "?"
    print(f"[DEBUG] {label}: stop_reason={msg.stop_reason} in={in_tok} out={out_tok}")

    if msg.stop_reason == "max_tokens":
        print(f"[ERROR] {label} 출력이 max_tokens 한도에서 절단됨 -> 한도 상향 필요")
        return True
    return False


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------

def is_relevant(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw.lower() in text:
            return False
    for kw in INCLUDE_KEYWORDS:
        if kw.lower() in text:
            return True
    return False


def fetch_items() -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK + 1)
    items, seen = [], set()

    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
        except Exception as e:
            print(f"[WARN] {source['name']} 수집 실패: {e}")
            continue

        for entry in feed.entries:
            title   = entry.get("title", "").strip()
            summary = entry.get("summary", entry.get("description", ""))[:1500]
            link    = entry.get("link", "")

            if link in seen:
                continue
            seen.add(link)

            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < since:
                    continue

            if not is_relevant(title, summary):
                continue

            items.append({
                "source":  source["name"],
                "type":    source["type"],
                "title":   title,
                "summary": re.sub(r"<[^>]+>", " ", summary).strip(),
                "url":     link,
            })

    items_arxiv = [i for i in items if i["type"] == "arxiv"]
    items_blog  = [i for i in items if i["type"] == "blog"]
    merged = (items_arxiv + items_blog)[:MAX_FETCH]
    print(f"[INFO] 수집: arxiv {len(items_arxiv)}개, 블로그 {len(items_blog)}개 → 후보 {len(merged)}개")
    return merged


# ---------------------------------------------------------------------------
# 랭킹
# ---------------------------------------------------------------------------

def rank_items(items: list[dict]) -> list[dict]:
    if len(items) <= MAX_ITEMS:
        print(f"[INFO] 후보 {len(items)}개 → 랭킹 생략")
        return items

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    items_text = ""
    for i, item in enumerate(items, 1):
        items_text += f"[{i}] {item['title']}\n내용: {item['summary'][:300]}\n---\n"

    prompt = f"""당신은 VLA/Physical AI 모델 연구자입니다.
아래 {len(items)}개 중 가장 중요한 {MAX_ITEMS}개 번호를 골라주세요.

높은 우선순위: 새 아키텍처/방법론 제안, VLA·Diffusion Policy·Flow Matching 혁신, DeepMind·NVIDIA·Meta·CMU·Stanford 발표, SOTA 개선
낮은 우선순위: 좁은 도메인 응용, 하드웨어 위주, 단순 벤치마크

{items_text}

마지막 줄에 중요한 순서대로 {MAX_ITEMS}개 번호만 콤마로 출력하세요.
예: 3,7,1,12,5,9,2,8,11,4"""

    msg = client.messages.create(
        model=MODEL_RANK,
        max_tokens=MAX_TOKENS_RANK,   # thinking이 예산을 함께 쓰므로 100은 불가
        messages=[{"role": "user", "content": prompt}],
    )
    check_truncation(msg, "rank")

    try:
        text = extract_text(msg)
        # thinking 블록은 이미 제외됐지만, 마지막 줄만 파싱해 안전성 확보
        last_line = [ln for ln in text.splitlines() if ln.strip()][-1]
        nums = re.findall(r"\d+", last_line)
        indices = [int(x) - 1 for x in nums]
        ranked, used = [], set()
        for i in indices:
            if 0 <= i < len(items) and i not in used:
                ranked.append(items[i])
                used.add(i)
        if not ranked:
            raise ValueError("파싱된 유효 인덱스 없음")
        print(f"[INFO] 랭킹 완료 → 상위 {len(ranked)}개")
        return ranked[:MAX_ITEMS]
    except Exception as e:
        print(f"[WARN] 랭킹 파싱 실패: {e} → 앞에서 {MAX_ITEMS}개 사용")
        return items[:MAX_ITEMS]


# ---------------------------------------------------------------------------
# 요약 (카드 / 트렌드를 분리 호출)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """당신은 VLA(Vision-Language-Action) 및 Physical AI 분야의 전문 리서처입니다.
매일 논문과 기술 블로그를 Medium 스타일의 한국어 뉴스레터로 정리합니다.

독자는 VLA/Embodied AI 모델 연구자입니다. 다음 원칙을 지켜주세요:
1. 로봇 하드웨어 이야기는 최소화 — 모델 아키텍처·학습 방법론·알고리즘에 집중
2. 전문 용어는 영어 유지, 설명은 한국어
3. 요청받은 HTML 구조를 정확히 지키고, html/head 태그 없이 body 내용만 출력"""


def _items_text(items: list[dict], clip: int = 800) -> str:
    out = ""
    for i, item in enumerate(items, 1):
        out += (
            f"\n[{i}] [{item['source']}] {item['title']}\n"
            f"URL: {item['url']}\n내용: {item['summary'][:clip]}\n---"
        )
    return out


def summarize_cards(items: list[dict]) -> tuple[str, bool]:
    """논문/블로그 카드만 생성. 트렌드는 별도 호출."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""아래 {len(items)}개 아이템을 분석해 HTML 뉴스레터의 '카드' 부분을 작성해 주세요.
트렌드 섹션은 작성하지 마세요 (별도로 생성합니다).

{_items_text(items)}

각 아이템마다: 한 줄 요약, 핵심 방법론(2~3문장), 기존 대비 차별점, 왜 중요한가

출력 HTML 구조:
1. arxiv 논문: <div class="section" id="papers">
   <div class="section-header">📄 오늘의 논문</div>
   각 논문: <div class="card paper-card">
     <h3 class="card-title"><a href="URL">제목</a></h3>
     <p class="source-badge">출처</p>
     <div class="one-liner">한 줄 요약</div>
     <div class="method"><strong>📐 방법론:</strong> ...</div>
     <div class="diff"><strong>✨ 차별점:</strong> ...</div>
     <div class="why"><strong>💡 왜 중요한가:</strong> ...</div>
     <div class="tags"><span class="tag">VLA</span> 형태</div>
   </div>

2. 블로그/뉴스가 있으면: <div class="section" id="blogs">
   <div class="section-header">📰 블로그 · 릴리스</div>
   각 아이템: <div class="card blog-card"> (간결하게)

HTML body 내용만 출력. 트렌드 섹션 금지."""

    # 출력이 16K 토큰을 넘길 수 있으므로 스트리밍 사용 (타임아웃 방지)
    with client.messages.stream(
        model=MODEL_SUMMARIZE,
        max_tokens=MAX_TOKENS_CARDS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        msg = stream.get_final_message()

    truncated = check_truncation(msg, "cards")
    return extract_text(msg), truncated


def summarize_trends(items: list[dict]) -> tuple[str, bool]:
    """트렌드 전용 호출.
    카드와 같은 호출에 묶여 있으면 출력 마지막이라 항상 먼저 절단된다.
    분리하면 논문 수가 늘어도 트렌드는 절대 잘리지 않는다.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""아래 {len(items)}개 아이템을 관통하는 연구 흐름 3가지를 뽑아 주세요.

{_items_text(items, clip=500)}

작성 규칙:
- 정확히 3개의 트렌드
- 각 트렌드는 해당하는 논문 제목을 본문에 명시할 것
- {len(items)}개 아이템이 **모두** 최소 하나의 트렌드에 포함되어야 함 (누락 금지)
- 각 트렌드 3~4문장. 단순 나열이 아니라 왜 이들이 같은 흐름인지 해석할 것
- 내용은 트랜드와 해당 트랜드에 해당하는 논문의 내용에 충실해야 함

출력 HTML 구조:
<div class="section" id="trends">
  <div class="section-header">📊 오늘의 트렌드</div>
  <div class="trend-item"><h3>[emoji] 굵은 제목</h3><p>본문</p></div>
  ... 3개
</div>

HTML만 출력."""

    msg = client.messages.create(
        model=MODEL_SUMMARIZE,
        max_tokens=MAX_TOKENS_TRENDS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    truncated = check_truncation(msg, "trends")
    return extract_text(msg), truncated


def build_body(items: list[dict]) -> tuple[str, bool]:
    if not items:
        return "<p style='padding:24px'>오늘은 관련 업데이트가 없습니다.</p>", False

    cards, t1 = summarize_cards(items)

    try:
        trends, t2 = summarize_trends(items)
    except Exception as e:
        print(f"[WARN] 트렌드 생성 실패: {e} → 카드만 발송")
        trends, t2 = "", False

    return cards + trends, (t1 or t2)


# ---------------------------------------------------------------------------
# HTML / 발송
# ---------------------------------------------------------------------------

def build_html(body: str, item_count: int) -> str:
    today = datetime.now().strftime("%Y년 %m월 %d일")
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: #f0f2f5; color: #1a1a2e; }}
  .wrap {{ max-width: 680px; margin: 0 auto; }}
  .header {{ background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%); padding: 40px 32px; text-align: center; }}
  .header h1 {{ color: #fff; font-size: 24px; font-weight: 700; }}
  .header h1 span {{ color: #a78bfa; }}
  .header .date {{ color: #c4b5fd; font-size: 13px; margin-top: 8px; }}
  .header .stats {{ display: inline-block; background: rgba(255,255,255,0.1); border-radius: 20px; padding: 6px 16px; margin-top: 12px; color: #e9d5ff; font-size: 12px; }}
  .section {{ background: white; margin: 16px 0; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
  .section-header {{ padding: 18px 24px; border-bottom: 1px solid #f3f4f6; font-weight: 700; font-size: 15px; color: #374151; background: #fafafa; }}
  .card {{ padding: 20px 24px; border-bottom: 1px solid #f3f4f6; }}
  .card:last-child {{ border-bottom: none; }}
  .card-title {{ font-size: 15px; font-weight: 700; line-height: 1.5; margin-bottom: 4px; }}
  .card-title a {{ color: #1d4ed8; text-decoration: none; }}
  .source-badge {{ font-size: 11px; color: #6b7280; margin-bottom: 12px; background: #f3f4f6; display: inline-block; padding: 2px 8px; border-radius: 4px; }}
  .one-liner {{ font-size: 14px; font-weight: 600; color: #111827; background: #eff6ff; border-left: 3px solid #3b82f6; padding: 8px 12px; border-radius: 0 6px 6px 0; margin-bottom: 10px; }}
  .method, .diff, .why {{ font-size: 13px; color: #374151; line-height: 1.7; margin-bottom: 8px; }}
  .tags {{ margin-top: 12px; }}
  .tag {{ display: inline-block; background: #f5f3ff; color: #7c3aed; font-size: 11px; padding: 3px 8px; border-radius: 4px; margin: 2px 3px 2px 0; font-weight: 500; }}
  .blog-card .one-liner {{ background: #f0fdf4; border-left-color: #22c55e; }}
  #trends .section-header {{ background: linear-gradient(135deg, #1e1b4b, #312e81); color: white; }}
  .trend-item {{ padding: 16px 24px; border-bottom: 1px solid #f3f4f6; }}
  .trend-item:last-child {{ border-bottom: none; }}
  .trend-item h3 {{ font-size: 14px; font-weight: 700; color: #1e1b4b; }}
  .trend-item p {{ font-size: 13px; color: #4b5563; line-height: 1.7; margin-top: 4px; }}
  .footer {{ padding: 24px; text-align: center; color: #9ca3af; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>🤖 VLA & <span>Physical AI</span> Digest</h1>
    <div class="date">{today}</div>
    <div class="stats">오늘 {item_count}개 업데이트 · arxiv + NVIDIA + DeepMind + Meta AI + HuggingFace</div>
  </div>
  {body}
  <div class="footer">
    <p>arxiv cs.RO / cs.LG / cs.CV · NVIDIA Blog · Google DeepMind · Meta AI · Hugging Face · Papers With Code</p>
    <p style="margin-top:4px">모델 아키텍처 · 학습 방법론 · Embodied AI 연구 중심 큐레이션</p>
  </div>
</div>
</body>
</html>"""


def send_email(html: str, item_count: int, truncated: bool = False):
    today = datetime.now().strftime("%m/%d")
    flag = "⚠️ " if truncated else ""
    subject = f"{flag}[VLA Digest {today}] Physical AI · 방법론 · arxiv 업데이트 {item_count}건"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    print(f"[INFO] 이메일 발송 완료 → {RECIPIENT_EMAIL}")


if __name__ == "__main__":
    print(f"[START] VLA Digest 시작 (model={MODEL_SUMMARIZE})")
    items = fetch_items()
    items = rank_items(items)
    body, truncated = build_body(items)
    html = build_html(body, len(items))
    send_email(html, len(items), truncated)
    print(f"[DONE] 완료 (truncated={truncated})")
