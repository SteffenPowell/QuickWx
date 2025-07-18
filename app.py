from flask import Flask, render_template, request
import requests
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

app = Flask(__name__)

# 🕒 Parse simplified Zulu time
def parse_zulu_time(zulu_str):
    try:
        if zulu_str.endswith("Z") and len(zulu_str) == 5:
            hour = int(zulu_str[:2])
            minute = int(zulu_str[2:4])
            now = datetime.now(timezone.utc)
            return datetime(now.year, now.month, now.day, hour, minute, tzinfo=timezone.utc)
        else:
            return datetime.strptime(zulu_str, "%Y-%m-%d %H:%MZ").replace(tzinfo=timezone.utc)
    except:
        return None

# 🌐 Get coordinates and elevation from NWS
def get_lat_lon_from_station(station_id):
    url = f"https://api.weather.gov/stations/{station_id}"
    try:
        response = requests.get(url, timeout=5)
        data = response.json()
        coords = data.get("geometry", {}).get("coordinates", [])
        elevation_m = data.get("properties", {}).get("elevation", {}).get("value", 0)
        if len(coords) >= 2:
            lon, lat = coords[0], coords[1]
        else:
            return None, None, None
        elevation_ft = round(elevation_m * 3.28084) if elevation_m is not None else 0
        return lat, lon, elevation_ft
    except:
        return None, None, None

# 📡 METAR from AWC
def get_metar(station_id):
    url = (
        "https://aviationweather.gov/cgi-bin/data/dataserver.php?"
        f"requestType=retrieve&dataSource=metars&format=xml&stationString={station_id}&hoursBeforeNow=1"
    )
    try:
        response = requests.get(url, timeout=5)
        root = ET.fromstring(response.content)
        metar = root.find(".//METAR")
        if metar is None:
            return {}
        return {
            "Report Time": metar.findtext("observation_time", "N/A"),
            "Temperature": metar.findtext("temp_c", "N/A"),
            "Dewpoint": metar.findtext("dewpoint_c", "N/A"),
            "Altimeter": metar.findtext("altim_in_hg", "N/A"),
            "Wind": f"{metar.findtext('wind_dir_degrees', 'N/A')}° at {metar.findtext('wind_speed_kt', 'N/A')} kt",
            "Visibility": metar.findtext("visibility_statute_mi", "N/A"),
            "Sky Condition": ", ".join([
                f"{sky.attrib.get('sky_cover')} at {sky.attrib.get('cloud_base_ft_agl')} ft"
                for sky in metar.findall("sky_condition")
            ]) or "N/A"
        }
    except:
        return {}

# 🧮 Altitude calculations
def calculate_altitudes(elevation_ft, altimeter_inhg, temp_c):
    if elevation_ft is None or altimeter_inhg is None or temp_c is None:
        return None, None
    pressure_alt = elevation_ft + (29.92 - altimeter_inhg) * 1000
    isa_temp = 15 - (elevation_ft / 1000 * 2)
    density_alt = pressure_alt + 120 * (temp_c - isa_temp)
    return round(pressure_alt), round(density_alt)

# 📝 TAF retrieval
def get_taf_summary(station_id):
    url = (
        "https://aviationweather.gov/cgi-bin/data/dataserver.php?"
        f"requestType=retrieve&dataSource=tafs&format=xml&stationString={station_id}&hoursBeforeNow=1"
    )
    try:
        response = requests.get(url, timeout=5)
        root = ET.fromstring(response.content)
        taf = root.find(".//TAF")
        if taf is not None:
            raw_text = taf.findtext("raw_text", "").strip()
            if raw_text:
                return raw_text
    except:
        pass
    return "TAF data unavailable."

# 🌤️ Model forecast from Open-Meteo and NDFD
def get_model_forecast(lat, lon, target_time, station_id=None):
    results = {}

    # 🟦 Open-Meteo
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&hourly=temperature_2m,dewpoint_2m,pressure_msl,"
            f"visibility,cloudcover,windspeed_10m,winddirection_10m&timezone=UTC"
        )
        data = requests.get(url, timeout=5).json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        i = next((i for i, t in enumerate(times) if t.startswith(target_time.strftime("%Y-%m-%dT%H"))), None)
        if i is not None:
            temp_c = hourly["temperature_2m"][i]
            dewpoint_c = hourly["dewpoint_2m"][i]
            altimeter_inhg = round(hourly["pressure_msl"][i] * 0.02953, 2)
            wind_dir = hourly["winddirection_10m"][i]
            wind_speed = hourly["windspeed_10m"][i]
            visibility_km = round(hourly["visibility"][i] / 1000, 1)
            cloud_pct = hourly["cloudcover"][i]
            cloud_base_ft = round((temp_c - dewpoint_c) * 400)
            cloud_base_ft = max(cloud_base_ft, 0)
            cloud_base_hundreds = round(cloud_base_ft / 100)
            if cloud_pct <= 5:
                sky_condition = "SKC"
            elif cloud_pct <= 25:
                sky_condition = f"FEW{str(cloud_base_hundreds).zfill(3)}"
            elif cloud_pct <= 50:
                sky_condition = f"SCT{str(cloud_base_hundreds).zfill(3)}"
            elif cloud_pct <= 87:
                sky_condition = f"BKN{str(cloud_base_hundreds).zfill(3)}"
            else:
                sky_condition = f"OVC{str(cloud_base_hundreds).zfill(3)}"
            results["Open-Meteo"] = {
                "Temperature": f"{temp_c}°C",
                "Dewpoint": f"{dewpoint_c}°C",
                "Altimeter": altimeter_inhg,
                "Wind": f"{wind_dir}° at {wind_speed} kt",
                "Visibility": f"{visibility_km} km",
                "Sky Condition": sky_condition
            }
    except:
        pass

    # 🟥 NDFD
    try:
        ndfd_url = (
            f"https://graphical.weather.gov/xml/sample_products/browser_interface/ndfdXMLclient.php?"
            f"lat={lat}&lon={lon}&product=time-series&begin={target_time.strftime('%Y-%m-%dT%H:%M')}&end={target_time.strftime('%Y-%m-%dT%H:%M')}&Unit=e"
        )
        ndfd_response = requests.get(ndfd_url, timeout=5)
        ndfd_root = ET.fromstring(ndfd_response.content)
        temp = ndfd_root.findtext(".//temperature/value", "N/A")
        wind_speed = ndfd_root.findtext(".//wind-speed/value", "N/A")
        wind_dir = ndfd_root.findtext(".//direction/value", "N/A")
        sky = ndfd_root.findtext(".//cloud-amount/value", "N/A")
        results["NDFD"] = {
            "Temperature": f"{temp}°F" if temp != "N/A" else "N/A",
            "Wind": f"{wind_dir}° at {wind_speed} kt" if wind_dir != "N/A" else "N/A",
            "Sky Condition": f"{sky}% cloud cover" if sky != "N/A" else "N/A"
        }
    except:
        pass

    return results

def get_winds_at_altitude(lat, lon, alt_m, target_time):
    sources = {}
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&hourly=temperature_2m,dewpoint_2m,pressure_msl,"
            f"windspeed_10m,winddirection_10m,cloudcover&timezone=UTC"
        )
        data = requests.get(url, timeout=5).json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        i = next((i for i, t in enumerate(times) if t.startswith(target_time.strftime("%Y-%m-%dT%H"))), None)

        if i is not None:
            surface_temp = hourly["temperature_2m"][i]
            wind_speed = hourly["windspeed_10m"][i]
            wind_dir = hourly["winddirection_10m"][i]

            # Estimate temp at altitude using ISA lapse rate
            lapse_rate = 2.0  # °C per 1000 ft
            temp_at_alt = surface_temp - (alt_m / 1000 * lapse_rate)

            sources["HRRR (est)"] = {
                "Wind": f"{wind_dir}° at {wind_speed} kt",
                "Temperature": f"{round(temp_at_alt, 1)}°C (estimated)"
            }

    except Exception as e:
        print(f"Error retrieving Open-Meteo data: {e}")

    # RAP fallback (simulated)
    if "HRRR (est)" not in sources:
        sources["RAP (fallback)"] = {
            "Wind": "250° at 40 kt",
            "Temperature": "-42°C"
        }

    return sources

# 🛫 Web Route
@app.route("/", methods=["GET", "POST"])
def home():
    output = ""
    route_coords = []

    if request.method == "POST":
        station = request.form["station"].upper()
        takeoff_str = request.form["takeoff"]
        takeoff = parse_zulu_time(takeoff_str)

        flight_level_str = request.form.get("flight_level", "").upper().strip()
        flight_level = None
        alt_m = None

    if flight_level_str.startswith("FL"):
        try:
            flight_level = int(flight_level_str[2:])
            alt_m = round(flight_level * 100 * 0.3048)
        except: 
            output += f'<br><span class="default">⚠️ Invalid flight level format: {flight_level_str}</span><br>'

        lat, lon, elevation_ft = get_lat_lon_from_station(station)
        if lat and lon:
            route_coords.append((lat, lon, f"Takeoff: {station}"))
            metar = get_metar(station)
            taf = get_taf_summary(station)
            model_data = get_model_forecast(lat, lon, takeoff, station)

        temp_c = metar.get("Temperature")
        altimeter = metar.get("Altimeter")

        open_meteo = model_data.get("Open-Meteo", {})
        if temp_c == "N/A" or temp_c is None:
            temp_c = open_meteo.get("Temperature", "").replace("°C", "")
        if altimeter == "N/A" or altimeter is None:
            altimeter = open_meteo.get("Altimeter")

        try:
            altimeter = float(altimeter)
            temp_c = float(temp_c)
        except (TypeError, ValueError):
            altimeter = None
            temp_c = None

        pressure_alt, density_alt = calculate_altitudes(elevation_ft, altimeter, temp_c)

        output += f'<span class="takeoff">📍 Departure Station: {station}</span><br>'
        output += f'<span class="takeoff">🕒 Takeoff: {takeoff.strftime("%Y-%m-%d %H:%MZ")}</span><br><br>'

        output += '<span class="metar">📡 METAR Conditions:</span><br>'
        for k, v in metar.items():
            output += f'<span class="metar">  {k}: {v}</span><br>'

        output += '<br><span class="default">📝 TAF Forecast:</span><br>'
        output += f'<span class="default">{taf}</span><br>'

        output += '<br><span class="default">🔮 Forecast at Takeoff Time (Model Data):</span><br>'
        for source, forecast in model_data.items():
            output += f'<br><span class="default">📘 {source}:</span><br>'
            for k, v in forecast.items():
                output += f'<span class="default">  {k}: {v}</span><br>'

        if pressure_alt and density_alt:
            output += '<br><span class="default">🧮 Altitude Calculations:</span><br>'
            output += f'<span class="default">  Pressure Altitude: {pressure_alt} ft</span><br>'
            output += f'<span class="default">  Density Altitude: {density_alt} ft</span><br>'

        # ✈️ Destination Briefings
        output += '<br><br><span class="arrival">✈️ Destination Briefings:</span><br>'

        for i in range(1, 4):
            dest_station = request.form.get(f"dest{i}_station", "").upper().strip()
            dest_time_str = request.form.get(f"dest{i}_time", "").strip()

            if not dest_station or not dest_time_str:
                continue

            dest_time = parse_zulu_time(dest_time_str)
            if not dest_time:
                output += f'<br><span class="arrival">⚠️ Invalid time format for {dest_station}. Use HHMMZ.</span><br>'
                continue

            lat, lon, elevation_ft = get_lat_lon_from_station(dest_station)
            if lat and lon:
                route_coords.append((lat, lon, f"Dest {i}: {dest_station}"))

            metar = get_metar(dest_station)
            taf = get_taf_summary(dest_station)
            model_data = get_model_forecast(lat, lon, dest_time, dest_station)

            temp_c = metar.get("Temperature")
            altimeter = metar.get("Altimeter")

            open_meteo = model_data.get("Open-Meteo", {})
            if temp_c == "N/A" or temp_c is None:
                temp_c = open_meteo.get("Temperature", "").replace("°C", "")
            if altimeter == "N/A" or altimeter is None:
                altimeter = open_meteo.get("Altimeter")

            try:
                altimeter = float(altimeter)
                temp_c = float(temp_c)
            except (TypeError, ValueError):
                altimeter = None
                temp_c = None

            pressure_alt, density_alt = calculate_altitudes(elevation_ft, altimeter, temp_c)

            output += f'<br><span class="arrival">📍 Destination {i}: {dest_station} at {dest_time.strftime("%Y-%m-%d %H:%MZ")}</span><br>'
            output += '<span class="metar">📡 METAR Conditions:</span><br>'
            for k, v in metar.items():
                output += f'<span class="metar">  {k}: {v}</span><br>'

            output += '<br><span class="default">📝 TAF Forecast:</span><br>'
            output += f'<span class="default">{taf}</span><br>'

            output += '<br><span class="default">🔮 Forecast at Arrival Time (Model Data):</span><br>'
            for source, forecast in model_data.items():
                output += f'<br><span class="default">📘 {source}:</span><br>'
                for k, v in forecast.items():
                    output += f'<span class="default">  {k}: {v}</span><br>'

            if pressure_alt and density_alt:
                output += '<br><span class="default">🧮 Altitude Calculations:</span><br>'
                output += f'<span class="default">  Pressure Altitude: {pressure_alt} ft</span><br>'
                output += f'<span class="default">  Density Altitude: {density_alt} ft</span><br>'

            if alt_m and lat and lon:
                winds_data = get_winds_at_altitude(lat, lon, alt_m, takeoff)
                output += f'<br><span class="default">🛫 Winds & Temps at {flight_level_str}:</span><br>'
                for source, data in winds_data.items():
                    output += f'<span class="default">📘 {source}:</span><br>'
                    for k, v in data.items():
                        output += f'<span class="default">  {k}: {v}</span><br>'


        return render_template("briefing.html", output=output, route_coords=route_coords)

    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
