import json
import os
import smtplib
import time
from email.message import EmailMessage

import requests
from google import genai
from google.genai import types

# ================= 1. Konfiguration =================
LLM_API_KEY = os.environ["LLM_API_KEY"]
SEARCH_API_KEY = os.environ["SEARCH_API_KEY"]

SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
EMAIL_TO = os.environ["EMAIL_TO"]

DEAL_THRESHOLD_EUR = 190.0
CANDIDATE_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.8-flash"]

client = genai.Client(api_key=LLM_API_KEY)


# ================= 2. Marktdaten abrufen =================
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
                f"Titel: {item.get('title')}\n"
                f"URL: {item.get('url')}\n"
                f"Inhalt: {item.get('content')}\n---"
            )
    return "\n".join(snippets)


# ================= 3. Angebote extrahieren =================
def extract_50ml_offers(raw_text: str) -> list[dict]:
    system_prompt = """
    Du bist ein Preisvergleichs-Parser für den europäischen Kosmetikmarkt.
    Extrahiere ausschließlich lieferbare Euro-Angebote (€) für: Le Labo Santal 33 Eau de Parfum (EDP) 50 ml.
    Ignoriere konsequent 15ml, 30ml, 100ml, Körperpflege, CHF-Preise und ausverkaufte Artikel.
    Antworte nur mit gültigem JSON auf Deutsch:
    {
      "offers": [
        {
          "retailer": "Händlername",
          "original_price_eur": 212.00,
          "discount_type": "percent oder fixed oder none",
          "discount_value": 15.0,
          "min_spend_eur": 79.00,
          "promo_code": "Rabattcode oder Kein Code",
          "free_shipping_threshold_eur": 149.00,
          "standard_shipping_eur": 5.95,
          "conditions": "Bedingungen oder Keine",
          "url": "Link"
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
                        http_options=types.HttpOptions(timeout=15000),
                    ),
                )
                print(f"Extraktion erfolgreich mit Modell: {model_name}")
                return json.loads(response.text).get("offers", [])
            except Exception as err:
                last_err = err
                print(f"Modell {model_name} Versuch {attempt + 1} fehlgeschlagen: {err}")
                time.sleep(3)

    raise RuntimeError(f"Alle Modelle nicht erreichbar: {last_err}")


# ================= 4. Endpreis berechnen =================
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


# ================= 5. E-Mail senden (Kurz & auf Deutsch) =================
def send_email_report(ranked: list[dict]) -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO

    if not ranked:
        msg["Subject"] = "Santal 33 (50ml): Keine aktuellen Angebote"
        msg.set_content("Heute wurden keine gültigen Angebote für Le Labo Santal 33 (50ml) gefunden.")
    else:
        best = ranked[0]
        is_deal = best["final_total_eur"] <= DEAL_THRESHOLD_EUR
        tag = "[DEAL]" if is_deal else "[PREIS-CHECK]"
        msg["Subject"] = f"{tag} Santal 33 (50ml): {best['final_total_eur']} € ({best['retailer']})"

        body = (
            f"Le Labo Santal 33 EDP (50ml)\n"
            f"Endpreis inkl. Versand: {best['final_total_eur']} €\n"
            f"Händler: {best['retailer']}\n"
            f"Aufschlüsselung: {best['original_price_eur']} € -> {best['discounted_price_eur']} € + {best['actual_shipping_eur']} € Versand\n"
            f"Rabattcode: {best['promo_code']} ({best['conditions']})\n"
            f"Link: {best['url']}\n"
        )
        msg.set_content(body)

    print(f"SMTP-Verbindung zu {SMTP_HOST}:{SMTP_PORT} | Von: {SMTP_USER} | An: {EMAIL_TO}")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.set_debuglevel(1)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASSWORD)
        refused = server.send_message(msg)
        print("Versand abgeschlossen. Abgelehnt:", refused)


# ================= 6. Hauptprogramm =================
if __name__ == "__main__":
    raw_data = fetch_market_data()
    offers = extract_50ml_offers(raw_data)
    ranked_offers = calculate_and_rank(offers)
    send_email_report(ranked_offers)
