import os
import json
import smtplib
from email.message import EmailMessage
import requests
from openai import OpenAI

# 1. AI 与检索配置
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
SEARCH_API_KEY = os.environ["SEARCH_API_KEY"]

# 2. SMTP 邮件配置
SMTP_HOST = os.environ["SMTP_HOST"]          # 例如: smtp.gmail.com / mail.gmx.net / smtp.web.de
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]          # 发件邮箱账号
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]  # 邮箱应用专用密码（App Password）
EMAIL_TO = os.environ["EMAIL_TO"]            # 收件邮箱（可填自己的邮箱）

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


# 3. 检索 50ml 报价与当天折扣
def fetch_50ml_data():
    queries = [
        '"Le Labo" "Santal 33" "50 ml" OR "50ml" Eau de Parfum Preis kaufen idealo breuninger 24s',
        'breuninger 24s printemps gutschein rabattcode beauty le labo aktuell'
    ]
    snippets = []
    for q in queries:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": SEARCH_API_KEY,
                "query": q,
                "search_depth": "advanced",
                "max_results": 8
            },
            timeout=30
        )
        resp.raise_for_status()
        for r in resp.json().get("results", []):
            snippets.append(
                f"标题: {r.get('title')}\n链接: {r.get('url')}\n内容: {r.get('content')}\n---"
            )
    return "\n".join(snippets)


# 4. 让 AI 提取 50ml 报价与折扣、免邮门槛参数
def extract_50ml_offers(raw_text):
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
    res = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_text}
        ],
        temperature=0.1
    )
    return json.loads(res.choices[0].message.content).get("offers", [])


# 5. 精确计算 50ml 折后价、折后免邮校验及最终到手总价
def calculate_50ml_final_price(offers):
    ranked = []
    for item in offers:
        price = float(item.get("original_price_eur", 0))
        if price < 130 or price > 250:
            continue

        discount_amount = 0.0
        if price >= float(item.get("min_spend_eur", 0)):
            d_type = item.get("discount_type")
            d_val = float(item.get("discount_value", 0))
            if d_type == "percent":
                discount_amount = price * (d_val / 100.0)
            elif d_type == "fixed":
                discount_amount = d_val

        discounted_price = round(price - discount_amount, 2)

        free_threshold = float(item.get("free_shipping_threshold_eur", 0))
        std_shipping = float(item.get("standard_shipping_eur", 0))
        actual_shipping = 0.0 if discounted_price >= free_threshold else std_shipping

        final_total = round(discounted_price + actual_shipping, 2)

        ranked.append({
            **item,
            "discounted_price_eur": discounted_price,
            "actual_shipping_eur": actual_shipping,
            "final_total_eur": final_total
        })

    return sorted(ranked, key=lambda x: x["final_total_eur"])


# 6. 通过 SMTP 发送邮件报告
def send_email_report(ranked):
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    if not ranked:
        msg["Subject"] = "Le Labo Santal 33 (50ml) 每日比价：今日暂无有效报价"
        msg.set_content("今日未抓取到有效的 Le Labo Santal 33 (50ml) 报价，请检查搜索词或数据源。")
    else:
        best = ranked[0]
        is_deal = best["final_total_eur"] <= 190.0
        prefix = "【好价提醒】" if is_deal else "【每日比价】"
        msg["Subject"] = f"{prefix} Le Labo Santal 33 (50ml) 最低到手 {best['final_total_eur']} € - {best['retailer']}"

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
        <div style="font-family: Arial, sans-serif; line-height: 1.6; color: #222;">
          <h2>{"🔥 50ml 出现好价！" if is_deal else "🧴 Le Labo Santal 33 (50ml) 每日比价"}</h2>
          <p style="font-size: 18px;">
            <strong>今日最低到手总价（含运费）：<span style="color: #b91c1c;">{best['final_total_eur']} €</span></strong>
          </p>
          <ul>
            <li><strong>购买渠道：</strong>{best['retailer']}</li>
            <li><strong>价格拆解：</strong>原价 {best['original_price_eur']} € &rarr; 折后 {best['discounted_price_eur']} € + 运费 {best['actual_shipping_eur']} €</li>
            <li><strong>折扣说明：</strong>{best['promo_code']}（{best['conditions']}）</li>
            <li><strong>商品直达：</strong><a href="{best['url']}">打开购买页面</a></li>
          </ul>
          <p style="font-size: 12px; color: #666;">参考基准：德国 50ml 日常专柜正价约 212 €；建议重点关注 190 € 及以下到手价。</p>
        </div>
        """

        msg.set_content(plain_text)
        msg.add_alternative(html_content, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


if __name__ == "__main__":
    raw = fetch_50ml_data()
    offers = extract_50ml_offers(raw)
    ranked = calculate_50ml_final_price(offers)
    send_email_report(ranked)
