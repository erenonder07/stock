import os
import re
import sys
import json
import logging
import requests
from bs4 import BeautifulSoup

# Log ayarlari
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Ortam degiskenleri
PRODUCT_URL = (
    os.environ.get("PRODUCT_URL", "").strip()
    or "https://www.avva.com.tr/siyah-kapusonlu-su-itici-ruzgar-gecirmez-10-dereceye-dayanikli-sisme-mont-38173"
)

TARGET_SIZE = (os.environ.get("TARGET_SIZE", "").strip() or "L").upper()

TELEGRAM_TOKEN = (
    os.environ.get("TELEGRAM_TOKEN", "").strip()
    or "9175136192:AAGdTqs3slW6SVFm3nSloYepDudXwli2iZ0"
)

CHAT_ID = os.environ.get("CHAT_ID", "").strip() or "1531910597"

OUT_OF_STOCK_CLASSES = {
    "passive",
    "out-of-stock",
    "outofstock",
    "disabled",
    "tukendi",
    "stokyok",
    "stok-yok",
    "inactive",
    "no-stock",
    "passive-option"
}

OUT_OF_STOCK_TEXTS = [
    "tükendi",
    "tukendi",
    "stokta yok",
    "gelince haber ver"
]


def send_telegram_notification(product_url: str, target_size: str, product_title: str = "") -> bool:
    """Stok bulundugunda Telegram uzerinden bildirim gonderir."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning(
            f"Telegram bildirim gonderilemedi! "
            f"(TELEGRAM_TOKEN: {'Var' if TELEGRAM_TOKEN else 'Eksik'}, CHAT_ID: {'Var' if CHAT_ID else 'Eksik'})"
        )
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    title_text = f"<b>Ürün:</b> {product_title}\n" if product_title else ""
    message = (
        "🚨 <b>STOK BİLDİRİMİ!</b>\n\n"
        f"{title_text}"
        f"🎯 <b>Hedef Beden:</b> {target_size}\n"
        f"📦 <b>Durum:</b> Stokta Var!\n\n"
        f"🔗 <a href='{product_url}'>Ürünü Satın Almak İçin Tıklayın</a>"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    try:
        response = requests.post(api_url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("ok"):
            logger.info("Telegram bildirimi basariyla gonderildi!")
            return True
        else:
            logger.error(f"Telegram API hatasi: {data}")
            return False
    except Exception as e:
        logger.error(f"Telegram mesaji gonderilirken hata olustu: {e}")
        return False


def check_schema_org_json_ld(soup: BeautifulSoup, target_size: str):
    """
    Sayfadaki schema.org ProductGroup JSON-LD verilerini tarar.
    AVVA ve modern Ticimax sitelerinde varyantlar hasVariant listesinde yer alir.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
            if data.get("@type") == "ProductGroup":
                variants = data.get("hasVariant", [])
                for v in variants:
                    size = str(v.get("size", "")).strip().upper()
                    if size == target_size:
                        offers = v.get("offers", {})
                        availability = offers.get("availability", "")
                        is_in_stock = "InStock" in availability
                        logger.info(f"[JSON-LD] {target_size} bedeni bulundu. Durum: {availability} (Stokta: {is_in_stock})")
                        return True, is_in_stock
        except Exception as e:
            logger.debug(f"JSON-LD parse hatasi: {e}")
    return None


def check_ticimax_product_model(html_text: str, target_size: str):
    """
    AVVA'nin kullandigi Ticimax 'productDetailModel' JavaScript nesnesinden
    stok verilerini kesin olarak okur.
    """
    idx = html_text.find("var productDetailModel = ")
    if idx != -1:
        try:
            sub = html_text[idx + len("var productDetailModel = "):]
            obj, _ = json.JSONDecoder().raw_decode(sub)
            variant_data = obj.get("productVariantData", [])
            for item in variant_data:
                # ekSecenekTipiTanim: 'Beden' veya tipID: 2
                tip_tanim = str(item.get("ekSecenekTipiTanim", "")).lower()
                tip_id = item.get("tipID")
                if tip_tanim == "beden" or tip_id == 2:
                    tanim = str(item.get("tanim", "")).strip().upper()
                    if tanim == target_size:
                        stock_qty = item.get("stokAdedi", 0)
                        logger.info(f"[Ticimax Model] {target_size} bedeni bulundu. Stok Adedi: {stock_qty}")
                        return True, (stock_qty > 0)
        except Exception as e:
            logger.debug(f"Ticimax model parse hatasi: {e}")
    return None


def is_element_out_of_stock(elem) -> bool:
    """Verilen HTML elemaninin veya ust kapsayicilarinin stok disi olup olmadigini kontrol eder."""
    current = elem
    for _ in range(4):
        if current is None:
            break

        classes = current.get("class", [])
        if isinstance(classes, list):
            class_str = " ".join(classes).lower()
        else:
            class_str = str(classes).lower()

        for oos in OUT_OF_STOCK_CLASSES:
            if oos in class_str:
                return True

        attrs = current.attrs
        if "disabled" in attrs:
            return True

        data_stock = attrs.get("data-stock") or attrs.get("data-stok") or attrs.get("stock")
        if data_stock is not None and str(data_stock).strip() == "0":
            return True

        data_active = attrs.get("data-aktif") or attrs.get("data-active")
        if data_active is not None and str(data_active).strip().lower() in ["false", "0"]:
            return True

        title_attr = str(attrs.get("title", "")).lower()
        for oos_text in OUT_OF_STOCK_TEXTS:
            if oos_text in title_attr:
                return True

        current = current.parent

    elem_text = elem.get_text(separator=" ", strip=True).lower()
    for oos_text in OUT_OF_STOCK_TEXTS:
        if oos_text in elem_text:
            return True

    return False


def check_stock():
    logger.info("Stok kontrolü başlatılıyor...")
    logger.info(f"Ürün URL: {PRODUCT_URL}")
    logger.info(f"Hedef Beden: {TARGET_SIZE}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    try:
        response = requests.get(PRODUCT_URL, headers=headers, timeout=20)
        logger.info(f"HTTP Yanıt Kodu: {response.status_code}")
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Sayfaya erişilirken HTTP hatası meydana geldi: {e}")
        sys.exit(1)

    soup = BeautifulSoup(response.content, "html.parser")

    # Ürün başlığı
    product_title = ""
    title_elem = (
        soup.find("h1", class_=re.compile(r"product.*title|title", re.I))
        or soup.find("h1")
        or soup.find("title")
    )
    if title_elem:
        product_title = title_elem.get_text(strip=True)
        logger.info(f"Ürün Adı: {product_title}")

    # YÖNTEM 1: Schema.org JSON-LD verisi
    ld_result = check_schema_org_json_ld(soup, TARGET_SIZE)
    if ld_result is not None:
        size_found, in_stock = ld_result
        if size_found:
            if in_stock:
                logger.info(f"🎉 MÜJDE! {TARGET_SIZE} bedeni STOKTA! Telegram bildirimi gönderiliyor...")
                send_telegram_notification(PRODUCT_URL, TARGET_SIZE, product_title)
            else:
                logger.info(f"⏳ {TARGET_SIZE} bedeni şu anda stokta bulunmuyor (Tükenmiş).")
            return

    # YÖNTEM 2: Ticimax Product Detail Modeli
    model_result = check_ticimax_product_model(response.text, TARGET_SIZE)
    if model_result is not None:
        size_found, in_stock = model_result
        if size_found:
            if in_stock:
                logger.info(f"🎉 MÜJDE! {TARGET_SIZE} bedeni STOKTA! Telegram bildirimi gönderiliyor...")
                send_telegram_notification(PRODUCT_URL, TARGET_SIZE, product_title)
            else:
                logger.info(f"⏳ {TARGET_SIZE} bedeni şu anda stokta bulunmuyor (Tükenmiş).")
            return

    # YÖNTEM 3: DOM Elemanları Üzerinden Tarama
    candidate_elements = []
    selectors = [
        "li.subvariant-item", "li.variant-item", "div.subvariant-item",
        "a.subvariant-item", "div.variant-box", "div.size-box",
        "option", "button", "a", "span", "div", "li"
    ]

    for sel in selectors:
        for elem in soup.select(sel):
            text = elem.get_text(strip=True).upper()
            val = str(elem.get("value", "")).strip().upper()
            data_val = str(elem.get("data-value", "")).strip().upper()
            data_title = str(elem.get("data-title", "")).strip().upper()

            is_match = False
            if text == TARGET_SIZE or val == TARGET_SIZE or data_val == TARGET_SIZE or data_title == TARGET_SIZE:
                is_match = True
            elif re.search(rf"\b{re.escape(TARGET_SIZE)}\b", text):
                is_match = True

            if is_match:
                candidate_elements.append(elem)

    if not candidate_elements:
        logger.warning(f"Sayfada '{TARGET_SIZE}' bedeni tespit edilemedi!")
        return

    found_in_stock = False
    for elem in candidate_elements:
        if not is_element_out_of_stock(elem):
            found_in_stock = True
            break

    if found_in_stock:
        logger.info(f"🎉 MÜJDE! {TARGET_SIZE} bedeni stokta tespit edildi! Telegram bildirimi gönderiliyor...")
        send_telegram_notification(PRODUCT_URL, TARGET_SIZE, product_title)
    else:
        logger.info(f"⏳ {TARGET_SIZE} bedeni şu anda stokta bulunmuyor (Tükenmiş / Pasif).")


if __name__ == "__main__":
    check_stock()
