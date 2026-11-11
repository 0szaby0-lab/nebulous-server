# Nebulous Egyedi Master Szerver & Kliens Módosító

Egyedi, teljes értékű privát szerver háttérrendszer (backend) a **Nebulous.io** játékhoz, **MongoDB** adatperzisztenciával (szintek, pontok, statisztikák és feloldott skinek mentése), **ingyenes bolti vásárlásokkal** és 1-kattintásos **Render.com** felhő-telepítéssel.

---

## 🚀 Főbb funkciók

1. **MongoDB Atlas Adatperzisztencia:**
   - Minden játékos szintje (XP), pontszáma, megnyert meccsei, statisztikái és feloldott tárgyai automatikusan mentésre kerülnek a MongoDB adatbázisba.
   - Helyi tesztelés esetén automatikus memóriabeli / JSON mentési tartalékfunkció.
2. **Minden Ingyenes Vásárlás ("Mindent ingyen"):**
   - Minden skin, kalap, háziállat (pet), halo és profil testreszabás ára `0` plazma.
   - A játékos alapértelmezetten 999 999 999 plazmával és VIP / Mod státusszal indul.
3. **Központi EU Master Szerver:**
   - Teljes `/api/account/*` végpont-támogatás (`CheckIn`, `GetClientConfig`, `GetPlayerProfile`, `GetPlayerStats`, `GetPurchasePrices`, `CoinPurchase`, `GetSkinIDs`, `JDKaYIIScQ`).
   - Ismeretlen végpontok automatikus hiba nélküli fallback lekezelése (a kliens sosem crashel).
4. **Automatizált Kliens Átíró (APK Patcher):**
   - 1 parancs segítségével bármilyen szerver URL-re átírható a játék APK-ja (`wa.r1`), újraépíti, aláírja és a `Download/` mappába másolja.

---

## 📂 Mappa struktúra

```text
nebulous/
├── server/
│   ├── main.py                  # FastAPI aszinkron főkiszolgáló (Render.com production)
│   ├── database.py              # Motor / Async MongoDB adatbázis réteg
│   ├── server_standalone.py     # Nulla függőségű önálló szerver (helyi tesztekhez a telefonon)
│   ├── requirements.txt         # Python csomagfüggőségek
│   ├── Dockerfile               # Docker konténer Render.com vagy VPS futtatáshoz
│   └── render.yaml              # Render.com Blueprint konfiguráció
├── patch_client.py              # Automata APK átíró, újraépítő és aláíró script
├── base.apk                     # Eredeti tiszta Nebulous APK
├── nebulous_mod.apk             # Már elkészített, aláírt és átirányított APK
└── debug.keystore               # Android fejlesztői aláíró kulcstár
```

---

## ☁️ 1. Telepítés Render.com-ra

### 1. lépés: MongoDB Atlas létrehozása (Ingyenes)
1. Regisztrálj a [mongodb.com](https://www.mongodb.com/) oldalon, és hozz létre egy ingyenes **M0 Free Cluster**-t.
2. A **Database Access** menüben hozz létre egy felhasználót és jelszót.
3. A **Network Access** menüben engedélyezd a hozzáférést bárhonnan (`0.0.0.0/0`).
4. Másold ki a csatlakozási linket (Connection String):
   `mongodb+srv://<felhasznalonev>:<jelszo>@cluster0.mongodb.net/?retryWrites=true&w=majority`

### 2. lépés: Render.com Szolgáltatás indítása
1. Töltsd fel a `server/` mappa tartalmát egy GitHub repóba.
2. Lépj be a [render.com](https://render.com) vezérlőpultjára.
3. Kattints a **New +** -> **Web Service** gombra, és válaszd ki a GitHub repódat.
4. Add meg a beállításokat:
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Az **Environment Variables** (Környezeti változók) szekcióban add hozzá:
   - `MONGODB_URI`: az 1. lépésben kapott MongoDB címed.
   - `DB_NAME`: `nebulous_db`
   - `SERVER_PUBLIC_IP`: a játék node szerver IP-je (pl. VPS IP vagy teszt IP).
6. Kattints a **Deploy Web Service** gombra.
7. Amikor kész, a Render ad egy HTTPS címet, például:
   `https://nebulous-master-server.onrender.com`

---

## 📱 2. Kliens APK átírása a Render URL-re

A telefonodon a terminálban egyszerűen futtasd a mellékelt scriptet a Renderes címeddel:

```bash
python3 patch_client.py https://nebulous-master-server.onrender.com
```

A script elvégzi a teljes folyamatot:
1. Átírja a `wa/r1.smali` hálózati URL-jét az új címedre.
2. Újraépíti az APK-t az `apktool` segítségével.
3. Aláírja az Android `apksigner` eszközzel a `debug.keystore`-ral.
4. Lemásolja az elkészült fájlt a telefonod Letöltések mappájába:
   `/storage/emulated/0/Download/nebulous_mod.apk`

---

## 💻 3. Helyi szerver futtatása a telefonon (Teszthez)

Ha a felhőbe való feltöltés előtt közvetlenül a telefonodon szeretnéd kipróbálni:

1. Indítsd el a szervert a háttérben:
   ```bash
   python3 server/server_standalone.py
   ```
2. A jelenlegi `nebulous_mod.apk` már előre be van állítva a helyi címre (`http://127.0.0.1:8000`), így azonnal tesztelhető!
