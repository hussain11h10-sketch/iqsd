import hashlib
import json
import time
import os
import sqlite3
import requests
from flask import Flask, request, jsonify

# ============================================================
# IQSD - Iraqi Secure Digital
# شبكة لامركزية P2P مثل Bitcoin
# ============================================================

DB_PATH = 'iqsd.db'

# ثوابت الشبكة
TOTAL_SUPPLY      = 21_000_000
INITIAL_REWARD    = 50
HALVING_INTERVAL  = 210_000
TARGET_TIME       = 120
DIFFICULTY_ADJ    = 100
FEE_RATE          = 0.001
CHAIN_ID          = 19861

app = Flask(__name__)

# قائمة العقد المتصلة
PEERS = set()

# ============================================================
# قاعدة البيانات
# ============================================================

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS blocks (
            height     INTEGER PRIMARY KEY AUTOINCREMENT,
            hash       TEXT UNIQUE NOT NULL,
            prev_hash  TEXT NOT NULL,
            miner      TEXT NOT NULL,
            reward     REAL NOT NULL,
            difficulty INTEGER NOT NULL,
            nonce      INTEGER NOT NULL,
            timestamp  INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wallets (
            address    TEXT PRIMARY KEY,
            balance    REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transactions (
            txid       TEXT PRIMARY KEY,
            sender     TEXT NOT NULL,
            receiver   TEXT NOT NULL,
            amount     REAL NOT NULL,
            fee        REAL NOT NULL,
            height     INTEGER,
            timestamp  INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS mempool (
            txid       TEXT PRIMARY KEY,
            sender     TEXT NOT NULL,
            receiver   TEXT NOT NULL,
            amount     REAL NOT NULL,
            fee        REAL NOT NULL,
            timestamp  INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS peers (
            url        TEXT PRIMARY KEY,
            last_seen  INTEGER
        );
    """)
    db.commit()
    db.close()

# ============================================================
# المحفظة
# ============================================================

def create_wallet(public_key: str) -> dict:
    address = "IQSD" + hashlib.sha256(public_key.encode()).hexdigest()[:36].upper()
    db = get_db()
    exists = db.execute("SELECT * FROM wallets WHERE address=?", (address,)).fetchone()
    if not exists:
        db.execute("INSERT INTO wallets (address, balance) VALUES (?,0)", (address,))
        db.commit()
    balance = db.execute("SELECT balance FROM wallets WHERE address=?", (address,)).fetchone()
    bal = balance["balance"] if balance else 0
    db.close()
    return {"address": address, "balance": bal}

def get_balance(address: str) -> float:
    db = get_db()
    row = db.execute("SELECT balance FROM wallets WHERE address=?", (address,)).fetchone()
    db.close()
    return row["balance"] if row else 0.0

# ============================================================
# التعدين
# ============================================================

def get_difficulty() -> int:
    db = get_db()
    count = db.execute("SELECT COUNT(*) as c FROM blocks").fetchone()["c"]
    if count < DIFFICULTY_ADJ:
        db.close()
        return 4
    rows = db.execute(
        "SELECT timestamp FROM blocks ORDER BY height DESC LIMIT ?", (DIFFICULTY_ADJ,)
    ).fetchall()
    last_diff = db.execute(
        "SELECT difficulty FROM blocks ORDER BY height DESC LIMIT 1"
    ).fetchone()
    db.close()
    current = last_diff["difficulty"] if last_diff else 4
    if len(rows) < 2:
        return current
    avg = (rows[0]["timestamp"] - rows[-1]["timestamp"]) / len(rows)
    if avg < TARGET_TIME * 0.5:
        return min(current + 1, 8)
    elif avg > TARGET_TIME * 2:
        return max(current - 1, 2)
    return current

def get_reward() -> float:
    db = get_db()
    height = db.execute("SELECT COUNT(*) as c FROM blocks").fetchone()["c"]
    db.close()
    halvings = height // HALVING_INTERVAL
    return max(INITIAL_REWARD / (2 ** halvings), 0.00000001)

def get_challenge() -> dict:
    db = get_db()
    last = db.execute("SELECT hash FROM blocks ORDER BY height DESC LIMIT 1").fetchone()
    db.close()
    prev_hash = last["hash"] if last else "0" * 64
    difficulty = get_difficulty()
    return {
        "prev_hash": prev_hash,
        "difficulty": difficulty,
        "target": "0" * difficulty,
        "reward": get_reward(),
        "timestamp": int(time.time())
    }

def submit_block(miner: str, nonce: int, block_hash: str) -> dict:
    db = get_db()
    difficulty = get_difficulty()

    # تحقق من الهاش
    if not block_hash.startswith("0" * difficulty):
        db.close()
        return {"success": False, "error": "هاش غير صحيح"}

    # تحقق من المحفظة
    wallet = db.execute("SELECT address FROM wallets WHERE address=?", (miner,)).fetchone()
    if not wallet:
        db.close()
        return {"success": False, "error": "محفظة غير موجودة"}

    last = db.execute("SELECT hash FROM blocks ORDER BY height DESC LIMIT 1").fetchone()
    prev_hash = last["hash"] if last else "0" * 64
    reward = get_reward()

    # معالجة الـ mempool
    pending = db.execute("SELECT * FROM mempool").fetchall()
    total_fees = sum(tx["fee"] for tx in pending)

    for tx in pending:
        db.execute("""INSERT OR IGNORE INTO transactions
            (txid,sender,receiver,amount,fee,height)
            VALUES (?,?,?,?,?,(SELECT COUNT(*)+1 FROM blocks))
        """, (tx["txid"], tx["sender"], tx["receiver"], tx["amount"], tx["fee"]))
        db.execute("UPDATE wallets SET balance=balance-? WHERE address=?",
                   (tx["amount"] + tx["fee"], tx["sender"]))
        db.execute("INSERT OR IGNORE INTO wallets (address) VALUES (?)", (tx["receiver"],))
        db.execute("UPDATE wallets SET balance=balance+? WHERE address=?",
                   (tx["amount"], tx["receiver"]))

    db.execute("DELETE FROM mempool")

    total = reward + total_fees
    db.execute("""INSERT INTO blocks (hash,prev_hash,miner,reward,difficulty,nonce,timestamp)
        VALUES (?,?,?,?,?,?,?)
    """, (block_hash, prev_hash, miner, total, difficulty, nonce, int(time.time())))
    db.execute("UPDATE wallets SET balance=balance+? WHERE address=?", (total, miner))
    db.commit()
    db.close()

    # أبلغ العقد الأخرى
    broadcast_block(block_hash, miner, nonce)

    return {"success": True, "reward": total, "message": f"ربحت {total:.4f} IQSD!"}

# ============================================================
# التحويل
# ============================================================

def transfer(sender: str, receiver: str, amount: float) -> dict:
    if amount <= 0:
        return {"success": False, "error": "المبلغ يجب أن يكون أكبر من صفر"}
    fee = round(amount * FEE_RATE, 8)
    total = amount + fee
    db = get_db()
    wallet = db.execute("SELECT balance FROM wallets WHERE address=?", (sender,)).fetchone()
    if not wallet or wallet["balance"] < total:
        db.close()
        return {"success": False, "error": f"رصيد غير كافٍ. تحتاج {total:.4f} IQSD"}
    txid = hashlib.sha256(f"{sender}{receiver}{amount}{time.time()}".encode()).hexdigest()
    db.execute("INSERT INTO mempool (txid,sender,receiver,amount,fee) VALUES (?,?,?,?,?)",
               (txid, sender, receiver, amount, fee))
    db.commit()
    db.close()
    return {"success": True, "txid": txid, "fee": fee, "message": "المعاملة في الانتظار"}

# ============================================================
# الشبكة P2P
# ============================================================

def register_peer(url: str):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO peers (url,last_seen) VALUES (?,?)",
               (url, int(time.time())))
    db.commit()
    db.close()
    PEERS.add(url)

def get_peers() -> list:
    db = get_db()
    rows = db.execute("SELECT url FROM peers").fetchall()
    db.close()
    return [r["url"] for r in rows]

def broadcast_block(block_hash: str, miner: str, nonce: int):
    for peer in get_peers():
        try:
            requests.post(f"{peer}/api/peer/block", json={
                "hash": block_hash,
                "miner": miner,
                "nonce": nonce
            }, timeout=3)
        except:
            pass

# ============================================================
# الإحصائيات
# ============================================================

def get_stats() -> dict:
    db = get_db()
    blocks = db.execute("SELECT COUNT(*) as c FROM blocks").fetchone()["c"]
    wallets = db.execute("SELECT COUNT(*) as c FROM wallets").fetchone()["c"]
    mined = db.execute("SELECT SUM(balance) as s FROM wallets").fetchone()["s"] or 0
    pending = db.execute("SELECT COUNT(*) as c FROM mempool").fetchone()["c"]
    db.close()
    return {
        "blocks": blocks,
        "wallets": wallets,
        "mined": round(mined, 4),
        "remaining": round(TOTAL_SUPPLY - mined, 4),
        "pending_tx": pending,
        "difficulty": get_difficulty(),
        "reward": get_reward(),
        "peers": len(get_peers()),
        "chain_id": CHAIN_ID
    }

# ============================================================
# API Routes
# ============================================================

@app.route('/api/wallet/create', methods=['POST'])
def api_create_wallet():
    data = request.get_json() or {}
    pk = data.get('public_key', '')
    if not pk:
        return jsonify({"success": False, "error": "public_key مطلوب"})
    return jsonify({"success": True, **create_wallet(pk)})

@app.route('/api/wallet/<address>')
def api_wallet(address):
    return jsonify({"success": True, "address": address, "balance": get_balance(address)})

@app.route('/api/mining/challenge')
def api_challenge():
    return jsonify({"success": True, **get_challenge()})

@app.route('/api/mining/submit', methods=['POST'])
def api_submit():
    data = request.get_json() or {}
    return jsonify(submit_block(
        data.get('miner', ''),
        data.get('nonce', 0),
        data.get('hash', '')
    ))

@app.route('/api/transfer', methods=['POST'])
def api_transfer():
    data = request.get_json() or {}
    return jsonify(transfer(
        data.get('sender', ''),
        data.get('receiver', ''),
        float(data.get('amount', 0))
    ))

@app.route('/api/stats')
def api_stats():
    return jsonify({"success": True, **get_stats()})

# ============================================================
# P2P Routes
# ============================================================

@app.route('/api/peer/register', methods=['POST'])
def api_register_peer():
    data = request.get_json() or {}
    url = data.get('url', '')
    if url:
        register_peer(url)
    return jsonify({"success": True, "peers": get_peers()})

@app.route('/api/peer/peers')
def api_get_peers():
    return jsonify({"success": True, "peers": get_peers()})

@app.route('/api/peer/block', methods=['POST'])
def api_receive_block():
    # عقدة ثانية أرسلت بلوك جديد
    data = request.get_json() or {}
    return jsonify({"success": True, "message": "بلوك مستلم"})

@app.route('/api/network')
def api_network():
    return jsonify({
        "name": "IQSD Network",
        "chain_id": CHAIN_ID,
        "chain_id_hex": hex(CHAIN_ID),
        "symbol": "IQSD",
        "decimals": 18,
        "rpc_url": request.host_url + "rpc",
        "peers": get_peers()
    })

# EVM RPC للـ Metamask
@app.route('/rpc', methods=['POST'])
def rpc():
    data = request.get_json()
    method = data.get('method')
    req_id = data.get('id', 1)

    def result(res):
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": res})

    if method == 'eth_chainId':
        return result(hex(CHAIN_ID))
    elif method == 'net_version':
        return result(str(CHAIN_ID))
    elif method == 'eth_blockNumber':
        db = get_db()
        count = db.execute("SELECT COUNT(*) as c FROM blocks").fetchone()["c"]
        db.close()
        return result(hex(count))
    elif method == 'eth_getBalance':
        params = data.get('params', [])
        address = params[0] if params else None
        balance = get_balance(address) if address else 0
        return result(hex(int(balance * (10 ** 18))))
    elif method == 'eth_gasPrice':
        return result(hex(1000000000))
    elif method == 'eth_estimateGas':
        return result(hex(21000))
    elif method == 'net_listening':
        return result(True)
    elif method == 'eth_syncing':
        return result(False)
    else:
        return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})

@app.route('/')
def index():
    try:
        return app.send_static_file('index.html')
    except:
        stats = get_stats()
        return jsonify({"name": "IQSD Network", "status": "running", **stats})

# ============================================================
# تشغيل
# ============================================================

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    print(f"IQSD Network running on port {port}")
    print(f"Chain ID: {CHAIN_ID}")
    app.run(host='0.0.0.0', port=port, debug=False)
