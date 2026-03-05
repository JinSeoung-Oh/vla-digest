"""
VLA / Physical AI Daily Digest
- arxiv RSS + 기업 블로그 RSS 수집
- Claude API로 Medium 스타일 한국어 요약
- Gmail로 매일 발송
"""

import os
import re
import smtplib
import feedparser
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import anthropic

# ─────────────────────────────────────────────
# 설정 (GitHub Secrets에서 주입)
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SENDER_EMAIL      = os.environ["SENDER_EMAIL"]       # 보내는 Gmail
SENDER_PASSWORD   = os.environ["SENDER_APP_PASSWORD"] # Gmail 앱 비밀번호
RECIPIENT_EMAIL   = os.environ["RECIPIENT_EMAIL"]    # 받는 이메일

MAX_FETCH = 30        # 수집 후 랭킹 전 후보 수
MAX_ITEMS = 10       # 최종 발송 아이템 수
DAYS_BACK = 1        # 며칠치 가져올지

# ─────────────────────────────────────────────
# RSS 소스 정의
# ─────────────────────────────────────────────
RSS_SOURCES = [
    # arxiv - 모델/방법론 관련 카테고리
    {
        "name": "arxiv: Robotics",
        "url": "https://arxiv.org/rss/cs.RO",
        "type": "arxiv",
    },
    {
        "name": "arxiv: Machine Learning",
        "url": "https://arxiv.org/rss/cs.LG",
        "type": "arxiv",
    },
    {
        "name": "arxiv: Computer Vision",
        "url": "https://arxiv.org/rss/cs.CV",
        "type": "arxiv",
    },
    {
        "name": "arxiv: AI",
        "url": "https://arxiv.org/rss/cs.AI",
        "type": "arxiv",
    },
    # 기업 기술 블로그
    {
        "name": "NVIDIA Technical Blog",
        "url": "https://developer.nvidia.com/blog/feed/",
        "type": "blog",
    },
    {
        "name": "Google DeepMind Blog",
        "url": "https://deepmind.google/blog/rss.xml",
        "type": "blog",
    },
    {
        "name": "Meta AI Blog",
        "url": "https://ai.meta.com/blog/rss/",
        "type": "blog",
    },
    {
        "name": "Hugging Face Blog",
        "url": "https://huggingface.co/blog/feed.xml",
        "type": "blog",
    },
    {
        "name": "Papers With Code",
        "url": "https://paperswithcode.com/rss",
        "type": "blog",
    },
]

# ─────────────────────────────────────────────
# 필터 키워드 (모델/방법론 집중, 하드웨어 제외)
# ─────────────────────────────────────────────
INCLUDE_KEYWORDS = [
    "vision language action", "VLA",
    "embodied", "physical ai", "physical intelligence",
    "diffusion policy", "flow matching",
    "action tokenization", "action chunking", "action representation",
    "world model", "foundation model",
    "imitation learning", "behavior cloning",
    "manipulation policy", "robot learning",
    "GR00T", "Isaac Lab", "Isaac Gym",
    "pi0", "openpi", "octo", "openvla",
    "rt-2", "rt2", "palm-e", "palme",
    "aloha", "act policy", "chi",
    "sim-to-real", "sim2real",
    "reinforcement learning from human feedback",
    "dexterous manipulation",
    "language conditioned", "language-conditioned",
    "multi-modal", "multimodal robot",
]

EXCLUDE_KEYWORDS = [
    "actuator design", "gripper design", "mechanical design",
    "sensor calibration", "hardware prototype",
    "circuit", "pcb", "microcontroller",
]

def is_relevant(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    # 제외 키워드 먼저
    for kw in EXCLUDE_KEYWORDS:
        if kw.lower() in text:
            return False
    # 포함 키워드 하나라도 있으면 통과
    for kw in INCLUDE_KEYWORDS:
        if kw.lower() in text:
            return True
    return False

# ─────────────────────────────────────────────
# RSS 수집
# ─────────────────────────────────────────────
def fetch_items() -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK + 1)
    items = []
    seen = set()

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

            # 중복 제거
            if link in seen:
                continue
            seen.add(link)

            # 날짜 필터
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < since:
                    continue

            # 관련성 필터
            if not is_relevant(title, summary):
                continue

            items.append({
                "source":  source["name"],
                "type":    source["type"],
                "title":   title,
                "summary": re.sub(r"<[^>]+>", " ", summary).strip(),
                "url":     link,
            })

    # arxiv 우선, 그 다음 블로그 / 랭킹 전 후보 MAX_FETCH개
    items_arxiv = [i for i in items if i["type"] == "arxiv"]
    items_blog  = [i for i in items if i["type"] == "blog"]
    merged = (items_arxiv + items_blog)[:MAX_FETCH]
    print(f"[INFO] 수집된 아이템: arxiv {len(items_arxiv)}개, 블로그 {len(items_blog)}개 → 랭킹 후보 {len(merged)}개")
    return merged

# ─────────────────────────────────────────────
# Claude 요약
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """당신은 VLA(Vision-Language-Action) 및 Physical AI 분야의 전문 리서처입니다.
매일 논문과 기술 블로그를 Medium 스타일의 한국어 뉴스레터로 정리합니다.

독자는 VLA/Embodied AI 모델 연구자입니다. 다음 원칙을 지켜주세요:

1. 로봇 하드웨어 이야기는 최소화 — 모델 아키텍처·학습 방법론·알고리즘에 집중
2. 각 아이템마다:
   - 한 줄 요약 (무엇을 풀었는가)
   - 핵심 방법론 (기술적 디테일, 2~3문장)
   - 기존 대비 차별점
   - 왜 중요한가
3. 전문 용어(VLA, Diffusion Policy 등)는 영어 유지, 설명은 한국어
4. 마지막에 "오늘의 트렌드" 섹션: 오늘 내용에서 보이는 연구 흐름 3가지"""

def summarize(items: list[dict]) -> str:
    if not items:
        return "<p>오늘은 관련 업데이트가 없습니다.</p>"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    items_text = ""
    for i, item in enumerate(items, 1):
        items_text += f"""
[{i}] [{item['source']}] {item['title']}
URL: {item['url']}
내용: {item['summary'][:800]}
---"""

    prompt = f"""아래 {len(items)}개의 아이템을 분석해 HTML 뉴스레터 본문을 작성해 주세요.

{items_text}

출력 HTML 구조:
1. arxiv 논문 섹션: <div class="section" id="papers">
   - 각 논문: <div class="card paper-card">
     - <h3 class="card-title"><a href="URL">제목</a></h3>
     - <p class="source-badge">출처</p>
     - <div class="one-liner">한 줄 요약</div>
     - <div class="method"><strong>📐 방법론:</strong> ...</div>
     - <div class="diff"><strong>✨ 차별점:</strong> ...</div>
     - <div class="why"><strong>💡 왜 중요한가:</strong> ...</div>
     - <div class="tags">키워드 태그들 <span class="tag">VLA</span> 형태</div>

2. 블로그/뉴스 섹션: <div class="section" id="blogs">
   - 각 아이템: <div class="card blog-card"> (동일 구조, 간결하게)

3. 오늘의 트렌드: <div class="section" id="trends">
   - <div class="trend-item">으로 3가지 트렌드
   - 각각 emoji + 굵은 제목 + 2~3문장 설명

HTML body 내용만 출력 (html/head 태그 제외)."""

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text

# ─────────────────────────────────────────────
# 이메일 HTML 템플릿
# ─────────────────────────────────────────────
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

  /* Header */
  .header {{ background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%);
             padding: 40px 32px; text-align: center; }}
  .header h1 {{ color: #fff; font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }}
  .header h1 span {{ color: #a78bfa; }}
  .header .date {{ color: #c4b5fd; font-size: 13px; margin-top: 8px; }}
  .header .stats {{ display: inline-block; background: rgba(255,255,255,0.1);
                    border-radius: 20px; padding: 6px 16px; margin-top: 12px;
                    color: #e9d5ff; font-size: 12px; }}

  /* Section */
  .section {{ background: white; margin: 16px 0; border-radius: 12px;
              overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,0.06); }}
  .section-header {{ padding: 18px 24px; border-bottom: 1px solid #f3f4f6;
                     font-weight: 700; font-size: 15px; color: #374151;
                     background: #fafafa; }}
  .section-header .icon {{ margin-right: 8px; }}

  /* Cards */
  .card {{ padding: 20px 24px; border-bottom: 1px solid #f3f4f6; }}
  .card:last-child {{ border-bottom: none; }}
  .card-title {{ font-size: 15px; font-weight: 700; line-height: 1.5; margin-bottom: 4px; }}
  .card-title a {{ color: #1d4ed8; text-decoration: none; }}
  .card-title a:hover {{ text-decoration: underline; }}
  .source-badge {{ font-size: 11px; color: #6b7280; margin-bottom: 12px;
                   background: #f3f4f6; display: inline-block;
                   padding: 2px 8px; border-radius: 4px; }}
  .one-liner {{ font-size: 14px; font-weight: 600; color: #111827;
                background: #eff6ff; border-left: 3px solid #3b82f6;
                padding: 8px 12px; border-radius: 0 6px 6px 0;
                margin-bottom: 10px; }}
  .method, .diff, .why {{ font-size: 13px; color: #374151; line-height: 1.7;
                           margin-bottom: 8px; }}
  .tags {{ margin-top: 12px; }}
  .tag {{ display: inline-block; background: #f5f3ff; color: #7c3aed;
          font-size: 11px; padding: 3px 8px; border-radius: 4px;
          margin: 2px 3px 2px 0; font-weight: 500; }}

  /* Blog card */
  .blog-card .one-liner {{ background: #f0fdf4; border-left-color: #22c55e; }}

  /* Trends */
  #trends .section-header {{ background: linear-gradient(135deg, #1e1b4b, #312e81); color: white; }}
  .trend-item {{ padding: 16px 24px; border-bottom: 1px solid #f3f4f6; }}
  .trend-item:last-child {{ border-bottom: none; }}
  .trend-item p {{ font-size: 13px; color: #4b5563; line-height: 1.7; margin-top: 4px; }}

  /* Footer */
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

# ─────────────────────────────────────────────
# 이메일 발송
# ─────────────────────────────────────────────
def send_email(html: str, item_count: int):
    today = datetime.now().strftime("%m/%d")
    subject = f"[VLA Digest {today}] Physical AI · 방법론 · arxiv 업데이트 {item_count}건"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    print(f"[INFO] 이메일 발송 완료 → {RECIPIENT_EMAIL}")

# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("[START] VLA Digest 시작")
    items   = fetch_items()
    items   = rank_items(items)
    body    = summarize(items)
    html    = build_html(body, len(items))
    send_email(html, len(items))
    print("[DONE] 완료")

# ─────────────────────────────────────────────
# Claude 중요도 랭킹 (요약 전 단계)
# ─────────────────────────────────────────────
def rank_items(items: list[dict]) -> list[dict]:
    """Claude가 중요도 점수 매겨서 상위 MAX_ITEMS개만 반환"""
    if len(items) <= MAX_ITEMS:
        return items

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    items_text = ""
    for i, item in enumerate(items, 1):
        items_text += f"[{i}] {item['title']}\n내용: {item['summary'][:300]}\n---\n"

    prompt = f"""당신은 VLA/Physical AI 모델 연구자입니다.
아래 {len(items)}개 논문/블로그 중 가장 중요한 {MAX_ITEMS}개의 번호를 골라주세요.

선택 기준 (높은 점수):
- 새로운 모델 아키텍처나 학습 방법론 제안
- VLA, Diffusion Policy, Flow Matching, World Model 등 핵심 방법론 혁신
- DeepMind, NVIDIA, Meta, CMU, Stanford 등 주요 기관 발표
- 기존 SOTA 대비 명확한 개선

선택 기준 (낮은 점수):
- 특정 좁은 도메인 응용만 다루는 논문
- 하드웨어/센서 위주
- 기존 방법 단순 벤치마크

{items_text}

중요한 순서대로 {MAX_ITEMS}개 번호만 콤마로 출력하세요. 예: 3,7,1,12,5,9,2,8,11,4
번호 외 다른 텍스트 없이."""

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        indices = [int(x.strip()) - 1 for x in msg.content[0].text.split(",")]
        ranked = [items[i] for i in indices if 0 <= i < len(items)]
        print(f"[INFO] 랭킹 완료 → 상위 {len(ranked)}개 선택")
        return ranked[:MAX_ITEMS]
    except Exception as e:
        print(f"[WARN] 랭킹 파싱 실패, 순서대로 사용: {e}")
        return items[:MAX_ITEMS]
