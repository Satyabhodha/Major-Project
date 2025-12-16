# app.py
import os
import datetime
from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, jsonify, current_app
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required,
    current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import config

# Earth Engine + folium (optional)
try:
    import ee
    import folium
except Exception:
    ee = None
    folium = None

from models import db, User, IoTReading

# ----------------------
# App setup
# ----------------------
app = Flask(__name__, instance_relative_config=True)
app.secret_key = getattr(config, "FLASK_SECRET", "supersecret")

os.makedirs(app.instance_path, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(app.instance_path, 'farming.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

# ----------------------
# Login manager
# ----------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(uid):
    # Admin 'user' has id "admin" (string) in session; handle separately
    try:
        return User.query.get(int(uid))
    except Exception:
        # If uid == 'admin' we created a fake admin object in admin_login route
        return None

# ----------------------
# Earth Engine init (optional)
# ----------------------
PROJECT_ID = "farmingaiproject"
def init_ee():
    if ee is None:
        app.logger.info("Earth Engine not available (ee import failed).")
        return False
    try:
        ee.Initialize(project=PROJECT_ID)
        app.logger.info("Earth Engine initialized.")
        return True
    except Exception:
        try:
            ee.Authenticate()
            ee.Initialize(project=PROJECT_ID)
            app.logger.info("Earth Engine initialized after authentication.")
            return True
        except Exception as e:
            app.logger.error("Earth Engine init failed: %s", e)
            return False

ee_ok = init_ee()

# ----------------------
# Helper: latest Sentinel-2 image (optional)
# ----------------------
def get_latest_s2_image(geometry):
    if not ee_ok:
        return None
    end = datetime.date.today()
    start = end - datetime.timedelta(days=60)
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR")
        .filterBounds(geometry)
        .filterDate(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        .sort("CLOUDY_PIXEL_PERCENTAGE")
    )
    return collection.first()

def get_true_color_map(lat, lon):
    if not (ee_ok and folium):
        return "<p>True color map unavailable (EE/folium not installed).</p>"
    point = ee.Geometry.Point(lon, lat)
    img = get_latest_s2_image(point)
    if img is None:
        m = folium.Map(location=[lat, lon], zoom_start=10)
        return m._repr_html_()
    vis = {"min": 0, "max": 3000, "bands": ["B4", "B3", "B2"]}
    try:
        map_id = img.getMapId(vis)
        tile_url = map_id["tile_fetcher"].url_format
    except Exception:
        return "<p>True color map error.</p>"
    m = folium.Map(location=[lat, lon], zoom_start=10)
    folium.TileLayer(tiles=tile_url, attr="Sentinel-2 True Color").add_to(m)
    return m._repr_html_()

def get_point_ndvi(lat, lon):
    if not (ee_ok and folium):
        return None, "<p>NDVI unavailable (EE/folium missing).</p>"
    point = ee.Geometry.Point(lon, lat)
    img = get_latest_s2_image(point)
    if img is None:
        return None, "<p>No Sentinel-2 image.</p>"
    ndvi = img.normalizedDifference(['B8', 'B4']).rename('NDVI')
    vis = {'min': -1, 'max': 1, 'palette': ['red','yellow','green']}
    try:
        map_id = ndvi.getMapId(vis)
        tile_url = map_id['tile_fetcher'].url_format
    except Exception:
        return None, "<p>NDVI map error.</p>"
    m = folium.Map([lat, lon], zoom_start=10)
    folium.TileLayer(tiles=tile_url, attr="NDVI").add_to(m)
    html = m._repr_html_()
    # NDVI mean at point
    try:
        ndvi_value = ndvi.reduceRegion(ee.Reducer.mean(), geometry=point, scale=10).get('NDVI').getInfo()
    except Exception:
        ndvi_value = None
    return ndvi_value, html

def get_polygon_ndvi_stats(coords):
    if not ee_ok:
        return None
    ee_coords = [[lon, lat] for lat, lon in coords]
    poly = ee.Geometry.Polygon([ee_coords])
    img = get_latest_s2_image(poly)
    if img is None:
        return None
    ndvi = img.normalizedDifference(['B8','B4']).rename('NDVI')
    stats = ndvi.reduceRegion(
        reducer=ee.Reducer.minMax()
            .combine(ee.Reducer.mean(), sharedInputs=True)
            .combine(ee.Reducer.stdDev(), sharedInputs=True),
        geometry=poly,
        scale=10,
        maxPixels=1e10
    ).getInfo()
    mapped = {
        "NDVI_min": stats.get("NDVI_min"),
        "NDVI_max": stats.get("NDVI_max"),
        "NDVI_mean": stats.get("NDVI_mean"),
        "NDVI_stdDev": stats.get("NDVI_stdDev"),
    }
    return mapped

def get_ndvi_timeseries(lat, lon):
    if not ee_ok:
        return [], []
    point = ee.Geometry.Point(lon, lat)
    end = datetime.date.today()
    start = end - datetime.timedelta(days=180)
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR")
        .filterBounds(point)
        .filterDate(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        .sort("system:time_start")
    )
    def compute_ndvi(img):
        ndvi_img = img.normalizedDifference(["B8","B4"]).rename("NDVI")
        mean = ndvi_img.reduceRegion(ee.Reducer.mean(), geometry=point, scale=10).get("NDVI")
        date = img.date().format("YYYY-MM-dd")
        return ee.Feature(None, {"date": date, "NDVI": mean})
    features = collection.map(compute_ndvi).getInfo().get("features", [])
    dates = [f["properties"]["date"] for f in features]
    values = [f["properties"]["NDVI"] for f in features]
    return dates, values

# ----------------------
# Weather
# ----------------------
def fetch_weather(lat, lon):
    key = getattr(config, "OPENWEATHER_API_KEY", "")
    if not key:
        return None, None
    wurl = f"http://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={key}&units=metric"
    furl = f"http://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={key}&units=metric"
    try:
        w = requests.get(wurl, timeout=8).json()
        f = requests.get(furl, timeout=8).json()
        return w, f
    except Exception:
        return None, None

# ----------------------
# Routes - Authentication
# ----------------------
# Hard-coded admin credentials
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"  # change this in code in production

@app.route("/")
def home():
    if current_user.is_authenticated:
        # if logged in as DB user
        try:
            if hasattr(current_user, 'role') and current_user.role == "admin":
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('farmer_dashboard'))
        except Exception:
            # fallback
            return redirect(url_for('login'))
    # show simple landing page with admin login icon link in template
    return render_template("login.html")

@app.route("/login", methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password) and user.role == "farmer":
            login_user(user)
            return redirect(url_for('farmer_dashboard'))
        else:
            flash("Invalid username or password (farmer only).")
    return render_template("login.html")

@app.route("/register", methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        if User.query.filter_by(username=username).first():
            flash("Username already exists!")
            return redirect(url_for('register'))
        if not username or not password:
            flash("Username and password required.")
            return redirect(url_for('register'))
        hashed = generate_password_hash(password)
        user = User(username=username, password=hashed, role="farmer")
        db.session.add(user)
        db.session.commit()
        flash("Account created successfully. Login as farmer.")
        return redirect(url_for('login'))
    return render_template("register.html")

@app.route("/admin_login", methods=['GET','POST'])
def admin_login():
    # separate admin login (hard-coded)
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            # create a lightweight admin user object for session
            class FakeAdmin:
                def __init__(self):
                    self.id = "admin"
                    self.username = ADMIN_USERNAME
                    self.role = "admin"
                    self.is_active = True
                def get_id(self):
                    return "admin"
            admin_user = FakeAdmin()
            login_user(admin_user)
            return redirect(url_for('admin_dashboard'))
        flash("Invalid admin credentials.")
    return render_template("admin_login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ----------------------
# Dashboards
# ----------------------
@app.route("/farmer_dashboard")
@login_required
def farmer_dashboard():
    # Send google maps key into template
    return render_template("farmer_dashboard.html", google_maps_key=getattr(config, "GOOGLE_MAPS_API_KEY", ""))

@app.route("/admin_dashboard")
@login_required
def admin_dashboard():
    # Only allow admin role (fake admin created on login)
    # For DB-stored admin users (if any), check role too.
    # Here we assume only hard-coded admin has role 'admin'.
    if getattr(current_user, 'role', None) != 'admin' and current_user.username != ADMIN_USERNAME:
        flash("Admin access required.")
        return redirect(url_for('login'))
    users = User.query.all()
    return render_template("admin_dashboard.html", users=users)

# ----------------------
# NDVI API endpoints
# ----------------------
@app.route("/api/point_ndvi", methods=['POST'])
@login_required
def api_point_ndvi():
    data = request.get_json(force=True)
    lat = float(data.get('lat', 28.6139))
    lon = float(data.get('lon', 77.2090))
    ndvi_value, ndvi_map = get_point_ndvi(lat, lon)
    true_color = get_true_color_map(lat, lon)
    dates, values = get_ndvi_timeseries(lat, lon)
    weather, forecast = fetch_weather(lat, lon)
    return jsonify({
        "ndvi_value": ndvi_value,
        "ndvi_map": ndvi_map,
        "true_color": true_color,
        "ts_dates": dates,
        "ts_values": values,
        "weather": weather,
        "forecast": forecast
    })

@app.route("/api/polygon_ndvi", methods=['POST'])
@login_required
def api_polygon_ndvi():
    data = request.get_json(force=True)
    coords = data.get('coords', [])
    stats = get_polygon_ndvi_stats(coords)
    return jsonify({"stats": stats})

# ----------------------
# IoT endpoints
# ----------------------
IOT_API_KEY = getattr(config, "IOT_API_KEY", "replace_with_some_secret_key")

@app.route("/api/iot_upload", methods=['POST'])
def api_iot_upload():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"ok": False, "error": "no json"}), 400
    if data.get("api_key") != IOT_API_KEY:
        return jsonify({"ok": False, "error": "invalid api_key"}), 403
    try:
        r = IoTReading(
            cap_soil_pct = float(data.get("cap_soil_pct") or 0.0),
            res_soil_pct = float(data.get("res_soil_pct") or 0.0),
            rain_pct = float(data.get("rain_pct") or 0.0),
            water_level_pct = float(data.get("water_level_pct") or 0.0),
            temperature_c = float(data.get("temperature_c") or 0.0),
            humidity_pct = float(data.get("humidity_pct") or 0.0),
        )
        db.session.add(r)
        db.session.commit()
        return jsonify({"ok": True, "id": r.id})
    except Exception as e:
        current_app.logger.exception("Saving IoT reading failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/latest_iot", methods=['GET'])
@login_required
def api_latest_iot():
    r = IoTReading.query.order_by(IoTReading.timestamp.desc()).first()
    if not r:
        return jsonify({"ok": False, "error": "no readings"}), 404
    return jsonify({
        "ok": True,
        "timestamp": r.timestamp.isoformat(),
        "cap_soil_pct": r.cap_soil_pct,
        "res_soil_pct": r.res_soil_pct,
        "rain_pct": r.rain_pct,
        "water_level_pct": r.water_level_pct,
        "temperature_c": r.temperature_c,
        "humidity_pct": r.humidity_pct
    })

# ----------------------
# Run
# ----------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        app.logger.info("Database created/checked.")
    app.run(host="0.0.0.0", debug=True)
