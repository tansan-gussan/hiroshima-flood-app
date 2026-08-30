# ==========================================
# 広島市役所周辺 浸水リスク分析 Webアプリ
# Streamlit / 公式データ自動取得 / 公開版
# ==========================================

import csv
import math
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import folium
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit_folium import folium_static, st_folium


# ==========================================
# 基本設定
# ==========================================

CSV_FILENAME = "hiroshima_external_flood_grid50m.csv"

# 三篠橋の水位基準（現在の試作で使用している値）
WAITING_LEVEL = 2.50
CAUTION_LEVEL = 2.70
EVACUATION_LEVEL = 2.80
DANGER_LEVEL = 3.20


st.set_page_config(
    page_title="広島市 浸水リスク分析",
    page_icon="🌧️",
    layout="wide",
)

st.markdown(
    """
    <style>
    .result-card {
        padding: 20px;
        border-radius: 16px;
        text-align: center;
        border: 1px solid rgba(128,128,128,0.3);
        margin-bottom: 10px;
    }
    .result-title {
        font-size: 16px;
        opacity: 0.8;
    }
    .result-score {
        font-size: 38px;
        font-weight: bold;
        margin: 5px 0;
    }
    .result-level {
        font-size: 18px;
        font-weight: bold;
    }
    .low { background: rgba(50, 180, 100, 0.15); }
    .medium { background: rgba(240, 190, 50, 0.18); }
    .high { background: rgba(240, 120, 40, 0.18); }
    .very-high { background: rgba(220, 60, 60, 0.20); }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==========================================
# 共通処理
# ==========================================

def csv_path():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, CSV_FILENAME)


def distance_m(lat1, lon1, lat2, lon2):
    """2地点間の距離をメートルで計算。"""
    r = 6_371_000

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(dlon / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def find_nearest_grid(lat, lon):
    """指定地点から最も近い50m格子を検索。"""
    nearest_data = None
    nearest_distance = float("inf")

    with open(csv_path(), "r", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)

        for row in reader:
            grid_lat = float(row["緯度"])
            grid_lon = float(row["経度"])
            distance = distance_m(lat, lon, grid_lat, grid_lon)

            if distance < nearest_distance:
                nearest_distance = distance
                nearest_data = row

    return nearest_data, nearest_distance


def get_nearby_grids(lat, lon, radius_m=800):
    """指定地点から半径radius_m以内の50m格子を取得。"""
    nearby = []

    with open(csv_path(), "r", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)

        for row in reader:
            grid_lat = float(row["緯度"])
            grid_lon = float(row["経度"])

            if distance_m(lat, lon, grid_lat, grid_lon) <= radius_m:
                nearby.append(row)

    return nearby


# ==========================================
# 公式データ取得
# ==========================================

@st.cache_data(ttl=300)
def get_hiroshima_rainfall():
    """
    気象庁アメダス「広島」(67437)から、
    最新の10分・1時間・24時間降水量を取得。
    """
    station_id = "67437"

    latest_url = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
    response = requests.get(latest_url, timeout=10)
    response.raise_for_status()

    latest_dt = datetime.fromisoformat(response.text.strip())
    timestamp = latest_dt.strftime("%Y%m%d%H%M%S")

    data_url = (
        "https://www.jma.go.jp/"
        f"bosai/amedas/data/map/{timestamp}.json"
    )
    response = requests.get(data_url, timeout=10)
    response.raise_for_status()
    data = response.json()

    if station_id not in data:
        raise ValueError("広島アメダスのデータが見つかりません")

    point = data[station_id]

    def get_value(name):
        value = point.get(name)
        if (
            isinstance(value, list)
            and len(value) > 0
            and value[0] is not None
        ):
            return float(value[0])
        return 0.0

    rain_10 = get_value("precipitation10m")
    rain_60 = get_value("precipitation1h")
    rain_24h = get_value("precipitation24h")

    return rain_10, rain_60, rain_24h, latest_dt


@st.cache_data(ttl=300)
def get_misasa_river_level():
    """
    広島県河川防災情報システムから
    三篠橋(国)の最新水位と観測時刻を取得。
    """
    url = (
        "https://www.kasen-bousai.pref.hiroshima.lg.jp/"
        "mobile/m4201/22333_4_1.html"
    )

    response = requests.get(url, timeout=10)
    response.raise_for_status()
    response.encoding = response.apparent_encoding

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    # 例: 10:20 (現況) 2.09 m
    water_match = re.search(
        r"(\d{1,2}:\d{2})\s*"
        r"\(現況\)\s*"
        r"([+-]?\d+(?:\.\d+)?)\s*m",
        text,
    )

    if water_match is None:
        raise ValueError("三篠橋の現在水位を取得できませんでした")

    river_time = water_match.group(1)
    river_level = float(water_match.group(2))

    # 例: 08月29日10時20分現在
    date_match = re.search(
        r"(\d{1,2})月"
        r"(\d{1,2})日"
        r"(\d{1,2})時"
        r"(\d{1,2})分現在",
        text,
    )

    if date_match:
        month = int(date_match.group(1))
        day = int(date_match.group(2))
        hour = int(date_match.group(3))
        minute = int(date_match.group(4))
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        observed_time = (
            f"{now.year}/{month:02d}/{day:02d} "
            f"{hour:02d}:{minute:02d}"
        )
    else:
        observed_time = river_time

    return river_level, observed_time


@st.cache_data(ttl=300)
def get_hiroshima_tide():
    """
    気象庁「広島」の潮位表から、
    現在時刻の毎時天文潮位（予測）と、その日の毎時予測の最小・最大を取得。
    """
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    year = now.year
    month = now.month
    day = now.day
    hour = now.hour

    url = (
        "https://www.data.jma.go.jp/"
        "kaiyou/db/tide/suisan/suisan.php"
        "?LV=DL"
        "&S_HILO=on"
        "&S_HOUR=on"
        "&stn=Q8"
        f"&ys={year}"
        f"&ms={month}"
        f"&ds={day}"
        f"&ye={year}"
        f"&me={month}"
        f"&de={day}"
    )

    response = requests.get(url, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    target_date = f"{year:04d}/{month:02d}/{day:02d}"
    hourly_values = None

    for row in soup.find_all("tr"):
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["th", "td"])
        ]

        if not cells:
            continue
        if target_date not in cells[0]:
            continue

        if len(cells) >= 25:
            values = []
            for value_text in cells[1:25]:
                try:
                    values.append(float(value_text))
                except ValueError:
                    values.append(None)

            if len(values) == 24:
                hourly_values = values
                break

    if hourly_values is None:
        raise ValueError("今日の毎時潮位を取得できませんでした")

    current_tide = hourly_values[hour]
    if current_tide is None:
        raise ValueError("現在時刻の潮位データがありません")

    valid_values = [value for value in hourly_values if value is not None]
    if not valid_values:
        raise ValueError("有効な潮位データがありません")

    tide_low = min(valid_values)
    tide_high = max(valid_values)
    observed_time = f"{year}/{month:02d}/{day:02d} {hour:02d}:00"

    return current_tide, tide_low, tide_high, observed_time


# ==========================================
# 分析モデル
# ==========================================

def elevation_vulnerability(elevation):
    """標高から地形脆弱性スコア(0-100)を計算。"""
    probability = 1 / (
        1 + math.exp(-(1.8703 - 1.0017 * elevation))
    )
    return probability * 100


def risk_level(score):
    if score < 25:
        return "低い"
    if score < 50:
        return "注意"
    if score < 75:
        return "高い"
    return "非常に高い"


def card_class(score):
    if score < 25:
        return "low"
    if score < 50:
        return "medium"
    if score < 75:
        return "high"
    return "very-high"


def show_risk_card(title, score, level):
    css_class = card_class(score)
    st.markdown(
        f"""
        <div class="result-card {css_class}">
            <div class="result-title">{title}</div>
            <div class="result-score">{score:.0f}</div>
            <div class="result-level">{level}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def terrain_color(score):
    if score < 25:
        return "green"
    if score < 50:
        return "orange"
    if score < 75:
        return "red"
    return "darkred"


def external_flood_color(rank):
    if rank == 0:
        return "gray"
    if rank == 1:
        return "green"
    if rank == 2:
        return "orange"
    if rank == 3:
        return "red"
    return "darkred"


def estimated_depth_color(depth_cm):
    if depth_cm <= 0:
        return "gray"
    if depth_cm < 10:
        return "green"
    if depth_cm < 30:
        return "orange"
    if depth_cm < 50:
        return "red"
    return "darkred"


def calculate_river_risk(river_level):
    """三篠橋の基準水位に対する表示用試作スコア。"""
    if river_level <= 0:
        score = 0
    elif river_level < WAITING_LEVEL:
        score = (river_level / WAITING_LEVEL) * 25
    elif river_level < CAUTION_LEVEL:
        score = 25 + (
            (river_level - WAITING_LEVEL)
            / (CAUTION_LEVEL - WAITING_LEVEL)
        ) * 25
    elif river_level < EVACUATION_LEVEL:
        score = 50 + (
            (river_level - CAUTION_LEVEL)
            / (EVACUATION_LEVEL - CAUTION_LEVEL)
        ) * 25
    elif river_level < DANGER_LEVEL:
        score = 75 + (
            (river_level - EVACUATION_LEVEL)
            / (DANGER_LEVEL - EVACUATION_LEVEL)
        ) * 25
    else:
        score = 100

    score = min(max(score, 0), 100)

    if river_level < WAITING_LEVEL:
        level_text = "水防団待機水位未満"
    elif river_level < CAUTION_LEVEL:
        level_text = "水防団待機水位以上"
    elif river_level < EVACUATION_LEVEL:
        level_text = "氾濫注意水位以上"
    elif river_level < DANGER_LEVEL:
        level_text = "避難判断水位以上"
    else:
        level_text = "氾濫危険水位以上"

    return score, level_text


def calculate_tide_score(tide_level, tide_low, tide_high):
    """その日の毎時天文潮位予測の範囲内で現在値を0-100化。"""
    if tide_high > tide_low:
        score = (
            (tide_level - tide_low)
            / (tide_high - tide_low)
            * 100
        )
        score = min(max(score, 0), 100)
    else:
        score = 0

    if score < 25:
        text = "潮位は比較的低い"
    elif score < 50:
        text = "潮位はやや低い"
    elif score < 75:
        text = "潮位は高め"
    else:
        text = "満潮側に近い"

    return score, text


def external_flood_level(rank):
    if rank == 0:
        return "想定区域外"
    if rank == 1:
        return "低い"
    if rank == 2:
        return "中程度"
    if rank == 3:
        return "高い"
    return "非常に高い"


def estimate_inland_depth(inland_rank, rain_60):
    """
    内水の試作推定浸水深。

    53 mm/hを0%、130 mm/hを100%として降雨スケールを作り、
    公的内水浸水深区分を比例縮小する試作モデル。
    """
    drainage_reference = 53.0
    maximum_scenario = 130.0

    rain_factor = (
        (rain_60 - drainage_reference)
        / (maximum_scenario - drainage_reference)
    )
    rain_factor = max(0.0, min(rain_factor, 1.0))

    try:
        rank = int(float(inland_rank))
    except (ValueError, TypeError):
        rank = 0

    depth_ranges = {
        1: (1, 19),
        2: (20, 49),
        3: (50, 99),
        4: (100, None),
    }

    if rank not in depth_ranges:
        return 0.0, 0.0, rain_factor

    min_depth, max_depth = depth_ranges[rank]
    estimated_min = min_depth * rain_factor
    estimated_max = None if max_depth is None else max_depth * rain_factor

    return estimated_min, estimated_max, rain_factor


def format_estimated_depth(estimated_min, estimated_max, rain_factor):
    if rain_factor == 0:
        return "0 cm（試作式上）"
    if estimated_max is None:
        return f"{estimated_min:.0f} cm以上相当"
    return f"{estimated_min:.0f}～{estimated_max:.0f} cm"


def drainage_condition(river_level, tide_score):
    """
    河川水位・潮位から、雨水を排出しにくくなる可能性を
    試作的に段階判定する。
    """
    reasons = []

    if river_level >= DANGER_LEVEL:
        river_effect = 3
        reasons.append("河川水位が氾濫危険水位以上")
    elif river_level >= EVACUATION_LEVEL:
        river_effect = 2
        reasons.append("河川水位が避難判断水位以上")
    elif river_level >= CAUTION_LEVEL:
        river_effect = 1
        reasons.append("河川水位が氾濫注意水位以上")
    else:
        river_effect = 0

    if tide_score >= 75:
        tide_effect = 2
        reasons.append("天文潮位予測がその日の高い側に近い")
    elif tide_score >= 50:
        tide_effect = 1
        reasons.append("天文潮位予測が比較的高い")
    else:
        tide_effect = 0

    total_effect = river_effect + tide_effect

    if total_effect == 0:
        level = "小さい"
    elif total_effect <= 2:
        level = "注意"
    elif total_effect <= 3:
        level = "大きい"
    else:
        level = "非常に大きい"

    return level, reasons


# ==========================================
# セッション状態
# ==========================================

def init_state():
    defaults = {
        "official_data_success": False,
        "official_data_errors": [],
        "rain_10": 0.0,
        "rain_60": 0.0,
        "rain_total": 0.0,
        "rain_observed_at": None,
        "river_level": 0.0,
        "river_observed_at": None,
        "tide_level": 0.0,
        "tide_low": 0.0,
        "tide_high": 0.0,
        "tide_observed_at": None,
        "latitude": 34.3853,
        "longitude": 132.4553,
        "analysis_done": False,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def mark_analysis_stale():
    """入力値が変わったら、前回分析結果とAI説明をいったん無効化。"""
    st.session_state.analysis_done = False


init_state()


# ==========================================
# タイトル
# ==========================================

st.title("🌧️ 広島市 浸水リスク分析")
st.write(
    "雨量・河川水位・地形・潮位・公式ハザード情報から、"
    "広島市役所周辺の浸水リスクを簡易的に分析します。"
)
st.info(
    "このシステムはハッカソン用の試作モデルです。"
    "実際の避難判断には、自治体・気象庁などの最新の公式防災情報を利用してください。"
)


# ==========================================
# 1. 公式データ・入力値
# ==========================================

st.header("1. 気象・河川・潮位データ")
st.subheader("📡 最新の公式データ")

if st.button(
    "📡 雨量・河川水位・潮位を一括取得",
    use_container_width=True,
):
    errors = []

    try:
        latest_10, latest_60, latest_24h, rain_time = get_hiroshima_rainfall()
        st.session_state.rain_10 = latest_10
        st.session_state.rain_60 = latest_60
        st.session_state.rain_total = latest_24h
        st.session_state.rain_observed_at = rain_time
    except Exception as e:
        errors.append(f"雨量：{e}")

    try:
        latest_river, river_time = get_misasa_river_level()
        st.session_state.river_level = latest_river
        st.session_state.river_observed_at = river_time
    except Exception as e:
        errors.append(f"河川水位：{type(e).__name__}: {e}")

    try:
        (
            latest_tide,
            latest_tide_low,
            latest_tide_high,
            tide_time,
        ) = get_hiroshima_tide()
        st.session_state.tide_level = latest_tide
        st.session_state.tide_low = latest_tide_low
        st.session_state.tide_high = latest_tide_high
        st.session_state.tide_observed_at = tide_time
    except Exception as e:
        errors.append(f"潮位：{e}")

    st.session_state.official_data_errors = errors
    st.session_state.official_data_success = len(errors) == 0
    mark_analysis_stale()

if st.session_state.official_data_success:
    st.success("✅ 雨量・河川水位・潮位の公式データを取得しました")

if st.session_state.official_data_errors:
    st.warning("一部のデータを取得できませんでした。")
    for error in st.session_state.official_data_errors:
        st.write(f"・{error}")


# --- 雨量 ---
st.subheader("🌧️ 雨量データ")

if st.button("🔄 気象庁から最新雨量を取得"):
    try:
        latest_10, latest_60, latest_24h, observed_at = get_hiroshima_rainfall()
        st.session_state.rain_10 = latest_10
        st.session_state.rain_60 = latest_60
        st.session_state.rain_total = latest_24h
        st.session_state.rain_observed_at = observed_at
        st.session_state.official_data_errors = []
        mark_analysis_stale()
    except Exception as e:
        st.error("気象庁データを取得できませんでした。")
        st.caption(f"取得エラー：{e}")

rain_col1, rain_col2, rain_col3 = st.columns(3)

with rain_col1:
    rain_10 = st.number_input(
        "10分雨量（mm）",
        min_value=0.0,
        step=0.5,
        key="rain_10",
        on_change=mark_analysis_stale,
    )

with rain_col2:
    rain_60 = st.number_input(
        "60分雨量（mm）",
        min_value=0.0,
        step=0.5,
        key="rain_60",
        on_change=mark_analysis_stale,
    )

with rain_col3:
    rain_total = st.number_input(
        "24時間降水量（mm）",
        min_value=0.0,
        step=0.5,
        key="rain_total",
        on_change=mark_analysis_stale,
    )

if st.session_state.rain_observed_at is not None:
    rain_time = st.session_state.rain_observed_at
    if hasattr(rain_time, "strftime"):
        rain_time = rain_time.strftime("%Y/%m/%d %H:%M")
    st.caption(f"📡 気象庁 アメダス広島（67437） {rain_time} 観測")


# --- 河川 ---
st.subheader("🌊 河川水位")

if st.button("🔄 国・県の公式データから三篠橋水位を取得"):
    try:
        latest_river, river_time = get_misasa_river_level()
        st.session_state.river_level = latest_river
        st.session_state.river_observed_at = river_time
        mark_analysis_stale()
    except Exception as e:
        st.error("三篠橋の水位を取得できませんでした。")
        st.caption(f"取得エラー：{e}")

river_level = st.number_input(
    "三篠橋 水位（m）",
    step=0.01,
    format="%.2f",
    key="river_level",
    on_change=mark_analysis_stale,
)

if st.session_state.river_observed_at is not None:
    st.caption(
        "📡 広島県河川防災情報システム "
        f"三篠橋(国) {st.session_state.river_observed_at} 観測"
    )


# --- 潮位 ---
st.subheader("🌊 潮位")

if st.button("🔄 気象庁から広島の潮位を取得"):
    try:
        (
            latest_tide,
            latest_tide_low,
            latest_tide_high,
            tide_time,
        ) = get_hiroshima_tide()
        st.session_state.tide_level = latest_tide
        st.session_state.tide_low = latest_tide_low
        st.session_state.tide_high = latest_tide_high
        st.session_state.tide_observed_at = tide_time
        mark_analysis_stale()
    except Exception as e:
        st.error("潮位データを取得できませんでした。")
        st.caption(f"取得エラー：{e}")

tide_col1, tide_col2, tide_col3 = st.columns(3)

with tide_col1:
    tide_level = st.number_input(
        "天文潮位（cm・予測値）",
        step=1.0,
        key="tide_level",
        on_change=mark_analysis_stale,
    )

with tide_col2:
    tide_low = st.number_input(
        "今日の毎時予測 最低値（cm）",
        step=1.0,
        key="tide_low",
        on_change=mark_analysis_stale,
    )

with tide_col3:
    tide_high = st.number_input(
        "今日の毎時予測 最高値（cm）",
        step=1.0,
        key="tide_high",
        on_change=mark_analysis_stale,
    )

if st.session_state.tide_observed_at is not None:
    st.caption(
        "📡 気象庁 潮位表・広島 "
        f"{st.session_state.tide_observed_at} 天文潮位（予測）"
    )


# ==========================================
# 2. 地点選択
# ==========================================

st.header("2. 調べる地点")
st.subheader("🗺️ 地図をクリックして地点を選択")

location_map = folium.Map(
    location=[st.session_state.latitude, st.session_state.longitude],
    zoom_start=15,
)

folium.Marker(
    [st.session_state.latitude, st.session_state.longitude],
    tooltip="現在選択中の地点",
).add_to(location_map)

map_result = st_folium(
    location_map,
    height=450,
    use_container_width=True,
    returned_objects=["last_clicked"],
    key="location_map",
)

if map_result and map_result.get("last_clicked") is not None:
    clicked_lat = map_result["last_clicked"]["lat"]
    clicked_lon = map_result["last_clicked"]["lng"]

    if (
        abs(clicked_lat - st.session_state.latitude) > 0.000001
        or abs(clicked_lon - st.session_state.longitude) > 0.000001
    ):
        st.session_state.latitude = clicked_lat
        st.session_state.longitude = clicked_lon
        mark_analysis_stale()
        st.rerun()

coord_col1, coord_col2 = st.columns(2)

with coord_col1:
    latitude = st.number_input(
        "緯度",
        format="%.6f",
        key="latitude",
        on_change=mark_analysis_stale,
    )

with coord_col2:
    longitude = st.number_input(
        "経度",
        format="%.6f",
        key="longitude",
        on_change=mark_analysis_stale,
    )

st.success(
    f"📍 選択地点：緯度 {latitude:.6f} / 経度 {longitude:.6f}"
)


# ==========================================
# 3. 分析
# ==========================================

if st.button(
    "分析する",
    type="primary",
    use_container_width=True,
):
    st.session_state.analysis_done = True


if st.session_state.analysis_done:
    # 毎回ここで再取得することで、別のウィジェットが再実行されても
    # grid_data / grid_distance が未定義にならないようにする。
    grid_data, grid_distance = find_nearest_grid(latitude, longitude)

    if grid_data is None:
        st.error("格子データを取得できませんでした。")
        st.stop()

    if grid_distance > 100:
        st.error("指定地点は現在の解析範囲外の可能性があります。")
        st.write(f"最も近い格子まで：{grid_distance:.1f} m")
        st.stop()

    # 選択地点の静的情報
    elevation = float(grid_data["標高_m"])
    river_distance = float(grid_data["河川からの距離_m"])
    nearest_river = grid_data["最近接河川"]
    inland_depth = grid_data["内水想定浸水深"]
    inland_rank = grid_data["内水浸水ランク"]
    selected_external_rank = int(grid_data["外水浸水ランク_num"])
    selected_external_depth = grid_data["外水想定浸水深"]
    selected_external_river = grid_data["最大ランク河川"]

    # 雨量スコア
    rain_10_score = min(max(rain_10 / 40 * 100, 0), 100)
    rain_60_score = min(max(rain_60 / 80 * 100, 0), 100)
    inland_risk = max(rain_10_score, rain_60_score)

    # 地形脆弱性
    elevation_score = elevation_vulnerability(elevation)

    # 河川・潮位
    river_score, river_level_text = calculate_river_risk(river_level)
    tide_score, tide_level_text = calculate_tide_score(
        tide_level, tide_low, tide_high
    )

    # 外水の表示用判定
    selected_external_level = external_flood_level(selected_external_rank)

    # 推定浸水深
    (
        estimated_min_depth,
        estimated_max_depth,
        rain_factor,
    ) = estimate_inland_depth(inland_rank, rain_60)

    estimated_depth_text = format_estimated_depth(
        estimated_min_depth,
        estimated_max_depth,
        rain_factor,
    )

    drainage_level, drainage_reasons = drainage_condition(
        river_level,
        tide_score,
    )

    # 周辺地図で共通利用
    nearby_grids = get_nearby_grids(latitude, longitude, radius_m=800)

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "📊 分析結果",
            "🏙️ 地形脆弱性マップ",
            "🌊 外水想定浸水深",
            "📏 推定浸水深",
            "🗺️ 推定浸水深マップ",
        ]
    )

    # ------------------------------------------
    # tab1 分析結果
    # ------------------------------------------
    with tab1:
        st.header("📊 分析結果")

        result_col1, result_col2, result_col3, result_col4 = st.columns(4)

        with result_col1:
            show_risk_card(
                "🌧️ 内水危険度",
                inland_risk,
                risk_level(inland_risk),
            )

        with result_col2:
            show_risk_card(
                "🏙️ 地形脆弱性",
                elevation_score,
                risk_level(elevation_score),
            )

        with result_col3:
            show_risk_card(
                "🌊 河川危険度",
                river_score,
                river_level_text,
            )

        with result_col4:
            show_risk_card(
                "🌊 潮位影響度",
                tide_score,
                tide_level_text,
            )

        st.subheader("🌧️ 雨量")
        rain_result1, rain_result2, rain_result3 = st.columns(3)

        with rain_result1:
            st.metric(
                "10分雨量",
                f"{rain_10:.1f} mm",
                f"スコア {rain_10_score:.0f}",
            )
        with rain_result2:
            st.metric(
                "60分雨量",
                f"{rain_60:.1f} mm",
                f"スコア {rain_60_score:.0f}",
            )
        with rain_result3:
            st.metric("24時間降水量", f"{rain_total:.1f} mm")

        st.subheader("📍 地点情報")
        point_col1, point_col2, point_col3 = st.columns(3)

        with point_col1:
            st.metric("標高", f"{elevation:.2f} m")
        with point_col2:
            st.metric("最寄り河川", nearest_river)
        with point_col3:
            st.metric("河川までの距離", f"{river_distance:.0f} m")

        st.subheader("🗺️ 公的ハザード情報")
        with st.container(border=True):
            st.write(f"**内水想定浸水深：** {inland_depth}")
            st.write(f"**外水地形脆弱性：** {selected_external_level}")
            st.write(f"**外水想定浸水深：** {selected_external_depth}")
            st.write(f"**対象河川：** {selected_external_river}")

        st.subheader("📡 使用データの時刻")
        with st.container(border=True):
            if st.session_state.rain_observed_at is not None:
                rain_time = st.session_state.rain_observed_at
                if hasattr(rain_time, "strftime"):
                    rain_time = rain_time.strftime("%Y/%m/%d %H:%M")
                st.write(f"🌧️ 雨量：{rain_time}")
            else:
                st.write("🌧️ 雨量：手入力または時刻情報なし")

            if st.session_state.river_observed_at is not None:
                st.write(
                    "🌊 河川水位（三篠橋）："
                    f"{st.session_state.river_observed_at}"
                )
            else:
                st.write("🌊 河川水位：手入力または時刻情報なし")

            if st.session_state.tide_observed_at is not None:
                st.write(
                    "🌊 天文潮位（予測）："
                    f"{st.session_state.tide_observed_at}"
                )
            else:
                st.write("🌊 天文潮位：手入力または時刻情報なし")

    # ------------------------------------------
    # tab2 地形脆弱性マップ
    # ------------------------------------------
    with tab2:
        st.subheader("🏙️ 周辺50m格子 地形脆弱性マップ")
        st.caption(
            "標高と公的な内水浸水想定区域との関係から作成した試作モデルです。"
            "現在の浸水状況を示すものではありません。"
        )

        terrain_map = folium.Map(
            location=[latitude, longitude],
            zoom_start=15,
        )

        for grid in nearby_grids:
            grid_lat = float(grid["緯度"])
            grid_lon = float(grid["経度"])
            grid_elevation = float(grid["標高_m"])
            grid_score = elevation_vulnerability(grid_elevation)
            color = terrain_color(grid_score)

            folium.CircleMarker(
                location=[grid_lat, grid_lon],
                radius=5,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.65,
                weight=1,
                tooltip=(
                    f"標高：{grid_elevation:.2f} m"
                    f"<br>地形脆弱性：{grid_score:.0f}"
                ),
            ).add_to(terrain_map)

        folium.Marker(
            [latitude, longitude],
            tooltip="選択地点",
            icon=folium.Icon(color="black", icon="info-sign"),
        ).add_to(terrain_map)

        st.caption("🟢 低い　🟠 注意　🔴 高い　濃赤 非常に高い")
        folium_static(terrain_map, width=900, height=500)

    # ------------------------------------------
    # tab3 外水想定浸水深マップ
    # ------------------------------------------
    with tab3:
        st.subheader("🌊 外水想定浸水深マップ")
        st.caption(
            "公的ハザードデータの想定最大規模による浸水深を、"
            "このアプリ独自の色で表示しています。"
        )
        st.markdown(
            """
            **表示色（アプリ独自）**

            ⚪ 想定区域外  
            🟢 0.5m未満  
            🟠 0.5～3.0m未満  
            🔴 3.0～5.0m未満  
            濃赤 5.0m以上
            """
        )

        external_map = folium.Map(
            location=[latitude, longitude],
            zoom_start=15,
        )

        for grid in nearby_grids:
            grid_lat = float(grid["緯度"])
            grid_lon = float(grid["経度"])
            grid_external_rank = int(grid["外水浸水ランク_num"])
            grid_external_depth = grid["外水想定浸水深"]
            grid_external_river = grid["最大ランク河川"]
            color = external_flood_color(grid_external_rank)

            folium.CircleMarker(
                location=[grid_lat, grid_lon],
                radius=5,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.7,
                weight=1,
                tooltip=(
                    f"想定浸水深：{grid_external_depth}"
                    f"<br>対象河川：{grid_external_river}"
                ),
            ).add_to(external_map)

        folium.Marker(
            [latitude, longitude],
            tooltip="選択地点",
            icon=folium.Icon(color="black", icon="info-sign"),
        ).add_to(external_map)

        folium_static(external_map, width=900, height=500)

    # ------------------------------------------
    # tab4 推定浸水深
    # ------------------------------------------
    with tab4:
        st.subheader("📏 現在の雨量からみた内水推定浸水深")
        st.metric("試作推定浸水深", estimated_depth_text)
        st.write(f"60分雨量：**{rain_60:.1f} mm**")
        st.write(f"降雨スケール：**{rain_factor * 100:.0f}%**")
        st.write(f"公的な内水想定浸水深：**{inland_depth}**")

        st.caption(
            "53 mm/hを0%、130 mm/hを100%として、公的ハザードデータの"
            "浸水深区分を現在の60分雨量に応じて比例縮小した試作値です。"
        )
        st.warning(
            "これは実際の浸水深を予報する公式モデルではありません。"
            "排水施設の稼働状況、周囲から流れ込む水、河川水位、潮位などを"
            "浸水深の数値そのものには十分反映していません。"
        )

        st.divider()
        st.subheader("🌊 排水条件による影響")
        st.metric("排水しにくくなる可能性", drainage_level)

        if drainage_reasons:
            st.write("今回の判定に影響した要因：")
            for reason in drainage_reasons:
                st.write(f"・{reason}")
        else:
            st.write(
                "河川水位・潮位による大きな補正要因は、"
                "現在の試作判定では検出されていません。"
            )

        if drainage_level in ["大きい", "非常に大きい"]:
            st.warning(
                "河川水位または潮位が高いため、雨量だけから求めた"
                "推定浸水深では実際の浸水を過小評価する可能性があります。"
            )

    # ------------------------------------------
    # tab5 推定浸水深マップ
    # ------------------------------------------
    with tab5:
        st.subheader("🗺️ 周辺50m格子 試作推定浸水深マップ")
        st.caption(
            "現在の60分雨量と各50m格子の公的な内水浸水ランクを使って、"
            "試作推定浸水深を計算しています。"
            "実際の浸水状況や公式の浸水予測ではありません。"
        )

        estimated_map = folium.Map(
            location=[latitude, longitude],
            zoom_start=15,
        )

        for grid in nearby_grids:
            grid_lat = float(grid["緯度"])
            grid_lon = float(grid["経度"])
            grid_inland_rank = grid["内水浸水ランク"]

            (
                grid_estimated_min,
                grid_estimated_max,
                grid_rain_factor,
            ) = estimate_inland_depth(grid_inland_rank, rain_60)

            if grid_rain_factor == 0:
                depth_for_color = 0
                depth_text = "0 cm（試作式上）"
            elif grid_estimated_max is None:
                depth_for_color = grid_estimated_min
                depth_text = f"{grid_estimated_min:.0f} cm以上相当"
            else:
                depth_for_color = grid_estimated_max
                depth_text = (
                    f"{grid_estimated_min:.0f}～"
                    f"{grid_estimated_max:.0f} cm"
                )

            color = estimated_depth_color(depth_for_color)

            folium.CircleMarker(
                location=[grid_lat, grid_lon],
                radius=5,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.70,
                weight=1,
                tooltip=(
                    f"試作推定浸水深：{depth_text}"
                    f"<br>内水浸水ランク：{grid_inland_rank}"
                    f"<br>60分雨量：{rain_60:.1f} mm"
                ),
            ).add_to(estimated_map)

        folium.Marker(
            [latitude, longitude],
            tooltip="選択地点",
            icon=folium.Icon(color="black", icon="info-sign"),
        ).add_to(estimated_map)

        st.markdown(
            """
            **表示色（アプリ独自）**

            ⚪ 0 cm  
            🟢 0～10 cm未満  
            🟠 10～30 cm未満  
            🔴 30～50 cm未満  
            濃赤 50 cm以上
            """
        )

        folium_static(estimated_map, width=900, height=500)

        st.warning(
            "この地図は試作モデルによる推定です。周辺格子すべてに同じ60分雨量を"
            "適用しており、排水施設、細かな地表面の水の流れ、河川水位、潮位などを"
            "浸水深の数値そのものには十分反映していません。"
        )

    # 全タブ共通の注意
    st.warning(
        "公的ハザード情報の想定浸水深は、現在入力した雨量から計算した値ではありません。"
        "想定条件に基づく静的な情報です。"
    )
    st.caption(
        "※ このシステムはハッカソン用の試作モデルです。"
        "実際の避難判断には、気象庁・広島市・広島県などの最新の公式情報を確認してください。"
    )

# 起動例:
# python -m streamlit run "app_fixed.py" --server.port 8502
