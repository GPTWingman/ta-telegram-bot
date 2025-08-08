# server.py (smoke test)
import os
from flask import Flask

app = Flask(__name__)

@app.get("/health")
def health():
    return "ok", 200
