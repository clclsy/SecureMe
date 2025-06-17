import os
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI"))
db = client["secureme"]

def log_scan_result(source, name, result, tags=None):
    db.scan_logs.insert_one({
        "source": source,
        "name": name,
        "result": result,
        "tags": tags or [],
        "timestamp": datetime.utcnow()
    })

def log_fbi_result(name, result):
    db.fbi_logs.insert_one({
        "name": name,
        "result": result,
        "timestamp": datetime.utcnow()
    })

def log_face_match(name, result, confidence):
    db.face_logs.insert_one({
        "name": name,
        "confidence": confidence,
        "result": result,
        "timestamp": datetime.utcnow()
    })
