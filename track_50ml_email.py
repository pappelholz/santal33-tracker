import json
import os
import smtplib
from email.message import EmailMessage

import requests
from google import genai
from google.genai import types

# ================= 1. 环境变量配置 =================
LLM_API_KEY = os.environ["LLM_API_KEY"]
SEARCH_API_KEY = os.environ["SEARCH_API_KEY"]

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

# 价格提醒阈值：德国 50ml 专柜日常正价约 212 €，低于 190 € 触发“好价提醒”
DEAL_THRESHOLD_EUR = 190.0
GEMINI_MODEL = "gemini-3.8-flash"

client = genai.Client(api_key=LLM_API_KEY)


# ================= 2. 检索实时报价与折扣 =================
def fetch_market_data() -> str:
    queries = [
        '"Le Labo" "Santal 33" "50 ml" OR "50ml" Eau de Parfum Preis kaufen idealo breuninger 24s',
        "breuninger 24s printemps gutschein rabattcode beauty le labo aktuell",
    ]
    snippets = []
    for query in queries:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": SEARCH_API_KEY,
                "query": query,
                "search_depth": "advanced",
                "max_results": 8,
            },
            timeout=30,
        )
        resp.raise_for_status()
        for item in resp.json().get("results", []):
            snippets.append(
                f"标题: {item.get('title')}\n"
                f"链接: {item.get('url')}\n"
                f"内容: {item.get('content')}\n---"
            )
    return "\n".join(snippets)


# ================= 3. AI 提取结构化价格与规则 =================
def extract_50ml_offers(raw_text: str) -> list[dict]:
    system_prompt = """
    你是欧洲美妆电商比价提取器。仅提取 Le Labo Santal 33 Eau de Parfum (EDP) 【50 ml 装】的欧元(€)在售报价。
    坚决剔除 15ml/30ml/100ml、身体护理、CHF标价及缺货商品。
    仅输出合法 JSON：
    {
      "offers": [
        {
          "retailer": "商家名",
          "original_price_eur": 212.00,
          "discount_type": "percent 或 fixed 或 none",
          "discount_value": 15.0,
          "min_spend_eur": 79.00,
          "promo_code": "折扣码或无",
          "free_shipping_threshold_eur": 149.00,
          "standard_shipping_eur": 5.95,
          "conditions": "触发条件",
          "url": "链接"
        }
      ]
    }
    """
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=raw_text,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(response.text).get("offers", [])


# ================= 4. 精确计算到手总价（含运费） =================
def calculate_and_rank(offers: list[dict]) -> list[dict]:
    ranked = []
    for item in offers:
        price = float(item.get("original_price_eur") or 0)
        # 过滤异常非 50ml 价格区间
        if not (130.0 <= price <= 250.0):
            continue

        discount_amount = 0.0
        min_spend = float(item.get("min_spend_eur") or 0)
        if price >= min_spend:
            d_type = item.get("discount_type")
            d_val = float(item.get("discount_value") or 0)
            if d_type == "percent":
                discount_amount = price * (d_val / 100.0)
            elif d_type == "fixed":
                discount_amount = d_val

        discounted_price = round(price - discount_amount, 2)

        # 折后价二次核验免邮门槛
        free_threshold = float(item.get("free_shipping_threshold_eur") or 0)
        std_shipping = float(item.get("standard_shipping_eur") or 0)
        actual_shipping = 0.0 if discounted_price >= free_threshold else std_shipping

        final_total = round(discounted_price + actual_shipping, 2)

        ranked.append(
            {
                **item,
                "discounted_price_eur": discounted_price,
                "actual_shipping_eur": actual_shipping,
                "final_total_eur": final_total,
            }
        )

    return sorted(ranked, key=lambda x: x["final_total_eur"])


# ================= 5. 发送比价邮件报告 =================
def send_email_report(ranked: list[dict]) -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    if not ranked:
        msg["Subject"] = "Le Labo Santal 33 (50ml) 每日比价：今日暂无有效报价"
        msg.set_content("今日未抓取到有效的 Le Labo Santal 33 (50ml) 报价，请检查搜索词或数据源。")
    else:
        best = ranked[0]
        is_deal = best["final_total_eur"] <= DEAL_THRESHOLD_EUR
        prefix = "好价- " if is_deal else "日报- "
