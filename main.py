import os
import jwt
import datetime
from datetime import timezone
from flask import Flask, request, jsonify
from mysql.connector import Error
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

# 새로 만든 모듈 import
from database import get_db_connection
from crypto import encrypt_data, decrypt_data

from bot_manager import bot_manager
from okx_trader import OKXTrader

# --- 환경 변수 및 Flask 앱 설정 ---
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")

# --- 기본 페이지 ---
@app.route("/")
def index():
    """서버가 실행 중인지 확인하는 기본 페이지"""
    return "Python server is running!"

# --- 로그인 API (토큰 발급 기능) ---
@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    uuid = data.get("uuid")

    if not all([email, password, uuid]):
        return jsonify({"message": "Email, password, and uuid are required"}), 400

    conn = get_db_connection()
    if conn is None:
        return jsonify({"message": "Database connection failed"}), 500

    cursor = conn.cursor(dictionary=True)
    query = "SELECT id, password FROM users WHERE email = %s"
    cursor.execute(query, (email,))
    user = cursor.fetchone()

    if user is None or not check_password_hash(user["password"], password):
        if conn.is_connected(): cursor.close(); conn.close()
        return jsonify({"message": "Invalid email or password"}), 401

    user_id = user["id"]
    try:
        # 액세스 토큰 (유효기간 1시간)
        access_token = jwt.encode({
            'user_id': user_id,
            'exp': datetime.datetime.now(timezone.utc) + datetime.timedelta(hours=1)
        }, app.config['SECRET_KEY'], algorithm="HS256")

        # 리프레시 토큰 (유효기간 14일)
        refresh_token_exp = datetime.datetime.now(timezone.utc) + datetime.timedelta(days=14)
        refresh_token = jwt.encode({
            'user_id': user_id,
            'exp': refresh_token_exp
        }, app.config['SECRET_KEY'], algorithm="HS256")

        # 리프레시 토큰을 DB에 저장
        upsert_token_query = """
        INSERT INTO refresh_tokens (user_id, token, uuid, expires_at)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE token = VALUES(token), expires_at = VALUES(expires_at)
        """
        cursor.execute(upsert_token_query, (user_id, refresh_token, uuid, refresh_token_exp.strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()

        return jsonify({
            "message": "Login successful",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_id": user_id
        }), 200

    except Exception as e:
        return jsonify({"message": f"An error occurred: {e}"}), 500
    finally:
        if conn.is_connected(): cursor.close(); conn.close()

# --- 토큰 재발급 API ---
@app.route("/refresh", methods=["POST"])
def refresh():
    data = request.get_json()
    refresh_token = data.get('refresh_token')

    if not refresh_token:
        return jsonify({"message": "Refresh token is required"}), 400

    conn = get_db_connection()
    if conn is None: return jsonify({"message": "DB connection failed"}), 500
    cursor = conn.cursor(dictionary=True)

    try:
        payload = jwt.decode(refresh_token, app.config['SECRET_KEY'], algorithms=["HS256"])
        user_id = payload['user_id']

        query = "SELECT id, user_id FROM refresh_tokens WHERE token = %s AND user_id = %s AND expires_at > NOW()"
        cursor.execute(query, (refresh_token, user_id))
        token_in_db = cursor.fetchone()

        if not token_in_db:
            return jsonify({"message": "Invalid or expired refresh token"}), 401

        new_access_token = jwt.encode({
            'user_id': user_id,
            'exp': datetime.datetime.now(timezone.utc) + datetime.timedelta(hours=1)
        }, app.config['SECRET_KEY'], algorithm="HS256")

        return jsonify({"access_token": new_access_token, "user_id": user_id}), 200

    except jwt.ExpiredSignatureError:
        return jsonify({"message": "Refresh token has expired"}), 401
    except jwt.InvalidTokenError:
        return jsonify({"message": "Invalid refresh token"}), 401
    finally:
        if conn.is_connected(): cursor.close(); conn.close()

# --- API 키 저장 ---
@app.route("/api-keys", methods=["POST"])
def add_api_keys():
    data = request.get_json()
    user_id = data.get("user_id")
    api_key = data.get("api_key")
    api_secret = data.get("api_secret")
    api_passphrase = data.get("api_passphrase")

    if not all([user_id, api_key, api_secret, api_passphrase]):
        return jsonify({"message": "All fields are required"}), 400

    encrypted_api_key = encrypt_data(api_key)
    encrypted_api_secret = encrypt_data(api_secret)
    encrypted_api_passphrase = encrypt_data(api_passphrase)

    conn = get_db_connection()
    if conn is None: return jsonify({"message": "DB connection failed"}), 500
    cursor = conn.cursor()

    try:
        query_check = "SELECT id FROM api_keys WHERE user_id = %s"
        cursor.execute(query_check, (user_id,))
        result = cursor.fetchone()

        if result:
            query = "UPDATE api_keys SET api_key = %s, api_secret = %s, api_passphrase = %s WHERE user_id = %s"
            params = (encrypted_api_key, encrypted_api_secret, encrypted_api_passphrase, user_id)
        else:
            query = "INSERT INTO api_keys (user_id, api_key, api_secret, api_passphrase) VALUES (%s, %s, %s, %s)"
            params = (user_id, encrypted_api_key, encrypted_api_secret, encrypted_api_passphrase)

        cursor.execute(query, params)
        conn.commit()
        return jsonify({"message": "API keys saved successfully"}), 201
    except Error as e:
        return jsonify({"message": f"An error occurred: {e}"}), 500
    finally:
        if conn.is_connected(): cursor.close(); conn.close()

# --- API 키 존재 여부 확인 ---
@app.route("/api-keys/check/<int:user_id>", methods=["GET"])
def check_api_keys(user_id):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"message": "Database connection failed"}), 500

    cursor = conn.cursor()
    try:
        query = "SELECT EXISTS(SELECT 1 FROM api_keys WHERE user_id = %s)"
        cursor.execute(query, (user_id,))
        has_keys = cursor.fetchone()[0]
        return jsonify({"has_api_keys": bool(has_keys)}), 200
    except Error as e:
        return jsonify({"message": f"An error occurred: {e}"}), 500
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


# --- API 키 조회 ---
@app.route("/api-keys/<int:user_id>", methods=["GET"])
def get_api_keys(user_id):
    conn = get_db_connection()
    if conn is None: return jsonify({"message": "DB connection failed"}), 500
    cursor = conn.cursor(dictionary=True)

    try:
        query = "SELECT api_key, api_secret, api_passphrase FROM api_keys WHERE user_id = %s"
        cursor.execute(query, (user_id,))
        encrypted_keys = cursor.fetchone()

        if encrypted_keys:
            decrypted_keys = {
                "api_key": decrypt_data(encrypted_keys["api_key"]),
                "api_secret": decrypt_data(encrypted_keys["api_secret"]),
                "api_passphrase": decrypt_data(encrypted_keys["api_passphrase"])
            }
            return jsonify(decrypted_keys), 200
        else:
            return jsonify({"message": "API keys not found"}), 404

    except Error as e:
        return jsonify({"message": f"An error occurred: {e}"}), 500
    finally:
        if conn.is_connected(): cursor.close(); conn.close()

# --- 봇 제어 API ---
@app.route("/bot/start", methods=["POST"])
def start_bot():
    data = request.get_json()
    user_id = data.get("user_id")
    ticker = data.get("ticker")
    leverage = int(data.get("leverage")) # leverage 처리 추가
    amount = float(data.get("amount"))

    if not all([user_id, ticker, leverage, amount]): # leverage 확인 추가
        return jsonify({"message": "All fields are required"}), 400

    # leverage 인수 추가
    result = bot_manager.start_bot(user_id, ticker, leverage, amount)

    if result["status"] == "success":
        return jsonify({"message": result["message"]}), 200
    else:
        return jsonify({"message": result["message"]}), 500


@app.route("/bot/stop", methods=["POST"])
def stop_bot():
    data = request.get_json()
    user_id = data.get("user_id")
    ticker = data.get("ticker") # 추가

    # 변경: user_id와 ticker를 모두 확인
    if not all([user_id, ticker]):
        return jsonify({"message": "user_id and ticker are required"}), 400


    if not user_id:
        return jsonify({"message": "user_id is required"}), 400

    # 봇 매니저에게 봇 중지를 위임
    result = bot_manager.stop_bot(user_id, ticker)

    if result["status"] == "success":
        return jsonify({"message": result["message"]}), 200
    else:
        return jsonify({"message": result["message"]}), 404

@app.route("/bot/status/<int:user_id>", methods=["GET"])
async def get_bot_status(user_id):
    # 1. 이 사용자의 실행중인 봇 티커 목록을 가져옵니다.
    active_tickers = []
    for uid, ticker in bot_manager.active_bots.keys():
        if uid == user_id:
            active_tickers.append(ticker)
    if not active_tickers:
        return jsonify([]), 200 # 실행중인 봇이 없으면 빈 리스트 반환


    # 2. API 키를 DB에서 가져옵니다.
    conn = get_db_connection()
    if conn is None: return jsonify({"message": "DB connection failed"}), 500
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM api_keys WHERE user_id = %s", (user_id,))
    keys = cursor.fetchone()
    conn.close()

    if not keys:
        return jsonify({"message": "API keys not found for status check"}), 404

    api_key = decrypt_data(keys['api_key'])
    api_secret = decrypt_data(keys['api_secret'])
    api_passphrase = decrypt_data(keys['api_passphrase'])

    # 3. 각 티커별로 포지션 정보를 조회합니다.
    bot_statuses = []
    # 정보를 조회하기 위한 임시 거래 객체를 생성합니다.
    temp_trader = await OKXTrader.create(api_key, api_secret, api_passphrase, active_tickers[0])
    if not temp_trader:
        return jsonify({"message": "Failed to create a temporary trader for status check"}), 500

    for ticker in active_tickers:
        position = await temp_trader.get_position(ticker)
        if position:
            unrealized_pnl = float(position.get('unrealizedPnl', 0))
            realized_pnl = float(position['info'].get('realizedPnl', 0))
            total_pnl = unrealized_pnl + realized_pnl # 총 손익 계산

            status = {
                "ticker": ticker,
                "total_pnl": total_pnl, # 'total_pnl' 이라는 이름으로 전송
                "margin": float(position.get('initialMargin', 0))
            }
            bot_statuses.append(status)

    # 4. 임시 거래 객체의 연결을 반드시 닫아줍니다.
    await temp_trader.close_connection()

    return jsonify(bot_statuses), 200


# --- 서버 실행 ---
if __name__ == "__main__":
    app.run(host='0.0.0.0', debug=False, port=5000)