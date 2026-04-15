#!/usr/bin/env python3
"""
Générateur de flux XML produits multi-plateforme pour Nuhanciam.
Récupère les produits depuis l'API Shopify Admin et génère un flux XML
compatible avec Google Merchant Center, Meta, Amazon, Pinterest et TikTok.

Usage:
    python generate_feed.py
    python generate_feed.py --output feed.xml
    python generate_feed.py --config config.json
"""

import json
import os
import sys
import argparse
import logging
import re
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html import escape as html_escape

# ---------------------------------------------------------------------------
# Configuration & Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("nuhanciam_feed")

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Charge la configuration depuis un fichier JSON."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Shopify API Client
# ---------------------------------------------------------------------------

class ShopifyClient:
    """Client léger pour l'API Admin REST de Shopify (2024-01)."""

    API_VERSION = "2024-01"

    def __init__(self, shop_domain: str, access_token: str):
        self.base_url = f"https://{shop_domain}/admin/api/{self.API_VERSION}"
        self.access_token = access_token

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}/{endpoint}.json"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"

        req = Request(url, headers={
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        })
        try:
            with urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            logger.error("Shopify API error %s: %s", e.code, e.read().decode())
            raise
        except URLError as e:
            logger.error("Network error: %s", e.reason)
            raise

    def get_products(self, limit: int = 250, published_status: str = "published") -> list:
        """Récupère tous les produits publiés avec pagination."""
        all_products = []
        params = {"limit": limit, "published_status": published_status}
        page_info = None

        while True:
            if page_info:
                params = {"limit": limit, "page_info": page_info}

            data = self._get("products", params)
            products = data.get("products", [])
            all_products.extend(products)

            if len(products) < limit:
                break

            # Pagination curseur Shopify (simplifié — en production, parser le header Link)
            last_id = products[-1]["id"]
            params = {
                "limit": limit,
                "published_status": published_status,
                "since_id": last_id,
            }

        logger.info("Récupéré %d produits depuis Shopify", len(all_products))
        return all_products

    def get_collections(self) -> list:
        """Récupère les collections (custom collections)."""
        data = self._get("custom_collections", {"limit": 250})
        return data.get("custom_collections", [])

    def get_collection_products(self, collection_id: int) -> list:
        """Récupère les produits d'une collection."""
        data = self._get(f"collections/{collection_id}/products", {"limit": 250})
        return data.get("products", [])


# ---------------------------------------------------------------------------
# Mappers : transforment les données Shopify en attributs de flux
# ---------------------------------------------------------------------------

def clean_html(html_text: str) -> str:
    """Supprime les balises HTML et retourne du texte brut."""
    if not html_text:
        return ""
    clean = re.sub(r"<[^>]+>", "", html_text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def get_availability(variant: dict) -> str:
    """Détermine la disponibilité selon l'inventaire Shopify."""
    if variant.get("inventory_management") is None:
        return "in_stock"
    qty = variant.get("inventory_quantity", 0)
    policy = variant.get("inventory_policy", "deny")
    if qty > 0:
        return "in_stock"
    elif policy == "continue":
        return "backorder"
    return "out_of_stock"


def map_google_category(product_type: str, tags: list[str]) -> str:
    """
    Mappe le type de produit Nuhanciam vers la taxonomie Google.
    Pour les cosmétiques : Health & Beauty > Skin Care
    Référence : https://support.google.com/merchants/answer/6324436
    """
    # Catégorie par défaut pour les cosmétiques / soins de la peau
    default = "Health & Beauty > Skin Care"

    type_lower = (product_type or "").lower()
    tags_lower = [t.lower().strip() for t in tags]

    # Mappings spécifiques cosmétiques Nuhanciam
    mappings = {
        "sérum": "Health & Beauty > Skin Care > Facial Skin Care > Face Serums",
        "serum": "Health & Beauty > Skin Care > Facial Skin Care > Face Serums",
        "crème": "Health & Beauty > Skin Care > Facial Skin Care > Facial Moisturizers",
        "creme": "Health & Beauty > Skin Care > Facial Skin Care > Facial Moisturizers",
        "nettoyant": "Health & Beauty > Skin Care > Facial Skin Care > Face Cleansers",
        "gommage": "Health & Beauty > Skin Care > Facial Skin Care > Face Exfoliants",
        "masque": "Health & Beauty > Skin Care > Facial Skin Care > Face Masks",
        "huile": "Health & Beauty > Skin Care > Facial Skin Care > Facial Oils",
        "contour des yeux": "Health & Beauty > Skin Care > Facial Skin Care > Eye Creams",
        "solaire": "Health & Beauty > Skin Care > Sunscreen",
        "corps": "Health & Beauty > Skin Care > Body Skin Care > Body Lotions & Creams",
        "lait": "Health & Beauty > Skin Care > Body Skin Care > Body Lotions & Creams",
        "savon": "Health & Beauty > Skin Care > Soap & Body Wash",
    }

    for keyword, category in mappings.items():
        if keyword in type_lower:
            return category
        for tag in tags_lower:
            if keyword in tag:
                return category

    return default


def build_item(product: dict, variant: dict, config: dict) -> dict:
    """
    Construit un dictionnaire d'attributs pour un item du flux.
    Couvre les champs requis et recommandés pour :
      - Google Merchant Center
      - Meta Commerce Manager
      - Amazon
      - Pinterest
      - TikTok
    """
    shop_url = config["shop_url"].rstrip("/")
    tags = [t.strip() for t in (product.get("tags") or "").split(",") if t.strip()]
    product_type = product.get("product_type", "")

    # Identifiant unique (SKU prioritaire, sinon variant ID)
    item_id = variant.get("sku") or f"shopify_{variant['id']}"

    # URL du produit
    handle = product.get("handle", "")
    link = f"{shop_url}/products/{handle}"
    if len(product.get("variants", [])) > 1:
        link += f"?variant={variant['id']}"

    # Image
    images = product.get("images", [])
    image_link = ""
    additional_images = []
    if images:
        # Chercher l'image associée à ce variant
        variant_image = next(
            (img for img in images if img["id"] == variant.get("image_id")), None
        )
        image_link = (variant_image or images[0]).get("src", "")
        additional_images = [
            img["src"] for img in images[1:10]  # Max 10 images additionnelles
            if img["id"] != variant.get("image_id")
        ]

    # Prix
    price = f"{variant.get('price', '0.00')} {config.get('currency', 'EUR')}"
    compare_at = variant.get("compare_at_price")
    sale_price = ""
    if compare_at and float(compare_at) > float(variant.get("price", 0)):
        sale_price = price
        price = f"{compare_at} {config.get('currency', 'EUR')}"

    # Titre enrichi (produit + variant si pertinent)
    title = product.get("title", "")
    variant_title = variant.get("title", "")
    if variant_title and variant_title != "Default Title":
        title = f"{title} - {variant_title}"

    # Description nettoyée
    description = clean_html(product.get("body_html", ""))
    if not description:
        description = title

    # Poids & dimensions
    weight = variant.get("weight", 0)
    weight_unit = variant.get("weight_unit", "kg")

    # Disponibilité
    availability = get_availability(variant)

    # Catégorie Google
    google_category = config.get("google_category_override") or map_google_category(
        product_type, tags
    )

    item = {
        # === Attributs obligatoires (Google) ===
        "g:id": item_id,
        "g:title": title[:150],  # Max 150 caractères
        "g:description": description[:5000],  # Max 5000 caractères
        "g:link": link,
        "g:image_link": image_link,
        "g:availability": availability,
        "g:price": price,
        "g:brand": config.get("brand", "Nuhanciam"),
        "g:condition": "new",

        # === Attributs fortement recommandés ===
        "g:google_product_category": google_category,
        "g:product_type": product_type or "Cosmétiques",
        "g:gtin": variant.get("barcode") or "",
        "g:mpn": variant.get("sku") or "",
        "g:identifier_exists": "yes" if variant.get("barcode") else "no",

        # === Prix barré / promo ===
        "g:sale_price": sale_price,

        # === Livraison & poids ===
        "g:shipping_weight": f"{weight} {weight_unit}" if weight else "",

        # === Images additionnelles ===
        "g:additional_image_link": additional_images,

        # === Attributs spécifiques cosmétiques ===
        "g:age_group": "adult",
        "g:gender": config.get("default_gender", "unisex"),

        # === Multi-plateforme ===
        # Meta (Facebook/Instagram) — utilise les mêmes champs g: + quelques extras
        "g:item_group_id": str(product["id"]),  # Regroupe les variants

        # Pinterest — compatible nativement avec le format Google
        # TikTok — compatible nativement avec le format Google
        # Amazon — champs mappés ci-dessous
        "g:custom_label_0": product.get("vendor", ""),  # Vendor / Marque
        "g:custom_label_1": product_type,  # Type de produit
        "g:custom_label_2": "on_sale" if sale_price else "regular",
        "g:custom_label_3": availability,
        "g:custom_label_4": ", ".join(tags[:5]),  # Tags principaux
    }

    # Nettoyage : supprimer les champs vides (sauf availability et identifier_exists)
    keep_if_empty = {"g:availability", "g:identifier_exists", "g:condition"}
    item = {
        k: v for k, v in item.items()
        if v or k in keep_if_empty
    }

    return item


# ---------------------------------------------------------------------------
# Génération XML
# ---------------------------------------------------------------------------

def generate_xml_feed(products: list, config: dict) -> str:
    """
    Génère le flux XML au format RSS 2.0 avec namespace Google.
    Ce format est compatible avec :
      - Google Merchant Center
      - Meta Commerce Manager (Facebook/Instagram)
      - Pinterest
      - TikTok Commerce
      - Amazon (avec adaptations mineures)
    """
    rss = Element("rss", attrib={
        "version": "2.0",
        "xmlns:g": "http://base.google.com/ns/1.0",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
    })

    channel = SubElement(rss, "channel")

    # En-tête du flux
    SubElement(channel, "title").text = config.get("feed_title", "Nuhanciam - Catalogue Produits")
    SubElement(channel, "link").text = config.get("shop_url", "https://nuhanciam.com")
    SubElement(channel, "description").text = config.get(
        "feed_description",
        "Flux produits Nuhanciam - Soins cosmétiques pour peaux foncées et métissées"
    )
    SubElement(channel, "language").text = config.get("language", "fr")
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S %z"
    )

    # Atom self link (bonne pratique)
    feed_url = config.get("feed_url", "")
    if feed_url:
        atom_link = SubElement(channel, "atom:link")
        atom_link.set("href", feed_url)
        atom_link.set("rel", "self")
        atom_link.set("type", "application/rss+xml")

    # Génération des items
    item_count = 0
    for product in products:
        variants = product.get("variants", [])
        if not variants:
            continue

        for variant in variants:
            item_data = build_item(product, variant, config)
            item_el = SubElement(channel, "item")

            for key, value in item_data.items():
                if isinstance(value, list):
                    # Images additionnelles : un élément par image
                    for v in value:
                        SubElement(item_el, key).text = str(v)
                else:
                    SubElement(item_el, key).text = str(value)

            item_count += 1

    logger.info("Flux généré : %d items", item_count)

    # Formatage XML propre
    xml_str = tostring(rss, encoding="unicode", xml_declaration=False)
    xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

    try:
        pretty = parseString(xml_str).toprettyxml(indent="  ", encoding="UTF-8")
        # Supprimer la double déclaration XML
        lines = pretty.decode("utf-8").split("\n")
        if lines and lines[0].startswith("<?xml"):
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
        return "\n".join(lines)
    except Exception:
        return xml_str


# ---------------------------------------------------------------------------
# Mode démo : génère un flux d'exemple sans appel API
# ---------------------------------------------------------------------------

def generate_demo_products() -> list:
    """Retourne des produits d'exemple pour tester le flux."""
    return [
        {
            "id": 1001,
            "title": "Sérum Éclat Vitamine C",
            "handle": "serum-eclat-vitamine-c",
            "body_html": "<p>Sérum concentré en Vitamine C pure pour raviver l'éclat des peaux foncées et métissées. Formule légère et non grasse qui pénètre rapidement.</p>",
            "product_type": "Sérum visage",
            "vendor": "Nuhanciam",
            "tags": "sérum, vitamine c, éclat, anti-taches, visage",
            "published_at": "2024-01-15T10:00:00+01:00",
            "images": [
                {"id": 1, "src": "https://nuhanciam.com/cdn/images/serum-vitc-1.jpg"},
                {"id": 2, "src": "https://nuhanciam.com/cdn/images/serum-vitc-2.jpg"},
                {"id": 3, "src": "https://nuhanciam.com/cdn/images/serum-vitc-3.jpg"},
            ],
            "variants": [
                {
                    "id": 2001,
                    "title": "30ml",
                    "sku": "NUH-SER-VITC-30",
                    "barcode": "3760123456001",
                    "price": "39.90",
                    "compare_at_price": None,
                    "weight": 0.12,
                    "weight_unit": "kg",
                    "inventory_quantity": 150,
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "image_id": 1,
                },
                {
                    "id": 2002,
                    "title": "50ml",
                    "sku": "NUH-SER-VITC-50",
                    "barcode": "3760123456002",
                    "price": "54.90",
                    "compare_at_price": "64.90",
                    "weight": 0.18,
                    "weight_unit": "kg",
                    "inventory_quantity": 75,
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "image_id": 1,
                },
            ],
        },
        {
            "id": 1002,
            "title": "Crème Hydratante Protectrice SPF30",
            "handle": "creme-hydratante-protectrice-spf30",
            "body_html": "<p>Crème de jour hydratante avec protection solaire SPF30, spécialement formulée pour les peaux foncées et métissées. Fini invisible, sans traces blanches.</p>",
            "product_type": "Crème visage",
            "vendor": "Nuhanciam",
            "tags": "crème, hydratant, SPF30, solaire, visage, protection",
            "published_at": "2024-02-01T10:00:00+01:00",
            "images": [
                {"id": 3, "src": "https://nuhanciam.com/cdn/images/creme-spf30-1.jpg"},
                {"id": 4, "src": "https://nuhanciam.com/cdn/images/creme-spf30-2.jpg"},
            ],
            "variants": [
                {
                    "id": 2003,
                    "title": "Default Title",
                    "sku": "NUH-CRE-SPF30-50",
                    "barcode": "3760123456003",
                    "price": "34.90",
                    "compare_at_price": None,
                    "weight": 0.15,
                    "weight_unit": "kg",
                    "inventory_quantity": 200,
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "image_id": 3,
                },
            ],
        },
        {
            "id": 1003,
            "title": "Lait Corps Unifiant",
            "handle": "lait-corps-unifiant",
            "body_html": "<p>Lait corporel unifiant et nourrissant qui aide à atténuer les taches pigmentaires et unifier le teint du corps. Enrichi en actifs naturels.</p>",
            "product_type": "Soin corps",
            "vendor": "Nuhanciam",
            "tags": "corps, lait, unifiant, anti-taches, hydratant",
            "published_at": "2024-03-10T10:00:00+01:00",
            "images": [
                {"id": 5, "src": "https://nuhanciam.com/cdn/images/lait-corps-1.jpg"},
            ],
            "variants": [
                {
                    "id": 2004,
                    "title": "200ml",
                    "sku": "NUH-LAI-UNI-200",
                    "barcode": "3760123456004",
                    "price": "29.90",
                    "compare_at_price": None,
                    "weight": 0.25,
                    "weight_unit": "kg",
                    "inventory_quantity": 0,
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "image_id": 5,
                },
                {
                    "id": 2005,
                    "title": "400ml",
                    "sku": "NUH-LAI-UNI-400",
                    "barcode": "3760123456005",
                    "price": "44.90",
                    "compare_at_price": "49.90",
                    "weight": 0.45,
                    "weight_unit": "kg",
                    "inventory_quantity": 30,
                    "inventory_management": "shopify",
                    "inventory_policy": "continue",
                    "image_id": 5,
                },
            ],
        },
        {
            "id": 1004,
            "title": "Gommage Doux Exfoliant",
            "handle": "gommage-doux-exfoliant",
            "body_html": "<p>Gommage doux aux grains fins pour exfolier en douceur les peaux foncées et métissées. Élimine les cellules mortes et prépare la peau aux soins.</p>",
            "product_type": "Gommage visage",
            "vendor": "Nuhanciam",
            "tags": "gommage, exfoliant, visage, doux, peau nette",
            "published_at": "2024-04-05T10:00:00+01:00",
            "images": [
                {"id": 6, "src": "https://nuhanciam.com/cdn/images/gommage-1.jpg"},
                {"id": 7, "src": "https://nuhanciam.com/cdn/images/gommage-2.jpg"},
            ],
            "variants": [
                {
                    "id": 2006,
                    "title": "Default Title",
                    "sku": "NUH-GOM-DX-75",
                    "barcode": "3760123456006",
                    "price": "24.90",
                    "compare_at_price": None,
                    "weight": 0.10,
                    "weight_unit": "kg",
                    "inventory_quantity": 95,
                    "inventory_management": "shopify",
                    "inventory_policy": "deny",
                    "image_id": 6,
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Générateur de flux XML Nuhanciam")
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH,
        help="Chemin vers le fichier de configuration JSON"
    )
    parser.add_argument(
        "--output", default=None,
        help="Chemin du fichier XML de sortie (défaut: feed.xml)"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Générer un flux de démonstration sans appel API Shopify"
    )
    args = parser.parse_args()

    # Charger la configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        logger.warning("Config non trouvée à %s, utilisation des valeurs par défaut", args.config)
        config = {
            "shop_domain": "nuhanciam.myshopify.com",
            "shop_url": "https://nuhanciam.com",
            "brand": "Nuhanciam",
            "currency": "EUR",
            "language": "fr",
            "feed_title": "Nuhanciam - Catalogue Produits",
            "feed_description": "Soins cosmétiques pour peaux foncées et métissées",
            "default_gender": "unisex",
        }

    output_path = args.output or config.get("output_file", "feed.xml")

    if args.demo:
        logger.info("Mode démo activé — utilisation des produits d'exemple")
        products = generate_demo_products()
    else:
        # Récupérer les produits depuis Shopify
        shop_domain = config.get("shop_domain") or os.environ.get("SHOPIFY_SHOP_DOMAIN")
        access_token = config.get("access_token") or os.environ.get("SHOPIFY_ACCESS_TOKEN")

        if not shop_domain or not access_token:
            logger.error(
                "shop_domain et access_token requis. "
                "Configurez-les dans config.json ou via les variables d'environnement "
                "SHOPIFY_SHOP_DOMAIN et SHOPIFY_ACCESS_TOKEN."
            )
            sys.exit(1)

        client = ShopifyClient(shop_domain, access_token)
        products = client.get_products()

    # Générer le flux XML
    xml_content = generate_xml_feed(products, config)

    # Écrire le fichier
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    logger.info("Flux XML écrit dans : %s", output_path)
    print(f"\n✓ Flux généré avec succès : {output_path}")
    print(f"  - {sum(len(p.get('variants', [])) for p in products)} items")
    print(f"  - Plateformes : Google Merchant Center, Meta, Amazon, Pinterest, TikTok")


if __name__ == "__main__":
    main()
