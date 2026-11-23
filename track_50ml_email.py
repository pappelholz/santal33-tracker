import json
import os
import smtplib
import time
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
CANDIDATE_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]

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


# ================= 3. AI 提取结构化价格与规则（含自动容错切换） =================
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
    last_err = None
    for model_name in CANDIDATE_MODELS:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=raw_text,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                )
                print(f"成功使用模型 {model_name} 完成数据提取")
                return json.loads(response.text).get("offers", [])
            except Exception as err:
                last_err = err
                print(f"模型 {model_name} 第 {attempt + 1} 次调用失败: {err}")
                time.sleep(3)

    raise RuntimeError(f"所有备用模型均暂时不可用: {last_err}")


# ================= 4. 精确计算到手总价（含运费） =================
def calculate_and_rank(offers: list[dict]) -> list[dict]:
    ranked = []
    for item in offers:
        price = float(item.get("original_price_eur") or 0)
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
        msg["Subject"] = (
            f"{prefix}Le Labo Santal 33 (50ml) 最低到手 {best['final_total_eur']} € - {best['retailer']}"
        )

        plain_text = (
            f"Le Labo Santal 33 Eau de Parfum (50ml) 每日最低到手报价\n"
            f"========================================\n"
            f"最低到手总价（含运费）：{best['final_total_eur']} €\n"
            f"购买渠道：{best['retailer']}\n"
            f"价格明细：原价 {best['original_price_eur']} € -> 折后 {best['discounted_price_eur']} € + 运费 {best['actual_shipping_eur']} €\n"
            f"优惠信息：{best['promo_code']}（{best['conditions']}）\n"
            f"商品链接：{best['url']}\n"
        )

        html_content = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #222; max-width: 600px;">
          <h2 style="margin-bottom: 12px;">{"🔥 50ml 出现好价！" if is_deal else "🧴 Le Labo Santal 33 (50ml) 每日比价"}</h2>
          <p style="font-size: 18px; background: #f8f9fa; padding: 12px; border-radius: 6px;">
            <strong>今日最低到手总价（含运费）：<span style="color: #b91c1c;">{best['final_total_eur']} €</span></strong>
          </p>
          <ul>
            <li><strong>购买渠道：</strong>{best['retailer']}</li>
            <li><strong>价格拆解：</strong>原价 {best['original_price_eur']} € &rarr; 折后 {best['discounted_price_eur']} € + 运费 {best['actual_shipping_eur']} €</li>
            <li><strong>折扣说明：</strong>{best['promo_code']}（{best['conditions']}）</li>
            <li><strong>商品直达：</strong><a href="{best['url']}" style="color: #2563eb;">点击前往购买页面</a></li>
          </ul>
          <p style="font-size: 12px; color: #666; border-top: 1px solid #eee; padding-top: 8px;">
            参考基准：德国 50ml 日常专柜正价约 212 €；当低于 {DEAL_THRESHOLD_EUR} € 时自动触发好价提醒。
          </p>
        </div>
        """

        msg.set_content(plain_text)
        msg.add_alternative(html_content, subtype="html")

    print(f"准备连接 SMTP: {SMTP_HOST}:{SMTP_PORT}, 发件人: {SMTP_USER}, 收件人: {EMAIL_TO}")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.set_debuglevel(1)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASSWORD)
        refused = server.send_message(msg)
        print("发送完成，被拒收列表:", refused)


# ================= 6. 主程序入口 =================
if __name__ == "__main__":
    raw_data = fetch_market_data()
    offers = extract_50ml_offers(raw_data)
    ranked_offers = calculate_and_rank(offers)
    send_email_report(ranked_offers)
