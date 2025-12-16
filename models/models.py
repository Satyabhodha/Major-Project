# models/models.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)  # hashed
    role = db.Column(db.String(20), nullable=False)       # "farmer" or "admin"

class IoTReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())
    cap_soil_pct = db.Column(db.Float)
    res_soil_pct = db.Column(db.Float)
    rain_pct = db.Column(db.Float)
    water_level_pct = db.Column(db.Float)
    temperature_c = db.Column(db.Float)
    humidity_pct = db.Column(db.Float)
