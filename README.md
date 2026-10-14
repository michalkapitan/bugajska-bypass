# Bugajska Bypass (v2.0)

Aplikacja webowa optymalizująca czas przejazdu w relacji Częstochowa -> Olsztyn k. Częstochowy (Rynek). System analizuje ryzyko utknięcia przed przejazdem kolejowym kat. A na ul. Bugajskiej (DK46, linia kolejowa nr 1, współrzędne `50.7788, 19.1672`) i porównuje czas przejazdu z bezkolizyjnym objazdem przez Srocko (przez Kusięta i Brzyszów).

## Kluczowe funkcje wersji 2.0

1. **Moduł lokalizacji (GPS i punkty predefiniowane):**
   - **Rondo Mickiewicza (domyślnie):** 7.0 min dojazdu do przejazdu.
   - **Częstochowa Raków / Aleja Pokoju:** 5.0 min dojazdu do przejazdu.
   - **Centrum / Dworzec Główny:** 10.0 min dojazdu do przejazdu.
   - **Własna lokalizacja z GPS:** pobiera współrzędne z przeglądarki (`navigator.geolocation`), wylicza odległość w linii prostej do przejazdu (`50.7788, 19.1672`) i szacuje czas dotarcia przy średniej prędkości miejskiej 45 km/h.
2. **Dynamiczny punkt predykcji:** `T_target = T_now + Czas_Dojazdu_Do_Torow`.
3. **Model blokady przejazdu (LK1):**
   - Zamknięcie rogatek: 3.5 min przed pociągiem.
   - Otwarcie rogatek: 1.0 min po minięciu przejazdu.
   - Bufor rozładowania kolejki: 4.0 min po otwarciu rogatek.
   - Pełne okno niedostępności dla pociągu: od `T_pociag - 3.5 min` do `T_pociag + 5.0 min`.
4. **Crowdsourcing i konsensus:**
   - Zgłoszenie `BLOCKED` ("Stoisz w korku") wymusza blokadę przejazdu na czas `TTL = 8 minut`.
   - 2 zgłoszenia `OPEN` ("Przejazd wolny") w oknie 3 minut anulują aktywną blokadę społecznościową.
   - Nadchodzący pociąg z rozkładu ma nadrzędność nad zgłoszeniem otwarcia.
5. **Interfejs w trybie Operate:**
   - Stonowana paleta kolorystyczna, czytelna hierarchia typograficzna i odświeżanie w tle co 30 sekund.

---

## Scenariusz 1: Uruchomienie lokalne na komputerze (Windows)

1. Pobierz i zainstaluj Python ze strony `https://www.python.org/downloads/` (podczas instalacji zaznacz pole **Add python.exe to PATH**).
2. Umieść pliki `main.py`, `requirements.txt` oraz `uruchom.bat` w jednym folderze.
3. Kliknij dwukrotnie plik `uruchom.bat`. Skrypt zainstaluje zależności i otworzy aplikację pod adresem `http://localhost:8000`.

---

## Scenariusz 2: Darmowe wdrożenie w chmurze Render.com (dostęp z telefonu przez LTE/5G)

Uwaga: Przeglądarki mobilne wymagają szyfrowanego połączenia HTTPS do działania modułu GPS (`navigator.geolocation`). Platforma Render.com zapewnia certyfikat HTTPS automatycznie.

1. Utwórz darmowe konto w serwisie GitHub (`https://github.com`) i stwórz nowe repozytorium publiczne o nazwie `bugajska-bypass`.
2. Prześlij do repozytorium pliki `main.py` oraz `requirements.txt`.
3. Zaloguj się w serwisie Render (`https://render.com`) za pomocą konta GitHub.
4. Kliknij **New +** -> **Web Service** i połącz swoje repozytorium `bugajska-bypass`.
5. Ustaw parametry wdrożenia:
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type:** `Free`
6. Kliknij **Create Web Service**. Po zakończeniu budowy otrzymasz stały adres `https://twoja-nazwa.onrender.com`.

---

## Instalacja na ekranie głównym telefonu (PWA)

1. Otwórz adres swojej aplikacji na Render.com w przeglądarce Google Chrome na telefonie z systemem Android.
2. Dotknij ikony menu (trzy kropki w prawym górnym rogu) i wybierz **Dodaj do ekranu głównego** (lub **Zainstaluj aplikację**).
3. Aplikacja będzie uruchamiać się w trybie pełnoekranowym bezpośrednio z ikony na pulpicie telefonu.