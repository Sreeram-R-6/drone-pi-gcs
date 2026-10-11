from flask import Flask, request, jsonify, render_template_string
from datetime import datetime

app = Flask(__name__)

latest_data = "No QR data received yet"
received_time = ""


HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Rescue Drone QR Server</title>
    <meta http-equiv="refresh" content="2">
    <style>
        body {
            font-family: Arial, sans-serif;
            background: #111;
            color: white;
            text-align: center;
            padding-top: 80px;
        }

        h1 {
            font-size: 36px;
        }

        .data {
            display: inline-block;
            margin-top: 30px;
            padding: 30px 60px;
            border-radius: 15px;
            background: #222;
            font-size: 64px;
            font-weight: bold;
        }

        .time {
            margin-top: 20px;
            color: #aaa;
            font-size: 18px;
        }
    </style>
</head>

<body>
    <h1>RESCUE DRONE</h1>
    <h2>Latest QR Data</h2>

    <div class="data">
        {{ data }}
    </div>

    <div class="time">
        {{ time }}
    </div>
</body>
</html>
"""


@app.get("/")
def home():
    return render_template_string(
        HTML,
        data=latest_data,
        time=received_time
    )


@app.post("/qr")
def receive_qr():
    global latest_data, received_time

    data = request.get_json(silent=True)

    if not data or "data" not in data:
        return jsonify({
            "status": "error",
            "message": "Missing QR data"
        }), 400

    latest_data = str(data["data"])
    received_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"QR RECEIVED: {latest_data}", flush=True)

    return jsonify({
        "status": "received",
        "data": latest_data
    }), 200


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
