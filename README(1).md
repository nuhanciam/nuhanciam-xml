# Nuhanciam XML Feed

Flux XML produits généré automatiquement depuis Shopify, déployé sur GitHub Pages.

## URL du flux

```
https://raphael-vaxelaire.github.io/nuhanciam-xml/feed.xml
```

## Plateformes compatibles

- ✅ Google Merchant Center
- ✅ Meta (Facebook / Instagram)
- ✅ TikTok
- ✅ Amazon

## Mise à jour automatique

Le flux est régénéré **tous les jours à 8h (heure de Paris)** via GitHub Actions.

## Configuration des secrets GitHub

Dans ton repo → Settings → Secrets and variables → Actions :

| Secret | Valeur |
|--------|--------|
| `SHOPIFY_ACCESS_TOKEN` | Ton token Shopify |
| `SHOPIFY_SHOP_DOMAIN` | `nuhanciam.myshopify.com` |

## Lancer manuellement

Dans l'onglet **Actions** → **Générer et déployer le flux XML** → **Run workflow**
