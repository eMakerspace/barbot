# Barbot - Automated Bartender WooCommerce Integration


Anleitung schreiben!!

Neuer Drink im onlineshop:

1. Bild generieren
2. Anderen Drink duplizieren
3. Bild, Text, Titel ersetzen
4. Inhalt ersetzen (attribute)
5. Preis setzen
6. Auf "Out of Stock" setzen
7. Veröffentlichen

Neues Attribut (Spirituose, Mixer) hinzufügen:
1. Unter Attribut -> Mixer -> add term (screenshot!)
2. Properties der Flasche (Bottle size, Viscosity)
3. Unter DIY Drink: Neue Attribute hinzufügen
4. Unter DIY Drink: Neue Variationen hinzufügen
5. Unter DIY Drink: Preise setzen für alle Variationen

Wenn es mehr als 200 Variationen gibt: Snippet anpassen.


## Setup
1. File namens ```.env``` erstellen.
2. Dort folgende 4 Umgebungsvariablen setzen:
```
WOOCOMMERCE_URL=https://barbot.emakerspace.ch
WOOCOMMERCE_KEY=ck_123456789
WOOCOMMERCE_SECRET=cs_123456789
HEARTBEAT_TOKEN=123456789
```

Codes stehen im Admin-Bereich des webshops.

