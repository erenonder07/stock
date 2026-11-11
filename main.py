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
PRODUCT_URL = os.environ.get(
    "PRODUCT_URL",
    "https://www.avva.com.tr/siyah-kapusonlu-su-itici-ruzgar-gecirmez-10-dereceye-dayanikli-sisme-mont-38173"
).strip()

TARGET_SIZE = os.environ.get("TARGET_SIZE", "L").strip().upper()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()

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
        logger.warning("TELEGRAM_TOKEN veya CHAT_ID tanimlanmamis! Telegram mesaji gonderilemedi.")
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


def is_element_out_of_stock(elem) -> bool:
    """Verilen HTML elemaninin veya ust kapsayicilarinin stok disi olup olmadigini kontrol eder."""
    current = elem
    for _ in range(4):  # Kendisi ve 3 ust kapsayicisini tara
        if current is None:
            break

        # 1. HTML Class kontrolu
        classes = current.get("class", [])
        if isinstance(classes, list):
            class_str = " ".join(classes).lower()
        else:
            class_str = str(classes).lower()

        for oos in OUT_OF_STOCK_CLASSES:
            if oos in class_str:
                return True

        # 2. HTML Nitelik (Attribute) kontrolu
        attrs = current.attrs
        if "disabled" in attrs:
            return True

        # data-stock="0" veya data-stok="0" kontrolu
        data_stock = attrs.get("data-stock") or attrs.get("data-stok") or attrs.get("stock")
        if data_stock is not None and str(data_stock).strip() == "0":
            return True

        # data-aktif="false" kontrolu
        data_active = attrs.get("data-aktif") or attrs.get("data-active")
        if data_active is not None and str(data_active).strip().lower() in ["false", "0"]:
            return True

        # 3. Text / Title kontrolu
        title_attr = str(attrs.get("title", "")).lower()
        for oos_text in OUT_OF_STOCK_TEXTS:
            if oos_text in title_attr:
                return True

        current = current.parent

    # Metin iceriginde acikca tukendi yaziyor mu?
    elem_text = elem.get_text(separator=" ", strip=True).lower()
    for oos_text in OUT_OF_STOCK_TEXTS:
        if oos_text in elem_text:
            return True

    return False


def check_script_variants(soup: BeautifulSoup, target_size: str):
    """
    Sayfadaki script tag'lerinde yer alabilecek Ticimax / JSON varyant verilerini analiz eder.
    Varsa (found: bool, in_stock: bool) doner, bulunamazsa None doner.
    """
    scripts = soup.find_all("script")
    for script in scripts:
        content = script.string or ""
        if not content:
            continue

        # Ticimax varyant listesi veya json degiskenleri
        if "subVariant" in content or "urunVaryant" in content or "ProductVariant" in content:
            matches = re.findall(r'(\{[^{}]*?' + re.escape(target_size) + r'[^{}]*?\})', content)
            for m in matches:
                try:
                    # Basit stok verisi yakalama
                    stock_match = re.search(r'["\']?(?:Stock|StokAdedi|stok|stock)["\']?\s*:\s*(\d+)', m, re.IGNORECASE)
                    if stock_match:
                        stock_qty = int(stock_match.group(1))
                        logger.info(f"Script icerisinde beden bulundu ({target_size}), stok adedi: {stock_qty}")
                        return True, (stock_qty > 0)
                except Exception:
                    pass

    return None


def check_stock():
    logger.info(f"Stok kontrolu baslatiliyor...")
    logger.info(f"Urun URL: {PRODUCT_URL}")
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
        logger.info(f"HTTP Yanit Kodu: {response.status_code}")
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Sayfaya erisilirken HTTP hatasi meydana geldi: {e}")
        sys.exit(1)

    soup = BeautifulSoup(response.content, "html.parser")

    # Urun basligini bulma (opsiyonel)
    product_title = ""
    title_elem = (
        soup.find("h1", class_=re.compile(r"product.*title|title", re.I))
        or soup.find("h1")
        or soup.find("title")
    )
    if title_elem:
        product_title = title_elem.get_text(strip=True)
        logger.info(f"Urun Adi: {product_title}")

    # Oncelikle script varyant verisi kontrolu
    script_result = check_script_variants(soup, TARGET_SIZE)
    if script_result is not None:
        size_found, in_stock = script_result
        if size_found and in_stock:
            logger.info(f"Tebrikler! {TARGET_SIZE} bedeni script verilerine gore STOKTA!")
            send_telegram_notification(PRODUCT_URL, TARGET_SIZE, product_title)
            return
        elif size_found and not in_stock:
            logger.info(f"{TARGET_SIZE} bedeni script verilerine gore stokta yok (Tukenmis).")
            return

    # DOM elemanlari uzerinden varyant/beden arama
    candidate_elements = []

    # Olası beden secenekleri tag ve class'lari (Ticimax ve genel e-ticaret kaliplari)
    selectors = [
        "li.subvariant-item",
        "li.variant-item",
        "div.subvariant-item",
        "a.subvariant-item",
        "div.variant-box",
        "div.size-box",
        "option",
        "button",
        "a",
        "span",
        "div",
        "li"
    ]

    for sel in selectors:
        for elem in soup.select(sel):
            text = elem.get_text(strip=True).upper()
            val = str(elem.get("value", "")).strip().upper()
            data_val = str(elem.get("data-value", "")).strip().upper()
            data_title = str(elem.get("data-title", "")).strip().upper()

            # Tam kelime veya tam eslesme kontrolu
            is_match = False
            if text == TARGET_SIZE or val == TARGET_SIZE or data_val == TARGET_SIZE or data_title == TARGET_SIZE:
                is_match = True
            elif re.search(rf"\b{re.escape(TARGET_SIZE)}\b", text):
                # Beden kelimesi iceriyorsa (ornegin "L Beden", "Beden: L")
                # Fakat "XL", "XXL" gibi bedenlerin "L" ile karismamasi icin \b kullanildi
                is_match = True

            if is_match:
                candidate_elements.append(elem)

    if not candidate_elements:
        logger.warning(
            f"Sayfada '{TARGET_SIZE}' bedeni tespit edilemedi! "
            f"Beden hic listelenmiyor veya urun sayfasi dinamik yukleniyor olabilir."
        )
        return

    logger.info(f"'{TARGET_SIZE}' bedeni icin {len(candidate_elements)} olasi DOM elemani bulundu. Stok durumlari denetleniyor...")

    # Aday elemanlarin stok durumlarini incele
    found_in_stock = False
    for elem in candidate_elements:
        out_of_stock = is_element_out_of_stock(elem)
        if not out_of_stock:
            found_in_stock = True
            logger.info(f"Eleman stokta gorunuyor! Tag: <{elem.name} class='{elem.get('class', [])}'>")
            break
        else:
            logger.info(f"Eleman stok disi (passive/disabled) olarak tespit edildi: <{elem.name} class='{elem.get('class', [])}'>")

    if found_in_stock:
        logger.info(f"🎉 MÜJDE! {TARGET_SIZE} bedeni stokta tespit edildi! Telegram bildirimi gonderiliyor...")
        send_telegram_notification(PRODUCT_URL, TARGET_SIZE, product_title)
    else:
        logger.info(f"⏳ {TARGET_SIZE} bedeni su anda stokta bulunmuyor (Tükenmiş / Pasif).")


if __name__ == "__main__":
    check_stock()
