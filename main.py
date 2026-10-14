import math
import os
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

try:
    from zoneinfo import ZoneInfo
    PL_TZ = ZoneInfo("Europe/Warsaw")
except Exception:
    PL_TZ = timezone(timedelta(hours=2))

app = FastAPI(
    title="Bugajska Bypass",
    description="System optymalizacji trasy Częstochowa - Olsztyn k. Częstochowy przez ul. Bugajską (DK46 / LK1) lub Srocko",
    version="2.0.0"
)

# ==============================================================================
# STAŁE GEOGRAFICZNE, CZASOWE I PARAMETRY ALGORYTMU
# ==============================================================================
CROSSING_LAT = 50.7788                 # Szerokość geogr. przejazdu kat. A na ul. Bugajskiej
CROSSING_LON = 19.1672                 # Długość geogr. przejazdu kat. A na ul. Bugajskiej
URBAN_SPEED_KMH = 45.0                 # Średnia prędkość miejska dla obliczeń GPS [km/h]

DEFAULT_TIME_TO_CROSSING_MIN = 7.0     # Domyślny czas dojazdu z Ronda Mickiewicza [min]
POST_CROSSING_TO_OLSZTYN_MIN = 5.0     # Czas przejazdu od przejazdu na Bugajskiej do Rynku w Olsztynie [min]
BYPASS_DETOUR_OVERHEAD_MIN = 8.0       # Stała różnica czasowa objazdu przez Srocko względem drożnej Trasy A [min]

ROUTE_A_BASE_DISTANCE_KM = 12.0        # Dystans bazowy Trasy A (z Ronda Mickiewicza) [km]
ROUTE_B_BASE_DISTANCE_KM = 17.0        # Dystans bazowy Trasy B (z Ronda Mickiewicza) [km]

GATE_CLOSE_BEFORE_MIN = 3.5            # Zamknięcie rogatek przed pociągiem [min]
GATE_OPEN_AFTER_MIN = 1.0              # Otwarcie rogatek po minięciu przejazdu [min]
QUEUE_BUFFER_AFTER_OPEN_MIN = 4.0      # Bufor rozładowania zatoru po otwarciu rogatek [min]
TOTAL_AFTER_TRAIN_MIN = GATE_OPEN_AFTER_MIN + QUEUE_BUFFER_AFTER_OPEN_MIN  # +5.0 min

CROWD_BLOCKED_TTL_MIN = 8.0            # Czas życia zgłoszenia BLOCKED [min]
CROWD_OPEN_WINDOW_MIN = 3.0            # Okno czasowe na zebranie konsensusu OPEN [min]
CROWD_OPEN_REQUIRED_COUNT = 2          # Liczba zgłoszeń OPEN wymagana do anulowania blokady

ORIGIN_PRESETS: Dict[str, Dict[str, Any]] = {
    "mickiewicza": {
        "id": "mickiewicza",
        "name": "Rondo Mickiewicza",
        "travel_to_crossing_min": 7.0,
        "distance_to_crossing_km": 5.2,
        "lat": 50.8042,
        "lon": 19.1189
    },
    "rakow": {
        "id": "rakow",
        "name": "Częstochowa Raków / Aleja Pokoju",
        "travel_to_crossing_min": 5.0,
        "distance_to_crossing_km": 3.8,
        "lat": 50.7795,
        "lon": 19.1465
    },
    "centrum": {
        "id": "centrum",
        "name": "Centrum / Dworzec Główny",
        "travel_to_crossing_min": 10.0,
        "distance_to_crossing_km": 7.5,
        "lat": 50.8118,
        "lon": 19.1205
    }
}

# ==============================================================================
# STAN SYSTEMU (CROWDSOURCING I KONSENSUS)
# ==============================================================================
state_lock = Lock()

class SystemState:
    def __init__(self):
        self.crowd_blocked_until: Optional[datetime] = None
        self.crowd_reported_at: Optional[datetime] = None
        self.open_reports: List[datetime] = []
        self.simulated_train_at: Optional[datetime] = None
        self.event_log: List[Dict[str, str]] = []

    def add_log(self, now: datetime, action: str, details: str):
        self.event_log.insert(0, {
            "time": now.strftime("%H:%M:%S"),
            "action": action,
            "details": details
        })
        self.event_log = self.event_log[:12]

state = SystemState()

def get_now() -> datetime:
    return datetime.now(PL_TZ)

# ==============================================================================
# MODUŁ GEOLOKALIZACJI I OBLICZEŃ DYSTANSU (HAVERSINE)
# ==============================================================================
def calculate_haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Oblicza odległość w linii prostej pomiędzy dwoma punktami geograficznymi (w kilometrach).
    """
    earth_radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius_km * c

def resolve_location_parameters(
    origin_id: str = "mickiewicza",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    custom_travel_min: Optional[float] = None
) -> Dict[str, Any]:
    """
    Wyznacza czas dojazdu do przejazdu kolejowego (T_dojazd) oraz dystans
    na podstawie wybranego punktu startowego lub współrzędnych GPS użytkownika.
    """
    origin_key = (origin_id or "mickiewicza").strip().lower()

    if origin_key == "gps" and lat is not None and lon is not None:
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            dist_km = calculate_haversine_km(lat, lon, CROSSING_LAT, CROSSING_LON)
            raw_minutes = (dist_km / URBAN_SPEED_KMH) * 60.0
            travel_min = max(0.5, min(60.0, round(raw_minutes, 1)))
            return {
                "origin_id": "gps",
                "origin_name": "Własna lokalizacja z GPS",
                "mode": "AUTO_GPS",
                "travel_to_crossing_min": travel_min,
                "distance_to_crossing_km": round(dist_km, 2),
                "speed_assumption_kmh": URBAN_SPEED_KMH,
                "user_lat": round(lat, 5),
                "user_lon": round(lon, 5)
            }

    if custom_travel_min is not None and 0.5 <= custom_travel_min <= 60.0:
        est_dist = (custom_travel_min / 60.0) * URBAN_SPEED_KMH
        return {
            "origin_id": origin_key,
            "origin_name": "Lokalizacja niestandardowa",
            "mode": "CUSTOM",
            "travel_to_crossing_min": round(custom_travel_min, 1),
            "distance_to_crossing_km": round(est_dist, 2),
            "speed_assumption_kmh": URBAN_SPEED_KMH,
            "user_lat": lat,
            "user_lon": lon
        }

    preset = ORIGIN_PRESETS.get(origin_key, ORIGIN_PRESETS["mickiewicza"])
    return {
        "origin_id": preset["id"],
        "origin_name": preset["name"],
        "mode": "PRESET",
        "travel_to_crossing_min": preset["travel_to_crossing_min"],
        "distance_to_crossing_km": preset["distance_to_crossing_km"],
        "speed_assumption_kmh": URBAN_SPEED_KMH,
        "user_lat": preset["lat"],
        "user_lon": preset["lon"]
    }

# ==============================================================================
# DETERMINISTYCZNY GENERATOR ROZKŁADU JAZDY (SZLAK CZĘSTOCHOWA TOW. - KORWINÓW)
# ==============================================================================
TRAIN_CATALOG = [
    {"number": "KŚ 40612", "name": "Koleje Śląskie S1", "dir": "N->S", "rel": "Częstochowa -> Gliwice"},
    {"number": "IC 14002", "name": "IC Korfanty", "dir": "N->S", "rel": "Warszawa Wsch. -> Katowice"},
    {"number": "KŚ 40655", "name": "Koleje Śląskie S1", "dir": "S->N", "rel": "Gliwice -> Częstochowa"},
    {"number": "IC 41003", "name": "IC Skarbek", "dir": "S->N", "rel": "Katowice -> Warszawa Wsch."},
    {"number": "PR 43310", "name": "Polregio", "dir": "N->S", "rel": "Częstochowa -> Zawiercie"},
    {"number": "IC 31104", "name": "IC Mickiewicz", "dir": "S->N", "rel": "Kraków Gł. -> Warszawa Wsch."},
    {"number": "KŚ 40618", "name": "Koleje Śląskie S1", "dir": "N->S", "rel": "Częstochowa -> Katowice"},
    {"number": "IC 13106", "name": "IC Szyndzielnia", "dir": "N->S", "rel": "Białystok -> Bielsko-Biała"},
    {"number": "KŚ 40661", "name": "Koleje Śląskie S1", "dir": "S->N", "rel": "Katowice -> Częstochowa"},
    {"number": "IC 45102", "name": "IC Doker", "dir": "S->N", "rel": "Katowice -> Gdynia Gł."},
]

INTERVAL_CYCLE_MINUTES = [24, 32, 21, 36, 29, 38]

def generate_trains_around(reference_time: datetime) -> List[Dict[str, Any]]:
    """
    Generuje deterministyczny rozkład pociągów wokół czasu bieżącego serwera.
    Okno niedostępności dla pojedynczego pociągu wynosi od (T_pociag - 3.5 min) do (T_pociag + 5.0 min).
    """
    day_anchor = reference_time.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
    day_seed = reference_time.timetuple().tm_yday
    current_cursor = day_anchor + timedelta(minutes=(day_seed * 5) % 15)

    end_horizon = reference_time + timedelta(hours=4)
    start_horizon = reference_time - timedelta(minutes=30)

    trains = []
    idx = 0

    while current_cursor <= end_horizon:
        if current_cursor >= start_horizon:
            meta = TRAIN_CATALOG[idx % len(TRAIN_CATALOG)]
            t_train = current_cursor
            gate_close = t_train - timedelta(minutes=GATE_CLOSE_BEFORE_MIN)
            gate_open = t_train + timedelta(minutes=GATE_OPEN_AFTER_MIN)
            block_end = t_train + timedelta(minutes=TOTAL_AFTER_TRAIN_MIN)

            trains.append({
                "id": f"LK1-{idx:03d}",
                "number": meta["number"],
                "name": meta["name"],
                "direction": meta["dir"],
                "relation": meta["rel"],
                "t_train": t_train,
                "gate_close": gate_close,
                "gate_open": gate_open,
                "block_end": block_end,
                "is_simulated": False
            })

            if idx % 4 == 1:
                opp_meta = TRAIN_CATALOG[(idx + 2) % len(TRAIN_CATALOG)]
                t_opp = t_train + timedelta(minutes=4)
                trains.append({
                    "id": f"LK1-{idx:03d}B",
                    "number": opp_meta["number"],
                    "name": opp_meta["name"],
                    "direction": "S->N" if meta["dir"] == "N->S" else "N->S",
                    "relation": opp_meta["rel"],
                    "t_train": t_opp,
                    "gate_close": t_opp - timedelta(minutes=GATE_CLOSE_BEFORE_MIN),
                    "gate_open": t_opp + timedelta(minutes=GATE_OPEN_AFTER_MIN),
                    "block_end": t_opp + timedelta(minutes=TOTAL_AFTER_TRAIN_MIN),
                    "is_simulated": False
                })

        step = INTERVAL_CYCLE_MINUTES[idx % len(INTERVAL_CYCLE_MINUTES)]
        current_cursor += timedelta(minutes=step)
        idx += 1

    if state.simulated_train_at is not None:
        if state.simulated_train_at + timedelta(minutes=TOTAL_AFTER_TRAIN_MIN) >= reference_time:
            t_sim = state.simulated_train_at
            trains.append({
                "id": "LK1-SIM1",
                "number": "IC 19100",
                "name": "IC Ekspres (Test kolizji)",
                "direction": "N->S",
                "relation": "Warszawa -> Katowice",
                "t_train": t_sim,
                "gate_close": t_sim - timedelta(minutes=GATE_CLOSE_BEFORE_MIN),
                "gate_open": t_sim + timedelta(minutes=GATE_OPEN_AFTER_MIN),
                "block_end": t_sim + timedelta(minutes=TOTAL_AFTER_TRAIN_MIN),
                "is_simulated": True
            })
            t_sim2 = t_sim + timedelta(minutes=4.5)
            trains.append({
                "id": "LK1-SIM2",
                "number": "KŚ 40699",
                "name": "KŚ Sprinter (Mijanka)",
                "direction": "S->N",
                "relation": "Gliwice -> Częstochowa",
                "t_train": t_sim2,
                "gate_close": t_sim2 - timedelta(minutes=GATE_CLOSE_BEFORE_MIN),
                "gate_open": t_sim2 + timedelta(minutes=GATE_OPEN_AFTER_MIN),
                "block_end": t_sim2 + timedelta(minutes=TOTAL_AFTER_TRAIN_MIN),
                "is_simulated": True
            })
        else:
            state.simulated_train_at = None

    trains.sort(key=lambda x: x["t_train"])
    return trains

def merge_blocking_windows(trains: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not trains:
        return []

    sorted_trains = sorted(trains, key=lambda x: x["gate_close"])
    merged = []

    for tr in sorted_trains:
        if not merged:
            merged.append({
                "start": tr["gate_close"],
                "end": tr["block_end"],
                "trains": [tr]
            })
        else:
            last = merged[-1]
            if tr["gate_close"] <= last["end"]:
                if tr["block_end"] > last["end"]:
                    last["end"] = tr["block_end"]
                last["trains"].append(tr)
            else:
                merged.append({
                    "start": tr["gate_close"],
                    "end": tr["block_end"],
                    "trains": [tr]
                })
    return merged

# ==============================================================================
# SILNIK PREDYKCJI I KALKULACJI TRAS
# ==============================================================================
def evaluate_system_state(
    now: datetime,
    origin_id: str = "mickiewicza",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    custom_travel_min: Optional[float] = None
) -> Dict[str, Any]:
    location_info = resolve_location_parameters(origin_id, lat, lon, custom_travel_min)
    travel_to_crossing_min = location_info["travel_to_crossing_min"]

    with state_lock:
        # 1. Weryfikacja ważności blokady crowdsourcingowej (TTL = 8 min) i okna konsensusu OPEN (3 min)
        if state.crowd_blocked_until and now >= state.crowd_blocked_until:
            state.add_log(now, "TTL_EXPIRED", "Wygasła 8-minutowa blokada zgłoszona przez kierowców.")
            state.crowd_blocked_until = None
            state.crowd_reported_at = None
            state.open_reports.clear()

        cutoff_open = now - timedelta(minutes=CROWD_OPEN_WINDOW_MIN)
        state.open_reports = [t for t in state.open_reports if t >= cutoff_open]

        crowd_active = state.crowd_blocked_until is not None and state.crowd_blocked_until > now
        crowd_ttl_remaining_sec = (
            max(0.0, (state.crowd_blocked_until - now).total_seconds())
            if crowd_active else 0.0
        )
        open_votes_count = len(state.open_reports)

        # 2. Wyznaczenie dynamicznego T_target = T_now + Czas_Dojazdu_Do_Torow
        t_target = now + timedelta(minutes=travel_to_crossing_min)

        # 3. Analiza rozkładu jazdy w punkcie T_target oraz T_now
        trains = generate_trains_around(now)
        windows = merge_blocking_windows(trains)

        timetable_window_at_target = None
        for w in windows:
            if w["start"] <= t_target < w["end"]:
                timetable_window_at_target = w
                break

        timetable_window_now = None
        for w in windows:
            if w["start"] <= now < w["end"]:
                timetable_window_now = w
                break

        # 4. Hierarchia źródeł blokady (rozkład ma nadrzędność nad OPEN, BLOCKED nadpisuje wolny rozkład)
        block_end_timetable: Optional[datetime] = (
            timetable_window_at_target["end"] if timetable_window_at_target else None
        )

        block_end_crowd: Optional[datetime] = None
        if crowd_active and state.crowd_blocked_until:
            crowd_delay_min = max(
                (crowd_ttl_remaining_sec / 60.0) + QUEUE_BUFFER_AFTER_OPEN_MIN,
                GATE_CLOSE_BEFORE_MIN + TOTAL_AFTER_TRAIN_MIN
            )
            block_end_crowd = t_target + timedelta(minutes=crowd_delay_min)

        effective_block_end: Optional[datetime] = None
        blocking_source = "CLEAR"

        if block_end_timetable and block_end_crowd:
            effective_block_end = max(block_end_timetable, block_end_crowd)
            blocking_source = "TIMETABLE_AND_CROWD"
        elif block_end_timetable:
            effective_block_end = block_end_timetable
            blocking_source = "TIMETABLE"
        elif block_end_crowd:
            effective_block_end = block_end_crowd
            blocking_source = "CROWDSOURCING"

        # 5. Kalkulacja czasów przejazdu
        # Czas bazowy Trasy A to czas dojazdu do przejazdu + 5 min dalszej jazdy DK46 do Olsztyna (dla Ronda Mickiewicza: 7 + 5 = 12 min).
        # Czas Trasy B (Srocko) uwzględnia stały narzut objazdu +8 min (dla Ronda Mickiewicza: 12 + 8 = 20 min).
        route_a_base_min = round(travel_to_crossing_min + POST_CROSSING_TO_OLSZTYN_MIN, 1)
        route_b_total_min = round(route_a_base_min + BYPASS_DETOUR_OVERHEAD_MIN, 1)

        if effective_block_end is None:
            wait_penalty_min = 0.0
            route_a_total_min = route_a_base_min
        else:
            wait_penalty_min = max(0.0, (effective_block_end - t_target).total_seconds() / 60.0)
            route_a_total_min = route_a_base_min + wait_penalty_min

        # 6. Kryterium decyzji
        if route_a_total_min <= route_b_total_min:
            recommended_route = "A"
            recommended_headline = "JEDŹ PRZEZ BUGAJSKĄ"
            recommended_subtitle = "Trasa A (DK46) jest szybsza w chwili dotarcia do przejazdu"
            eta_minutes = route_a_total_min
        else:
            recommended_route = "B"
            recommended_headline = "WYBIERZ OBJAZD SROCKO"
            recommended_subtitle = "Zator na ul. Bugajskiej przekracza próg opłacalności. Jedź przez Kusięta i Brzyszów"
            eta_minutes = route_b_total_min

        delta_t_min = abs(route_b_total_min - route_a_total_min)
        eta_arrival_time = now + timedelta(minutes=eta_minutes)

        dist_a_km = round(location_info["distance_to_crossing_km"] + 6.8, 1)
        dist_b_km = round(dist_a_km + 5.0, 1)
        if location_info["origin_id"] == "mickiewicza":
            dist_a_km = ROUTE_A_BASE_DISTANCE_KM
            dist_b_km = ROUTE_B_BASE_DISTANCE_KM

        upcoming_trains = []
        for tr in trains:
            if tr["block_end"] >= now - timedelta(minutes=2) and len(upcoming_trains) < 7:
                in_target = (tr["gate_close"] <= t_target < tr["block_end"])
                in_now = (tr["gate_close"] <= now < tr["block_end"])
                minutes_to_train = (tr["t_train"] - now).total_seconds() / 60.0
                upcoming_trains.append({
                    "id": tr["id"],
                    "number": tr["number"],
                    "name": tr["name"],
                    "direction": tr["direction"],
                    "relation": tr["relation"],
                    "t_train_str": tr["t_train"].strftime("%H:%M:%S"),
                    "gate_close_str": tr["gate_close"].strftime("%H:%M:%S"),
                    "gate_open_str": tr["gate_open"].strftime("%H:%M:%S"),
                    "block_end_str": tr["block_end"].strftime("%H:%M:%S"),
                    "minutes_to_train": round(minutes_to_train, 1),
                    "blocks_t_target": in_target,
                    "blocks_now": in_now,
                    "is_simulated": tr["is_simulated"]
                })

        blocking_trains_names = []
        if timetable_window_at_target:
            blocking_trains_names = [
                f"{t['number']} ({t['direction']})" for t in timetable_window_at_target["trains"]
            ]

        return {
            "timestamps": {
                "t_now": now.strftime("%H:%M:%S"),
                "t_now_hm": now.strftime("%H:%M"),
                "t_now_iso": now.isoformat(),
                "t_target": t_target.strftime("%H:%M:%S"),
                "t_target_hm": t_target.strftime("%H:%M"),
                "travel_to_crossing_min": round(travel_to_crossing_min, 1),
                "block_end_at_target": effective_block_end.strftime("%H:%M:%S") if effective_block_end else None,
                "block_end_at_target_hm": effective_block_end.strftime("%H:%M") if effective_block_end else None,
                "eta_olsztyn_hm": eta_arrival_time.strftime("%H:%M")
            },
            "location": {
                **location_info,
                "crossing_lat": CROSSING_LAT,
                "crossing_lon": CROSSING_LON
            },
            "decision": {
                "recommended_route": recommended_route,
                "recommended_headline": recommended_headline,
                "recommended_subtitle": recommended_subtitle,
                "delta_t_min": round(delta_t_min, 1),
                "eta_total_min": round(eta_minutes, 1),
                "blocking_source": blocking_source,
                "is_target_blocked": effective_block_end is not None,
                "blocking_trains": blocking_trains_names
            },
            "routes": {
                "route_a": {
                    "id": "A",
                    "name": "Trasa A (ul. Bugajska / DK46)",
                    "via": "Przejazd kolejowy kat. A (LK1) -> DK46 -> Olsztyn",
                    "distance_km": dist_a_km,
                    "base_time_min": route_a_base_min,
                    "wait_penalty_min": round(wait_penalty_min, 1),
                    "total_time_min": round(route_a_total_min, 1)
                },
                "route_b": {
                    "id": "B",
                    "name": "Trasa B (Objazd przez Srocko)",
                    "via": "Bezkolizyjnie przez Kusięta -> Brzyszów -> Srocko -> Olsztyn",
                    "distance_km": dist_b_km,
                    "base_time_min": route_b_total_min,
                    "wait_penalty_min": 0.0,
                    "total_time_min": round(route_b_total_min, 1)
                }
            },
            "crowdsourcing": {
                "is_blocked_override": crowd_active,
                "ttl_remaining_sec": int(round(crowd_ttl_remaining_sec)),
                "ttl_total_min": CROWD_BLOCKED_TTL_MIN,
                "blocked_until_str": state.crowd_blocked_until.strftime("%H:%M:%S") if crowd_active and state.crowd_blocked_until else None,
                "open_reports_in_window": open_votes_count,
                "open_reports_required": CROWD_OPEN_REQUIRED_COUNT,
                "open_window_min": CROWD_OPEN_WINDOW_MIN,
                "timetable_overrides_open": timetable_window_at_target is not None,
                "crossing_blocked_right_now": crowd_active or (timetable_window_now is not None),
                "event_log": list(state.event_log)
            },
            "timetable": {
                "line": "LK 1 (Częstochowa Towarowa - Korwinów)",
                "simulation_active": state.simulated_train_at is not None,
                "upcoming_trains": upcoming_trains
            }
        }

# ==============================================================================
# MODELE ŻĄDAŃ I ENDPOINTY API
# ==============================================================================
class ReportRequest(BaseModel):
    status: str
    origin_id: Optional[str] = "mickiewicza"
    lat: Optional[float] = None
    lon: Optional[float] = None
    travel_min: Optional[float] = None

class SimulateRequest(BaseModel):
    origin_id: Optional[str] = "mickiewicza"
    lat: Optional[float] = None
    lon: Optional[float] = None
    travel_min: Optional[float] = None

@app.get("/api/status")
def api_get_status(
    origin_id: str = Query(default="mickiewicza"),
    lat: Optional[float] = Query(default=None),
    lon: Optional[float] = Query(default=None),
    travel_min: Optional[float] = Query(default=None)
):
    now = get_now()
    return evaluate_system_state(
        now=now,
        origin_id=origin_id,
        lat=lat,
        lon=lon,
        custom_travel_min=travel_min
    )

@app.post("/api/report")
def api_post_report(payload: ReportRequest):
    now = get_now()
    report_type = payload.status.strip().upper()

    if report_type not in ("BLOCKED", "OPEN"):
        raise HTTPException(status_code=400, detail="Dozwolone wartości statusu to 'BLOCKED' lub 'OPEN'.")

    with state_lock:
        if report_type == "BLOCKED":
            state.crowd_reported_at = now
            state.crowd_blocked_until = now + timedelta(minutes=CROWD_BLOCKED_TTL_MIN)
            state.open_reports.clear()
            state.add_log(
                now,
                "REPORT_BLOCKED",
                f"Zgłoszono zator na przejeździe. Blokada aktywna na {int(CROWD_BLOCKED_TTL_MIN)} min (do {state.crowd_blocked_until.strftime('%H:%M:%S')})."
            )
            message = "Zarejestrowano zgłoszenie zatoru. Blokada przejazdu jest aktywna przez 8 minut."

        elif report_type == "OPEN":
            cutoff = now - timedelta(minutes=CROWD_OPEN_WINDOW_MIN)
            state.open_reports = [t for t in state.open_reports if t >= cutoff]
            state.open_reports.append(now)
            count = len(state.open_reports)

            if state.crowd_blocked_until and state.crowd_blocked_until > now:
                if count >= CROWD_OPEN_REQUIRED_COUNT:
                    state.crowd_blocked_until = None
                    state.crowd_reported_at = None
                    state.open_reports.clear()
                    state.add_log(
                        now,
                        "CONSENSUS_OPEN",
                        "Osiągnięto konsensus (2 zgłoszenia OPEN w oknie 3 min). Blokada społecznościowa została zniesiona."
                    )
                    message = "Osiągnięto konsensus (2/2 zgłoszeń). Blokada społecznościowa została anulowana."
                else:
                    remaining_needed = CROWD_OPEN_REQUIRED_COUNT - count
                    state.add_log(
                        now,
                        "REPORT_OPEN_PARTIAL",
                        f"Zgłoszenie przejazdu wolnego ({count}/{CROWD_OPEN_REQUIRED_COUNT} w oknie 3 min). Wymagane jeszcze {remaining_needed} potwierdzenie."
                    )
                    message = f"Zarejestrowano zgłoszenie ({count}/{CROWD_OPEN_REQUIRED_COUNT}). Do anulowania blokady potrzebne jest jeszcze {remaining_needed} potwierdzenie w ciągu 3 min."
            else:
                state.add_log(
                    now,
                    "REPORT_OPEN_INFO",
                    "Zgłoszono przejazd wolny (brak aktywnej blokady społecznościowej, obowiązuje rozkład jazdy LK1)."
                )
                message = "Zarejestrowano zgłoszenie wolnego przejazdu. System weryfikuje nadrzędny rozkład jazdy pociągów."

    updated_state = evaluate_system_state(
        now=now,
        origin_id=payload.origin_id or "mickiewicza",
        lat=payload.lat,
        lon=payload.lon,
        custom_travel_min=payload.travel_min
    )
    return {
        "ok": True,
        "message": message,
        "state": updated_state
    }

@app.post("/api/simulate-train")
def api_simulate_train(payload: Optional[SimulateRequest] = None):
    now = get_now()
    origin_id = payload.origin_id if payload and payload.origin_id else "mickiewicza"
    lat = payload.lat if payload else None
    lon = payload.lon if payload else None
    travel_min = payload.travel_min if payload else None

    loc = resolve_location_parameters(origin_id, lat, lon, travel_min)
    current_travel_min = loc["travel_to_crossing_min"]

    with state_lock:
        if state.simulated_train_at is None:
            state.simulated_train_at = now + timedelta(minutes=current_travel_min + 0.5)
            state.add_log(
                now,
                "SIM_TRAIN_ON",
                f"Włączono testowy pociąg w oknie T_target ({state.simulated_train_at.strftime('%H:%M:%S')})."
            )
        else:
            state.simulated_train_at = None
            state.add_log(now, "SIM_TRAIN_OFF", "Wyłączono testowy pociąg. Przywrócono standardowy rozkład LK1.")

    return {
        "ok": True,
        "state": evaluate_system_state(now, origin_id, lat, lon, travel_min)
    }

@app.get("/healthz")
def healthcheck():
    return {"status": "ok", "service": "bugajska-bypass", "version": "2.0.0"}

# ==============================================================================
# WSPARCIE PWA (MANIFEST.JSON, SERVICE WORKER, IKONA SVG)
# ==============================================================================
@app.get("/manifest.json")
def get_manifest():
    manifest_data = {
        "name": "Bugajska Bypass",
        "short_name": "BugajskaBypass",
        "description": "Asystent wyboru trasy Częstochowa - Olsztyn przez ul. Bugajską (DK46) lub Srocko.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#09090b",
        "theme_color": "#09090b",
        "orientation": "portrait-primary",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "192x192 512x512",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ]
    }
    return JSONResponse(content=manifest_data, media_type="application/manifest+json")

@app.get("/icon.svg")
def get_icon_svg():
    svg_content = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
      <rect width="512" height="512" rx="96" fill="#09090b"/>
      <rect x="40" y="40" width="432" height="432" rx="64" fill="#18181b" stroke="#27272a" stroke-width="12"/>
      <path d="M136 368 L256 128 L376 368" fill="none" stroke="#10b981" stroke-width="36" stroke-linecap="round" stroke-linejoin="round"/>
      <line x1="104" y1="264" x2="408" y2="264" stroke="#f59e0b" stroke-width="24" stroke-dasharray="36,24"/>
      <circle cx="256" cy="128" r="20" fill="#fafafa"/>
    </svg>"""
    return Response(content=svg_content, media_type="image/svg+xml")

@app.get("/sw.js")
def get_service_worker():
    sw_code = """
    self.addEventListener('install', (event) => {
        self.skipWaiting();
    });
    self.addEventListener('activate', (event) => {
        event.waitUntil(self.clients.claim());
    });
    self.addEventListener('fetch', (event) => {
        event.respondWith(fetch(event.request).catch(() => new Response('Brak polaczenia z siecia komorkowa.')));
    });
    """
    return Response(content=sw_code, media_type="application/javascript")

# ==============================================================================
# INTERFEJS UŻYTKOWNIKA (TRYB OPERATE - STONOWANY DESIGN ZORIENTOWANY NA ZADANIE)
# ==============================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pl" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#09090b">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="BugajskaBypass">
    <meta name="mobile-web-app-capable" content="yes">
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" type="image/svg+xml" href="/icon.svg">
    <link rel="apple-touch-icon" href="/icon.svg">
    <title>Bugajska Bypass | Nawigacja Przejazdowa DK46</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {
            background-color: #09090b;
            color: #fafafa;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
            touch-action: manipulation;
            -webkit-font-smoothing: antialiased;
        }
        .tnum {
            font-variant-numeric: tabular-nums;
        }
        details > summary {
            list-style: none;
        }
        details > summary::-webkit-details-marker {
            display: none;
        }
    </style>
</head>
<body class="min-h-screen flex flex-col justify-between bg-zinc-950 text-zinc-100 selection:bg-zinc-700 selection:text-white">

    <!-- DYSKRETNY NAGŁÓWEK SYSTEMOWY -->
    <header class="border-b border-zinc-800/80 bg-zinc-950 sticky top-0 z-20">
        <div class="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between gap-3">
            <div class="flex items-center gap-2.5">
                <span class="w-2.5 h-2.5 rounded-full bg-emerald-500" id="status-dot"></span>
                <div>
                    <h1 class="text-sm font-semibold tracking-tight text-zinc-100 leading-none">Bugajska Bypass</h1>
                    <p class="text-[11px] text-zinc-400 mt-0.5">Częstochowa -> Olsztyn (DK46 / LK1)</p>
                </div>
            </div>

            <div class="flex items-center gap-3 text-xs text-zinc-400 tnum">
                <span id="sync-label">Synchronizacja: --:--:--</span>
                <button onclick="manualRefresh()" class="px-2.5 py-1 rounded-md bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border border-zinc-800 transition text-[11px] font-medium">
                    Odśwież
                </button>
            </div>
        </div>
    </header>

    <!-- GŁÓWNY KONTENER ZADANIOWY -->
    <main class="max-w-3xl w-full mx-auto px-4 py-5 flex-1 flex flex-col gap-5">

        <!-- ================================================================ -->
        <!-- POZIOM 1: GŁÓWNY MODUŁ DECYZJI (WIDOCZNY NA PIERWSZY RZUT OKA)   -->
        <!-- ================================================================ -->
        <section id="decision-panel" class="rounded-xl border border-zinc-800 bg-zinc-900/90 p-5 sm:p-6 transition-colors">
            <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
                <span id="decision-badge" class="inline-flex items-center px-2.5 py-0.5 rounded text-xs font-semibold bg-zinc-800 text-zinc-200">
                    Trwa wyznaczanie trasy...
                </span>
                <span id="decision-source" class="text-xs text-zinc-400 tnum">
                    Dotarcie do torów: --:--
                </span>
            </div>

            <div class="my-2">
                <p class="text-xs uppercase tracking-wider text-zinc-400 font-medium">Rekomendowana trasa</p>
                <h2 id="decision-headline" class="text-3xl sm:text-4xl font-bold tracking-tight text-white mt-1">
                    ŁADOWANIE DANYCH
                </h2>
                <p id="decision-subtitle" class="text-sm text-zinc-300 mt-1.5">
                    Analiza rozkładu jazdy linii nr 1 oraz zgłoszeń kierowców.
                </p>
            </div>

            <!-- Kluczowe metryki decyzji -->
            <div class="mt-5 pt-4 border-t border-zinc-800/90 grid grid-cols-3 gap-3 tnum">
                <div>
                    <span class="text-[11px] text-zinc-400 block">Zysk czasowy</span>
                    <span id="metric-delta" class="text-xl sm:text-2xl font-bold text-white mt-0.5 block">-- min</span>
                </div>
                <div>
                    <span class="text-[11px] text-zinc-400 block">Na torach (T_target)</span>
                    <span id="metric-target-time" class="text-xl sm:text-2xl font-bold text-white mt-0.5 block">--:--</span>
                    <span id="metric-target-offset" class="text-[11px] text-zinc-400 block">za -- min</span>
                </div>
                <div>
                    <span class="text-[11px] text-zinc-400 block">Czas do Olsztyna</span>
                    <span id="metric-eta-total" class="text-xl sm:text-2xl font-bold text-white mt-0.5 block">-- min</span>
                    <span id="metric-eta-clock" class="text-[11px] text-zinc-400 block">przyjazd --:--</span>
                </div>
            </div>

            <!-- Bezpośrednie porównanie obu wariantów -->
            <div class="mt-4 pt-4 border-t border-zinc-800/90 grid grid-cols-1 sm:grid-cols-2 gap-3 tnum">
                <div id="box-route-a" class="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3 flex items-center justify-between">
                    <div>
                        <span class="text-xs font-semibold text-zinc-200 block">Trasa A: ul. Bugajska</span>
                        <span id="desc-route-a" class="text-[11px] text-zinc-400 block mt-0.5">Bazowo 12.0 min | Zator +0.0 min</span>
                    </div>
                    <span id="total-route-a" class="text-lg font-bold text-white">-- min</span>
                </div>

                <div id="box-route-b" class="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3 flex items-center justify-between">
                    <div>
                        <span class="text-xs font-semibold text-zinc-200 block">Trasa B: Objazd Srocko</span>
                        <span id="desc-route-b" class="text-[11px] text-zinc-400 block mt-0.5">Bezkolizyjnie (Kusięta / Brzyszów)</span>
                    </div>
                    <span id="total-route-b" class="text-lg font-bold text-white">-- min</span>
                </div>
            </div>
        </section>

        <!-- ================================================================ -->
        <!-- POZIOM 2: LOKALIZACJA STARTOWA I RAPORTOWANIE STANU ROGATEK      -->
        <!-- ================================================================ -->
        <section class="grid grid-cols-1 md:grid-cols-12 gap-4">

            <!-- Wybór lokalizacji (automatyczny GPS i manualny) -->
            <div class="md:col-span-5 rounded-xl border border-zinc-800 bg-zinc-900/70 p-4 flex flex-col justify-between">
                <div>
                    <div class="flex items-center justify-between mb-2">
                        <label for="origin-select" class="text-xs font-semibold uppercase tracking-wider text-zinc-300">
                            Punkt początkowy
                        </label>
                        <span id="location-mode-pill" class="text-[11px] px-2 py-0.5 rounded bg-zinc-800 text-zinc-300 tnum">
                            Predefiniowany
                        </span>
                    </div>

                    <select id="origin-select" onchange="handleOriginChange()" class="w-full rounded-lg bg-zinc-950 border border-zinc-700 px-3 py-2.5 text-sm text-zinc-100 focus:outline-none focus:border-zinc-500 transition">
                        <option value="mickiewicza">Rondo Mickiewicza (7 min do torów)</option>
                        <option value="rakow">Częstochowa Raków / Al. Pokoju (5 min)</option>
                        <option value="centrum">Centrum / Dworzec Główny (10 min)</option>
                        <option value="gps">Własna lokalizacja z GPS (automatyczna)</option>
                    </select>
                </div>

                <div class="mt-3 pt-3 border-t border-zinc-800/80 text-xs text-zinc-400 space-y-1 tnum">
                    <div class="flex justify-between">
                        <span>Odległość do przejazdu:</span>
                        <span id="loc-distance" class="text-zinc-200 font-medium">5.2 km</span>
                    </div>
                    <div class="flex justify-between">
                        <span>Estymowany dojazd (45 km/h):</span>
                        <span id="loc-travel-time" class="text-zinc-200 font-medium">7.0 min</span>
                    </div>
                    <p id="gps-status-msg" class="text-[11px] text-zinc-400 pt-1 hidden"></p>
                </div>
            </div>

            <!-- Przyciski raportowania stanu rogatek -->
            <div class="md:col-span-7 rounded-xl border border-zinc-800 bg-zinc-900/70 p-4 flex flex-col justify-between">
                <div class="flex items-center justify-between mb-2.5">
                    <div>
                        <h3 class="text-xs font-semibold uppercase tracking-wider text-zinc-300">Raportowanie stanu przejazdu</h3>
                        <p class="text-[11px] text-zinc-400">Potwierdź sytuację na ul. Bugajskiej jednym dotknięciem</p>
                    </div>
                    <span id="crowd-status-pill" class="text-[11px] px-2 py-0.5 rounded bg-zinc-800 text-zinc-300 font-medium tnum">
                        Brak alertu
                    </span>
                </div>

                <div class="grid grid-cols-2 gap-3 my-1">
                    <button onclick="sendReport('BLOCKED')" class="py-3.5 px-3 rounded-lg bg-rose-950/40 hover:bg-rose-900/50 active:bg-rose-900/70 border border-rose-800/80 text-left transition flex flex-col justify-between">
                        <span class="text-sm font-bold text-rose-100 block">Stoisz w korku</span>
                        <span class="text-[11px] text-rose-300/90 mt-1 block">Wymusza blokadę (8 min)</span>
                    </button>

                    <button onclick="sendReport('OPEN')" class="py-3.5 px-3 rounded-lg bg-emerald-950/40 hover:bg-emerald-900/50 active:bg-emerald-900/70 border border-emerald-800/80 text-left transition flex flex-col justify-between">
                        <span class="text-sm font-bold text-emerald-100 block">Przejazd wolny</span>
                        <span class="text-[11px] text-emerald-300/90 mt-1 block">2 głosy w 3 min znoszą alert</span>
                    </button>
                </div>

                <div class="mt-2.5 pt-2.5 border-t border-zinc-800/80 flex items-center justify-between text-xs text-zinc-400 tnum">
                    <span>Potwierdzenia otwarcia (3 min): <strong id="consensus-counter" class="text-zinc-200">0 / 2</strong></span>
                    <span id="crowd-ttl-label" class="text-amber-400 font-medium hidden">Wygasa za: <span id="crowd-ttl-value">00:00</span></span>
                </div>
                <div id="feedback-banner" class="mt-2 text-xs text-zinc-200 bg-zinc-800/90 border border-zinc-700 rounded px-3 py-1.5 hidden"></div>
            </div>

        </section>

        <!-- ================================================================ -->
        <!-- POZIOM 3: SZCZEGÓŁY TECHNICZNE (ZWIJANY PANEL INFORMACYJNY)      -->
        <!-- ================================================================ -->
        <section class="rounded-xl border border-zinc-800 bg-zinc-900/40 overflow-hidden">
            <details id="tech-details" class="group">
                <summary class="px-4 py-3.5 cursor-pointer flex items-center justify-between hover:bg-zinc-900/80 transition select-none">
                    <div>
                        <span class="text-xs font-semibold uppercase tracking-wider text-zinc-300 block">
                            Szczegóły techniczne, schemat relacji i rozkład jazdy LK1
                        </span>
                        <span class="text-[11px] text-zinc-400">
                            Szlak Częstochowa Towarowa - Korwinów (okno blokady pociągu: -3.5 min do +5.0 min)
                        </span>
                    </div>
                    <span class="text-xs text-zinc-400 group-open:rotate-180 transition-transform duration-200">
                        &#9662;
                    </span>
                </summary>

                <div class="p-4 border-t border-zinc-800 space-y-5 bg-zinc-950/50">

                    <!-- Schemat topologiczny i narzędzie testowe -->
                    <div>
                        <div class="flex flex-wrap items-center justify-between gap-2 mb-2.5">
                            <h4 class="text-xs font-semibold uppercase tracking-wider text-zinc-400">
                                Schemat topologiczny trasy i przejazdu kat. A (50.7788, 19.1672)
                            </h4>
                            <button onclick="toggleSimulatedTrain()" id="sim-train-btn" class="text-xs px-2.5 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition">
                                Symuluj pociąg w T_target
                            </button>
                        </div>

                        <div class="rounded-lg border border-zinc-800/90 bg-zinc-950 p-3 overflow-x-auto">
                            <svg viewBox="0 0 680 185" class="w-full h-auto min-w-[480px]">
                                <!-- Trasa B (górna) -->
                                <path id="svg-path-b" d="M 80 105 Q 240 20, 410 32 T 600 105" fill="none" stroke="#52525b" stroke-width="5" stroke-linecap="round"/>
                                <text x="340" y="24" text-anchor="middle" fill="#a1a1aa" font-size="11" font-weight="600">Trasa B: Objazd przez Kusięta / Brzyszów / Srocko (bezkolizyjna)</text>

                                <!-- Trasa A (dolna) -->
                                <path id="svg-path-a" d="M 80 105 L 330 140 L 600 105" fill="none" stroke="#10b981" stroke-width="6" stroke-linecap="round"/>
                                <text id="svg-label-to-crossing" x="200" y="155" text-anchor="middle" fill="#a1a1aa" font-size="10">Dojazd: 7.0 min</text>
                                <text x="470" y="155" text-anchor="middle" fill="#a1a1aa" font-size="10">DK46 do Olsztyna: 5.0 min</text>

                                <!-- Linia kolejowa nr 1 -->
                                <line x1="330" y1="45" x2="330" y2="170" stroke="#d97706" stroke-width="3" stroke-dasharray="6,5"/>
                                <text x="338" y="60" fill="#fbbf24" font-size="10" font-weight="600">Linia kolejowa nr 1</text>

                                <!-- Punkt przejazdu -->
                                <circle id="svg-node-crossing" cx="330" cy="140" r="10" fill="#10b981" stroke="#09090b" stroke-width="2.5"/>
                                <text id="svg-text-crossing" x="330" y="122" text-anchor="middle" fill="#d4d4d8" font-size="10" font-weight="600">ul. Bugajska</text>

                                <!-- Punkt startu i mety -->
                                <circle cx="80" cy="105" r="8" fill="#e4e4e7"/>
                                <text id="svg-start-name" x="80" y="88" text-anchor="middle" fill="#fafafa" font-size="11" font-weight="600">Start</text>

                                <circle cx="600" cy="105" r="8" fill="#e4e4e7"/>
                                <text x="600" y="88" text-anchor="middle" fill="#fafafa" font-size="11" font-weight="600">Olsztyn (Rynek)</text>
                            </svg>
                        </div>
                    </div>

                    <!-- Tabela rozkładu jazdy LK1 -->
                    <div>
                        <h4 class="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-2">
                            Najbliższe pociągi na szlaku Częstochowa Towarowa - Korwinów
                        </h4>
                        <div class="overflow-x-auto rounded-lg border border-zinc-800/90">
                            <table class="w-full text-left border-collapse text-xs tnum">
                                <thead>
                                    <tr class="border-b border-zinc-800 bg-zinc-900/80 text-zinc-400 text-[11px]">
                                        <th class="py-2 px-3 font-medium">Pociąg</th>
                                        <th class="py-2 px-3 font-medium">Kierunek i relacja</th>
                                        <th class="py-2 px-3 font-medium">Przejazd (T_pociąg)</th>
                                        <th class="py-2 px-3 font-medium">Okno blokady (-3.5m / +5.0m)</th>
                                        <th class="py-2 px-3 font-medium text-right">Status dla T_target</th>
                                    </tr>
                                </thead>
                                <tbody id="trains-table-body" class="divide-y divide-zinc-800/60 bg-zinc-950">
                                    <tr>
                                        <td colspan="5" class="py-4 text-center text-zinc-500">Wczytywanie rozkładu jazdy...</td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <!-- Dziennik zdarzeń -->
                    <div>
                        <h4 class="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-2">
                            Dziennik zdarzeń i konsensusu
                        </h4>
                        <div id="event-log-list" class="rounded-lg border border-zinc-800/90 bg-zinc-950 p-3 text-xs text-zinc-400 space-y-1 max-h-32 overflow-y-auto tnum">
                            <div>Brak zarejestrowanych zdarzeń w bieżącej sesji.</div>
                        </div>
                    </div>

                </div>
            </details>
        </section>

    </main>

    <!-- STOPKA -->
    <footer class="border-t border-zinc-900 bg-zinc-950 py-3 px-4 text-center text-[11px] text-zinc-500">
        Bugajska Bypass v2.0 - Przejazd kolejowy kat. A (50.7788, 19.1672) - Automatyczna synchronizacja co 30 s
    </footer>

    <script>
        const CROSSING_LAT = 50.7788;
        const CROSSING_LON = 19.1672;
        const URBAN_SPEED_KMH = 45.0;
        const POLL_INTERVAL_MS = 30000;

        let currentOriginId = 'mickiewicza';
        let userGpsLat = null;
        let userGpsLon = null;
        let customTravelMin = null;
        let localTtlRemainingSec = 0;

        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/sw.js').catch(() => {});
            });
        }

        function calculateHaversineKm(lat1, lon1, lat2, lon2) {
            const R = 6371.0;
            const toRad = (deg) => (deg * Math.PI) / 180.0;
            const dLat = toRad(lat2 - lat1);
            const dLon = toRad(lon2 - lon1);
            const a =
                Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
                Math.sin(dLon / 2) * Math.sin(dLon / 2);
            const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
            return R * c;
        }

        function formatSecondsMMSS(totalSec) {
            const s = Math.max(0, Math.floor(totalSec));
            const m = Math.floor(s / 60);
            const rem = s % 60;
            return String(m).padStart(2, '0') + ':' + String(rem).padStart(2, '0');
        }

        function buildStatusUrl() {
            const params = new URLSearchParams();
            params.set('origin_id', currentOriginId);
            if (currentOriginId === 'gps' && userGpsLat !== null && userGpsLon !== null) {
                params.set('lat', String(userGpsLat));
                params.set('lon', String(userGpsLon));
            }
            if (customTravelMin !== null) {
                params.set('travel_min', String(customTravelMin));
            }
            return '/api/status?' + params.toString();
        }

        async function fetchStatus() {
            try {
                const res = await fetch(buildStatusUrl(), { cache: 'no-store' });
                if (!res.ok) throw new Error('Błąd sieci');
                const data = await res.json();
                renderInterface(data);
            } catch (err) {
                const dot = document.getElementById('status-dot');
                const syncLabel = document.getElementById('sync-label');
                if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-amber-500';
                if (syncLabel) syncLabel.textContent = 'Tryb offline - ponawianie co 30 s';
            }
        }

        function manualRefresh() {
            if (currentOriginId === 'gps') {
                requestGpsPosition(true);
            } else {
                fetchStatus();
            }
        }

        function handleOriginChange() {
            const select = document.getElementById('origin-select');
            currentOriginId = select.value;
            const gpsMsg = document.getElementById('gps-status-msg');

            if (currentOriginId === 'gps') {
                requestGpsPosition(true);
            } else {
                userGpsLat = null;
                userGpsLon = null;
                customTravelMin = null;
                if (gpsMsg) gpsMsg.classList.add('hidden');
                fetchStatus();
            }
        }

        function requestGpsPosition(triggerFetchAfter) {
            const gpsMsg = document.getElementById('gps-status-msg');
            const modePill = document.getElementById('location-mode-pill');

            if (!('geolocation' in navigator)) {
                if (gpsMsg) {
                    gpsMsg.textContent = 'Przeglądarka nie obsługuje geolokalizacji GPS. Użyto wartości domyślnej (7 min).';
                    gpsMsg.classList.remove('hidden');
                }
                fetchStatus();
                return;
            }

            if (gpsMsg) {
                gpsMsg.textContent = 'Pobieranie pozycji GPS użytkownika...';
                gpsMsg.classList.remove('hidden');
            }
            if (modePill) modePill.textContent = 'Pobieranie GPS...';

            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    userGpsLat = pos.coords.latitude;
                    userGpsLon = pos.coords.longitude;
                    const distKm = calculateHaversineKm(userGpsLat, userGpsLon, CROSSING_LAT, CROSSING_LON);
                    const estMin = Math.max(0.5, Math.min(60.0, (distKm / URBAN_SPEED_KMH) * 60.0));
                    customTravelMin = Number(estMin.toFixed(1));

                    if (gpsMsg) {
                        gpsMsg.textContent = `Pozycja GPS aktywna (${userGpsLat.toFixed(4)}, ${userGpsLon.toFixed(4)})`;
                        gpsMsg.classList.remove('hidden');
                    }
                    if (triggerFetchAfter) fetchStatus();
                },
                (err) => {
                    if (gpsMsg) {
                        gpsMsg.textContent = 'Brak dostępu do GPS. Wybierz punkt z listy lub zezwól na lokalizację.';
                        gpsMsg.classList.remove('hidden');
                    }
                    if (modePill) modePill.textContent = 'Brak zgody GPS';
                    if (triggerFetchAfter) fetchStatus();
                },
                {
                    enableHighAccuracy: true,
                    timeout: 8000,
                    maximumAge: 25000
                }
            );
        }

        async function sendReport(statusType) {
            const banner = document.getElementById('feedback-banner');
            try {
                const res = await fetch('/api/report', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        status: statusType,
                        origin_id: currentOriginId,
                        lat: userGpsLat,
                        lon: userGpsLon,
                        travel_min: customTravelMin
                    })
                });
                const payload = await res.json();
                if (payload.state) renderInterface(payload.state);
                if (banner && payload.message) {
                    banner.textContent = payload.message;
                    banner.classList.remove('hidden');
                    setTimeout(() => banner.classList.add('hidden'), 5000);
                }
            } catch (e) {
                if (banner) {
                    banner.textContent = 'Błąd wysyłania zgłoszenia. Sprawdź połączenie z internetem.';
                    banner.classList.remove('hidden');
                }
            }
        }

        async function toggleSimulatedTrain() {
            try {
                const res = await fetch('/api/simulate-train', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        origin_id: currentOriginId,
                        lat: userGpsLat,
                        lon: userGpsLon,
                        travel_min: customTravelMin
                    })
                });
                const payload = await res.json();
                if (payload.state) renderInterface(payload.state);
            } catch (e) {}
        }

        function renderInterface(data) {
            // Nagłówek i czas synchronizacji
            const dot = document.getElementById('status-dot');
            const syncLabel = document.getElementById('sync-label');
            if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-500';
            if (syncLabel) syncLabel.textContent = 'Synchronizacja: ' + data.timestamps.t_now;

            // Poziom 1: Moduł decyzji
            const panel = document.getElementById('decision-panel');
            const badge = document.getElementById('decision-badge');
            const recRoute = data.decision.recommended_route;
            const isBlocked = data.decision.is_target_blocked;

            document.getElementById('decision-headline').textContent = data.decision.recommended_headline;
            document.getElementById('decision-subtitle').textContent = data.decision.recommended_subtitle;
            document.getElementById('decision-source').textContent =
                'Dotarcie do torów: ' + data.timestamps.t_target_hm + ' (za ' + data.timestamps.travel_to_crossing_min.toFixed(1) + ' min)';

            if (recRoute === 'A') {
                panel.className = 'rounded-xl border-2 border-emerald-600/80 bg-zinc-900 p-5 sm:p-6 transition-colors';
                if (!isBlocked) {
                    badge.className = 'inline-flex items-center px-2.5 py-0.5 rounded text-xs font-semibold bg-emerald-950 text-emerald-300 border border-emerald-800/80';
                    badge.textContent = 'PRZEJAZD WOLNY W T_TARGET';
                } else {
                    badge.className = 'inline-flex items-center px-2.5 py-0.5 rounded text-xs font-semibold bg-amber-950 text-amber-300 border border-amber-800/80';
                    badge.textContent = 'KRÓTKIE OCZEKIWANIE DO ' + (data.timestamps.block_end_at_target_hm || '--:--');
                }
            } else {
                panel.className = 'rounded-xl border-2 border-amber-600/90 bg-zinc-900 p-5 sm:p-6 transition-colors';
                badge.className = 'inline-flex items-center px-2.5 py-0.5 rounded text-xs font-semibold bg-rose-950 text-rose-300 border border-rose-800/80';
                const src = data.decision.blocking_source;
                if (src === 'CROWDSOURCING') {
                    badge.textContent = 'ZATOR ZGŁOSZONY PRZEZ KIEROWCÓW (DO ' + (data.timestamps.block_end_at_target_hm || '--:--') + ')';
                } else if (src === 'TIMETABLE') {
                    badge.textContent = 'BLOKADA ROZKŁADOWA PKP DO ' + (data.timestamps.block_end_at_target_hm || '--:--');
                } else {
                    badge.textContent = 'ZATOR PKP + ALERT KIEROWCÓW DO ' + (data.timestamps.block_end_at_target_hm || '--:--');
                }
            }

            // Metryki liczbowe
            document.getElementById('metric-delta').textContent = data.decision.delta_t_min.toFixed(1) + ' min szybciej';
            document.getElementById('metric-target-time').textContent = data.timestamps.t_target_hm;
            document.getElementById('metric-target-offset').textContent = 'za ' + data.timestamps.travel_to_crossing_min.toFixed(1) + ' min';
            document.getElementById('metric-eta-total').textContent = data.decision.eta_total_min.toFixed(1) + ' min';
            document.getElementById('metric-eta-clock').textContent = 'przyjazd ok. ' + data.timestamps.eta_olsztyn_hm;

            // Porównanie Trasa A i Trasa B
            const rA = data.routes.route_a;
            const rB = data.routes.route_b;
            document.getElementById('total-route-a').textContent = rA.total_time_min.toFixed(1) + ' min';
            document.getElementById('desc-route-a').textContent =
                'Bazowo ' + rA.base_time_min.toFixed(1) + ' min | Zator +' + rA.wait_penalty_min.toFixed(1) + ' min';
            document.getElementById('total-route-b').textContent = rB.total_time_min.toFixed(1) + ' min';
            document.getElementById('desc-route-b').textContent =
                'Bezkolizyjnie (' + rB.distance_km.toFixed(1) + ' km przez Srocko)';

            const boxA = document.getElementById('box-route-a');
            const boxB = document.getElementById('box-route-b');
            if (recRoute === 'A') {
                boxA.className = 'rounded-lg border border-emerald-700/80 bg-emerald-950/20 p-3 flex items-center justify-between';
                boxB.className = 'rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 flex items-center justify-between opacity-75';
            } else {
                boxA.className = 'rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 flex items-center justify-between opacity-75';
                boxB.className = 'rounded-lg border border-amber-700/80 bg-amber-950/20 p-3 flex items-center justify-between';
            }

            // Poziom 2: Informacje o lokalizacji
            const loc = data.location;
            document.getElementById('loc-distance').textContent = loc.distance_to_crossing_km.toFixed(2) + ' km';
            document.getElementById('loc-travel-time').textContent = loc.travel_to_crossing_min.toFixed(1) + ' min';

            const modePill = document.getElementById('location-mode-pill');
            if (loc.mode === 'AUTO_GPS') {
                modePill.textContent = 'GPS aktywny';
                modePill.className = 'text-[11px] px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-800/60 tnum';
            } else {
                modePill.textContent = 'Punkt z listy';
                modePill.className = 'text-[11px] px-2 py-0.5 rounded bg-zinc-800 text-zinc-300 tnum';
            }

            // Poziom 2: Status crowdsourcingu
            const crowd = data.crowdsourcing;
            document.getElementById('consensus-counter').textContent =
                crowd.open_reports_in_window + ' / ' + crowd.open_reports_required;

            const crowdPill = document.getElementById('crowd-status-pill');
            const ttlLabel = document.getElementById('crowd-ttl-label');
            localTtlRemainingSec = crowd.ttl_remaining_sec;

            if (crowd.is_blocked_override) {
                crowdPill.textContent = 'Aktywna blokada kierowców';
                crowdPill.className = 'text-[11px] px-2 py-0.5 rounded bg-rose-950 text-rose-300 border border-rose-800/70 font-medium tnum';
                ttlLabel.classList.remove('hidden');
                document.getElementById('crowd-ttl-value').textContent = formatSecondsMMSS(localTtlRemainingSec);
            } else {
                crowdPill.textContent = 'Brak alertu społeczności';
                crowdPill.className = 'text-[11px] px-2 py-0.5 rounded bg-zinc-800 text-zinc-300 font-medium tnum';
                ttlLabel.classList.add('hidden');
            }

            // Poziom 3: Schemat SVG i tabela pociągów
            const svgPathA = document.getElementById('svg-path-a');
            const svgPathB = document.getElementById('svg-path-b');
            const svgNode = document.getElementById('svg-node-crossing');
            const svgLabelToCrossing = document.getElementById('svg-label-to-crossing');
            const svgStartName = document.getElementById('svg-start-name');

            if (svgLabelToCrossing) {
                svgLabelToCrossing.textContent = 'Dojazd: ' + loc.travel_to_crossing_min.toFixed(1) + ' min';
            }
            if (svgStartName) {
                svgStartName.textContent = loc.origin_name.split('/')[0].trim();
            }

            if (recRoute === 'A') {
                svgPathA.setAttribute('stroke', '#10b981');
                svgPathA.setAttribute('stroke-width', '6');
                svgPathB.setAttribute('stroke', '#52525b');
                svgPathB.setAttribute('stroke-width', '4');
            } else {
                svgPathA.setAttribute('stroke', '#f43f5e');
                svgPathA.setAttribute('stroke-width', '4');
                svgPathB.setAttribute('stroke', '#10b981');
                svgPathB.setAttribute('stroke-width', '6');
            }

            if (isBlocked) {
                svgNode.setAttribute('fill', '#f43f5e');
            } else {
                svgNode.setAttribute('fill', '#10b981');
            }

            const simBtn = document.getElementById('sim-train-btn');
            if (data.timetable.simulation_active) {
                simBtn.textContent = 'Wyłącz symulację pociągu';
                simBtn.className = 'text-xs px-2.5 py-1 rounded bg-amber-950 text-amber-300 border border-amber-800 transition';
            } else {
                simBtn.textContent = 'Symuluj pociąg w T_target';
                simBtn.className = 'text-xs px-2.5 py-1 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition';
            }

            const tbody = document.getElementById('trains-table-body');
            const trains = data.timetable.upcoming_trains || [];
            if (trains.length === 0) {
                tbody.innerHTML = '<tr><td colspan="5" class="py-4 text-center text-zinc-500">Brak pociągów w najbliższym oknie czasowym.</td></tr>';
            } else {
                tbody.innerHTML = trains.map(tr => {
                    const rowStyle = tr.blocks_t_target
                        ? 'bg-rose-950/30 text-zinc-100'
                        : (tr.blocks_now ? 'bg-amber-950/20 text-zinc-200' : 'text-zinc-300');

                    const statusCell = tr.blocks_t_target
                        ? '<span class="px-2 py-0.5 rounded bg-rose-900/80 text-rose-200 text-[10px] font-semibold">Kolizja w T_target</span>'
                        : (tr.blocks_now
                            ? '<span class="px-2 py-0.5 rounded bg-amber-900/70 text-amber-200 text-[10px] font-semibold">Teraz na przejeździe</span>'
                            : '<span class="text-zinc-400">za ' + tr.minutes_to_train.toFixed(1) + ' min</span>');

                    return `<tr class="${rowStyle}">
                        <td class="py-2 px-3">
                            <div class="font-semibold text-zinc-100">${tr.number}</div>
                            <div class="text-[11px] text-zinc-400">${tr.name}</div>
                        </td>
                        <td class="py-2 px-3">
                            <div class="font-medium text-zinc-200">${tr.direction}</div>
                            <div class="text-[11px] text-zinc-400">${tr.relation}</div>
                        </td>
                        <td class="py-2 px-3 font-medium">${tr.t_train_str}</td>
                        <td class="py-2 px-3 text-zinc-400">${tr.gate_close_str} - ${tr.block_end_str}</td>
                        <td class="py-2 px-3 text-right">${statusCell}</td>
                    </tr>`;
                }).join('');
            }

            const logContainer = document.getElementById('event-log-list');
            const logs = crowd.event_log || [];
            if (logs.length > 0) {
                logContainer.innerHTML = logs.map(item =>
                    `<div><span class="text-zinc-500">[${item.time}]</span> <strong class="text-zinc-300 font-medium">${item.action}:</strong> ${item.details}</div>`
                ).join('');
            }
        }

        // Odświeżanie w tle co 30 sekund (oszczędzanie energii i brak rozpraszania kierowcy)
        setInterval(() => {
            if (currentOriginId === 'gps') {
                requestGpsPosition(true);
            } else {
                fetchStatus();
            }
        }, POLL_INTERVAL_MS);

        // Lokalny licznik wygasania TTL dla aktywnej blokady społecznościowej
        setInterval(() => {
            if (localTtlRemainingSec > 0) {
                localTtlRemainingSec -= 1;
                const el = document.getElementById('crowd-ttl-value');
                if (el) el.textContent = formatSecondsMMSS(localTtlRemainingSec);
            }
        }, 1000);

        // Inicjalne pobranie stanu po otwarciu aplikacji
        fetchStatus();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return HTMLResponse(content=HTML_TEMPLATE)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)