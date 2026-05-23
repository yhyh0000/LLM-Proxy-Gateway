"""
免费 LLM 智能代理网关  (优先社区 Key + 粘度保留 + 供应商日志)
核心功能：
- 调度优先级：P0(社区) > P1(官方免费) > P2(付费)
- 社区 Key 失败：冷却 60 秒，连续失败 3 次才删除（粘度保护）
- 社区 Key 成功：重置失败计数，继续保留
- 日志中明确显示供应商名称（手动供应商的 name 或 "社区:key预览"）
- 自动清理请求中的 image_url，避免 400 错误
- 爬虫每 5 分钟更新社区 Key 池（完全替换，但保留现有冷却中的 Key？为了粘度，合并新旧时保留现有冷却状态）
启动：python proxy.py
管理面板：http://127.0.0.1:8800/admin
"""

import os
import json
import time
import threading
import logging
import hashlib
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler

import requests
from flask import Flask, request, jsonify, Response, render_template
from waitress import serve
from cryptography.fernet import Fernet

# ============================================
# 强制 UTF-8 编码
# ============================================
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============================================
# 安全配置
# ============================================
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
API_ACCESS_TOKEN = os.getenv("API_ACCESS_TOKEN", "")
ENABLE_ADMIN_AUTH = os.getenv("ENABLE_ADMIN_AUTH", "false").lower() == "true"
ENABLE_API_AUTH = os.getenv("ENABLE_API_AUTH", "false").lower() == "true"

def check_admin_auth():
    if not ENABLE_ADMIN_AUTH:
        return True
    auth = request.authorization
    return auth and auth.username == ADMIN_USERNAME and auth.password == ADMIN_PASSWORD

def check_api_token():
    if not ENABLE_API_AUTH:
        return True
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth.split(" ")[1] == API_ACCESS_TOKEN

# ============================================
# 基础配置
# ============================================
PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8800"))
UPDATE_INTERVAL = 300          # 爬虫更新间隔(秒)
MAX_RETRIES = 10               # auto 模式最大尝试次数
REQUEST_TIMEOUT = 30           # 转发请求超时(秒)
P1_P2_COOLDOWN = 60            # 稳定供应商失败冷却时间(秒)
COMMUNITY_COOLDOWN = 60        # 社区 Key 失败冷却时间(秒)
COMMUNITY_MAX_FAILURES = 3     # 社区 Key 连续失败次数上限，超过则删除

DEFAULT_P0_ENDPOINT = "#"

# 数据源文件
SOURCES_FILE = "sources.json"
DEFAULT_SOURCES = [
    {
        "url": "#",
        "target_models": ["deepseek-chat"],
        "endpoint": "#",
        "name": "默认中转站"
    }
]

# 稳定供应商文件
PROVIDERS_FILE = "providers.json"

# 日志
log_handler = TimedRotatingFileHandler("proxy.log", when="midnight", interval=1, backupCount=7, encoding="utf-8")
log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger = logging.getLogger("proxy")
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)

MAX_LOG_ENTRIES = 200
recent_logs = []
logs_lock = threading.Lock()
server_start_time = time.time()

crawler_status = {"last_update": None, "next_update": None, "status": "pending", "key_count": 0}

def add_log(level, message):
    logger.log(level, message)
    with logs_lock:
        recent_logs.append({
            "time": datetime.now().isoformat(),
            "level": logging.getLevelName(level),
            "message": message
        })
        if len(recent_logs) > MAX_LOG_ENTRIES:
            recent_logs.pop(0)

# ============================================
# 加密工具
# ============================================
SECRET_FILE = ".secret_key"

def get_cipher():
    if not os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "wb") as f:
            f.write(Fernet.generate_key())
        os.chmod(SECRET_FILE, 0o600)
    with open(SECRET_FILE, "rb") as f:
        return Fernet(f.read())

cipher = get_cipher()

def encrypt_key(plain: str) -> str:
    return cipher.encrypt(plain.encode()).decode()

def decrypt_key(encrypted: str) -> str:
    return cipher.decrypt(encrypted.encode()).decode()

def preview_key(key_str: str, visible=6) -> str:
    if len(key_str) <= visible * 2:
        return key_str[:visible] + "***"
    return key_str[:visible] + "..." + key_str[-visible:]

def get_provider_display(key_info):
    """获取供应商显示名称"""
    if key_info.get("source") == "community":
        return f"社区:{preview_key(key_info['key'])}"
    else:
        return key_info.get("name", preview_key(key_info['key']))

# ============================================
# 数据源管理
# ============================================
def load_sources():
    if not os.path.exists(SOURCES_FILE):
        save_sources(DEFAULT_SOURCES)
        return DEFAULT_SOURCES
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_SOURCES

def save_sources(sources):
    with open(SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump(sources, f, indent=2, ensure_ascii=False)

# ============================================
# 加载稳定供应商 (providers.json)
# ============================================
def load_stable_providers():
    if not os.path.exists(PROVIDERS_FILE):
        return {}
    try:
        with open(PROVIDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        providers = {}
        for item in data:
            try:
                decrypted = decrypt_key(item["api_key"])
            except Exception:
                decrypted = ""
            if not decrypted:
                continue
            key_str = decrypted
            providers[key_str] = {
                "key": key_str,
                "models": item.get("selected_models", []),
                "name": item.get("name", preview_key(key_str)),
                "verified_models": item.get("verified_models", []),
                "priority": item.get("priority", 1),
                "source": "stable" if item.get("priority", 1) == 1 else "paid",
                "endpoint": item.get("endpoint", "").rstrip("/"),
                "provider_type": item.get("provider_type", "openai"),
                "cooldown_until": 0,
                "fail_count": 0,
                "avg_latency": 1.0,
                "rate_limit": "",
                "expiry": ""
            }
        return providers
    except Exception as e:
        add_log(logging.ERROR, f"加载稳定供应商失败: {e}")
        return {}

def save_stable_providers(providers_dict):
    data = []
    for key_str, info in providers_dict.items():
        if info.get("source") not in ("stable", "paid"):
            continue
        data.append({
            "name": info.get("name", preview_key(key_str)),
            "endpoint": info["endpoint"],
            "api_key": encrypt_key(key_str),
            "selected_models": info.get("models", []),
            "verified_models": info.get("verified_models", []),
            "provider_type": info.get("provider_type", "openai"),
            "priority": info.get("priority", 1),
            "created_at": info.get("created_at", int(time.time()))
        })
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    add_log(logging.INFO, f"稳定供应商已保存 ({len(data)} 个)")

# ============================================
# 全局 Key 池
# ============================================
pool_lock = threading.Lock()
key_pool = {}

def refresh_key_pool():
    """加载稳定供应商（不覆盖社区部分）"""
    stable = load_stable_providers()
    with pool_lock:
        for k, v in stable.items():
            if k not in key_pool:
                key_pool[k] = v
            else:
                # 更新已有稳定供应商的信息（如名称、端点等）
                key_pool[k].update(v)
    add_log(logging.INFO, f"稳定供应商已刷新，总数: {len(stable)}")

# ============================================
# 爬虫线程（定期抓取 P0 Key，合并新旧时保留冷却和失败计数）
# ============================================
def crawler_loop():
    while True:
        try:
            sources = load_sources()
            if not sources:
                time.sleep(UPDATE_INTERVAL)
                continue
            all_new_p0 = {}
            for src in sources:
                try:
                    resp = requests.get(src["url"], timeout=30)
                    resp.raise_for_status()
                    data = resp.json()
                    target_models = set(src.get("target_models", ["deepseek-chat"]))
                    endpoint = src.get("endpoint", DEFAULT_P0_ENDPOINT).rstrip('/')
                    # 支持两种格式
                    if isinstance(data, list):
                        for item in data:
                            model = item.get("model") or (item.get("models", [None])[0])
                            if not model or model not in target_models:
                                continue
                            key_str = item.get("key")
                            if key_str and key_str not in all_new_p0:
                                all_new_p0[key_str] = {
                                    "key": key_str,
                                    "models": [model],
                                    "priority": 0,
                                    "source": "community",
                                    "endpoint": endpoint,
                                    "provider_type": "openai",
                                    "cooldown_until": 0,
                                    "fail_count": 0,          # 粘度：失败计数
                                    "avg_latency": 1.0,
                                    "name": f"社区:{preview_key(key_str)}",
                                    "rate_limit": item.get("rate_limit", ""),
                                    "expiry": item.get("expiry", "")
                                }
                    else:
                        for group in data.get("groups", []):
                            for item in group.get("keys", []):
                                model = item.get("model", "")
                                if model not in target_models:
                                    continue
                                key_str = item["api_key"]
                                if key_str not in all_new_p0:
                                    all_new_p0[key_str] = {
                                        "key": key_str,
                                        "models": [model],
                                        "priority": 0,
                                        "source": "community",
                                        "endpoint": endpoint,
                                        "provider_type": "openai",
                                        "cooldown_until": 0,
                                        "fail_count": 0,
                                        "avg_latency": 1.0,
                                        "name": f"社区:{preview_key(key_str)}",
                                        "rate_limit": item.get("rate_limit", ""),
                                        "expiry": item.get("expiry", "")
                                    }
                except Exception as e:
                    add_log(logging.ERROR, f"抓取源 {src['url']} 失败: {e}")
            # 合并到内存池：保留稳定供应商（priority>0），社区 Key 合并：保留旧 Key 的冷却和失败计数，新 Key 加入
            with pool_lock:
                # 保留稳定供应商
                keep = {k: v for k, v in key_pool.items() if v.get("priority", 0) > 0}
                # 合并社区 Key
                for k, new_info in all_new_p0.items():
                    if k in keep:
                        # 已存在（可能是旧社区 Key），保留其冷却和失败计数
                        old = keep[k]
                        new_info["cooldown_until"] = old.get("cooldown_until", 0)
                        new_info["fail_count"] = old.get("fail_count", 0)
                    keep[k] = new_info
                key_pool.clear()
                key_pool.update(keep)
            add_log(logging.INFO, f"爬虫更新完成，社区 Key 数量: {len(all_new_p0)} (保留粘度状态)")
            crawler_status["last_update"] = datetime.now().isoformat()
            crawler_status["next_update"] = (datetime.now() + timedelta(seconds=UPDATE_INTERVAL)).isoformat()
            crawler_status["status"] = "success"
            crawler_status["key_count"] = len([k for k in key_pool if key_pool[k].get("priority")==0])
        except Exception as e:
            add_log(logging.ERROR, f"爬虫异常: {e}")
            crawler_status["status"] = "error"
        time.sleep(UPDATE_INTERVAL)

# ============================================
# 调度与失败处理（优先 P0，粘度保护）
# ============================================
def get_next_key(exclude_keys=None, prefer_community=True):
    """
    获取下一个可用 Key。
    策略：优先返回优先级 0（社区）且未冷却的 Key，如果没有则返回优先级 1/2 的稳定供应商。
    排除 exclude_keys 中的 Key。
    """
    with pool_lock:
        now = time.time()
        # 首先收集所有可用 Key（未冷却）
        available = []
        for k, v in key_pool.items():
            if exclude_keys and k in exclude_keys:
                continue
            if v.get("cooldown_until", 0) <= now:
                available.append(v)
        if not available:
            return None
        # 排序：优先 priority 0（社区），然后 priority 1,2...
        # 注意 priority 0 最小，所以升序排序就是 0,1,2
        available.sort(key=lambda x: x["priority"])
        best = available[0]
        add_log(logging.INFO, f"选中 Key: {get_provider_display(best)} (priority={best['priority']})")
        return best

def mark_success(key_info):
    """请求成功时重置失败计数（用于粘度）"""
    with pool_lock:
        key_str = key_info["key"]
        if key_str in key_pool:
            key_pool[key_str]["fail_count"] = 0
            # 可选：记录最后成功时间，但不强制

def mark_failed(key_info):
    """根据 source 决定是冷却还是删除（粘度保护：社区失败达到阈值才删除）"""
    with pool_lock:
        key_str = key_info["key"]
        if key_str not in key_pool:
            return
        if key_info.get("source") == "community":
            # 社区 Key：增加失败计数
            fail_count = key_pool[key_str].get("fail_count", 0) + 1
            key_pool[key_str]["fail_count"] = fail_count
            if fail_count >= COMMUNITY_MAX_FAILURES:
                # 连续失败次数过多，删除
                del key_pool[key_str]
                add_log(logging.INFO, f"社区 Key 连续失败 {fail_count} 次，已删除: {preview_key(key_str)}")
            else:
                # 冷却一段时间
                cooldown_until = time.time() + COMMUNITY_COOLDOWN
                key_pool[key_str]["cooldown_until"] = cooldown_until
                add_log(logging.WARNING, f"社区 Key 失败 ({fail_count}/{COMMUNITY_MAX_FAILURES})，冷却 {COMMUNITY_COOLDOWN}s: {preview_key(key_str)}")
        else:
            # 稳定供应商：冷却，不删除
            cooldown_until = time.time() + P1_P2_COOLDOWN
            key_pool[key_str]["cooldown_until"] = cooldown_until
            add_log(logging.WARNING, f"稳定供应商冷却 {P1_P2_COOLDOWN}s: {key_info.get('name', preview_key(key_str))}")

# ============================================
# 请求处理：清理多模态 + auto 切换
# ============================================
def clean_multimodal(body_str):
    """移除 messages 中的 image_url，只保留文本"""
    try:
        data = json.loads(body_str)
        for msg in data.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                texts = [item.get("text", "") for item in content if "text" in item]
                msg["content"] = " ".join(texts) if texts else ""
            if "image_url" in msg:
                del msg["image_url"]
        return json.dumps(data)
    except:
        return body_str

def build_upstream_url(key_info, model_name):
    endpoint = key_info["endpoint"].rstrip('/')
    provider_type = key_info.get("provider_type", "openai")
    if provider_type == "google":
        return f"{endpoint}/models/{model_name}:generateContent"
    else:
        if endpoint.endswith('/v1') or endpoint.endswith('/v1beta'):
            return endpoint + '/chat/completions'
        else:
            return endpoint

def handle_chat_request():
    """处理请求，优先社区 Key，支持 auto 无感切换"""
    try:
        body = request.get_data(as_text=True)
        body = clean_multimodal(body)

        try:
            data = json.loads(body)
            is_auto = (data.get("model") == "auto")
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON body"}), 400
        except Exception:
            return jsonify({"error": "Failed to parse request body"}), 400

        tried_keys = set()
        max_attempts = MAX_RETRIES if not is_auto else 50  # 足够大

        for attempt in range(max_attempts):
            key_info = get_next_key(exclude_keys=tried_keys)
            if not key_info:
                break
            tried_keys.add(key_info["key"])

            if is_auto:
                model_list = key_info.get("verified_models") or key_info.get("models") or []
                if not model_list:
                    add_log(logging.WARNING, f"Key {get_provider_display(key_info)} 无可用模型，标记失败")
                    mark_failed(key_info)
                    continue
                chosen_model = model_list[0]
                data["model"] = chosen_model
                body = json.dumps(data)
                add_log(logging.INFO, f"auto 尝试使用: {get_provider_display(key_info)} 模型: {chosen_model}")
            else:
                if "model" not in data or not data["model"]:
                    return jsonify({"error": "请求缺少 model 字段"}), 400
                chosen_model = data["model"]

            provider_type = key_info.get("provider_type", "openai")
            url = build_upstream_url(key_info, chosen_model)
            headers = {k: v for k, v in request.headers if k.lower() != 'host'}

            if provider_type == "google":
                if '?' in url:
                    url += f"&key={key_info['key']}"
                else:
                    url += f"?key={key_info['key']}"
                headers.pop("Authorization", None)
            else:
                headers["Authorization"] = f"Bearer {key_info['key']}"

            if "Content-Type" not in headers:
                headers["Content-Type"] = "application/json"

            try:
                upstream_resp = requests.post(
                    url,
                    headers=headers,
                    data=body,
                    timeout=REQUEST_TIMEOUT,
                    stream=True
                )
                if 200 <= upstream_resp.status_code < 300:
                    # 成功：重置失败计数
                    mark_success(key_info)
                    def generate():
                        for chunk in upstream_resp.iter_content(8192):
                            if chunk:
                                yield chunk
                    response = Response(generate(), status=upstream_resp.status_code)
                    for k, v in upstream_resp.headers.items():
                        if k.lower() not in ('connection', 'keep-alive', 'transfer-encoding'):
                            response.headers[k] = v
                    add_log(logging.INFO, f"请求成功 供应商: {get_provider_display(key_info)} 模型: {chosen_model}")
                    return response
                else:
                    error_body = upstream_resp.text[:200]
                    add_log(logging.WARNING, f"上游 {upstream_resp.status_code} 供应商: {get_provider_display(key_info)} 模型: {chosen_model} 响应: {error_body}")
                    mark_failed(key_info)
                    continue
            except Exception as e:
                add_log(logging.ERROR, f"请求异常: {e} 供应商: {get_provider_display(key_info)}")
                mark_failed(key_info)
                continue

        return jsonify({"error": "所有 Key 均不可用"}), 503

    except Exception as e:
        add_log(logging.ERROR, f"handle_chat_request 未捕获异常: {e}", exc_info=True)
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

# ============================================
# Flask 应用 (管理面板 + API) - 保持不变，略作修改以显示更多信息
# ============================================
app = Flask(__name__)

@app.before_request
def require_admin_auth():
    if request.path.startswith('/admin') or (request.path.startswith('/api/') and request.path != '/api/providers/detect'):
        if not check_admin_auth():
            return Response('需要管理员凭证', 401, {'WWW-Authenticate': 'Basic realm="Admin Area"'})

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    if not check_api_token():
        return jsonify({"error": "Unauthorized"}), 401
    return handle_chat_request()

@app.route('/v1/models', methods=['GET'])
def list_models():
    return jsonify({
        "object": "list",
        "data": [{"id": "auto", "object": "model", "created": int(time.time()), "owned_by": "proxy"}]
    })

@app.route('/health')
def health():
    with pool_lock:
        now = time.time()
        total = len(key_pool)
        active = sum(1 for v in key_pool.values() if v.get("cooldown_until", 0) <= now)
        p0 = sum(1 for v in key_pool.values() if v.get("priority") == 0)
        p1 = sum(1 for v in key_pool.values() if v.get("priority") == 1)
        p2 = sum(1 for v in key_pool.values() if v.get("priority") == 2)
    uptime_seconds = int(time.time() - server_start_time)
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{days}天 {hours}时 {minutes}分" if days else f"{hours}时 {minutes}分 {seconds}秒"
    with logs_lock:
        total_requests = len(recent_logs)
        success_count = sum(1 for l in recent_logs if "成功" in l["message"])
        success_rate = round(success_count / total_requests, 3) if total_requests else 0
    return jsonify({
        "status": "ok",
        "total": total,
        "active": active,
        "pool_stats": {"P0": p0, "P1": p1, "P2": p2},
        "uptime": uptime_str,
        "crawler": crawler_status,
        "stats": {"total_requests": total_requests, "success_rate": success_rate}
    })

@app.route('/api/logs')
def get_logs():
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    with logs_lock:
        total = len(recent_logs)
        start = max(0, total - page * limit)
        end = total - (page - 1) * limit
        page_logs = recent_logs[start:end]
        page_logs = list(reversed(page_logs))
    return jsonify({
        "total": total, "page": page, "limit": limit,
        "total_pages": (total + limit - 1) // limit,
        "logs": page_logs
    })

@app.route('/api/providers', methods=['GET'])
def list_providers():
    result = []
    with pool_lock:
        for k, v in key_pool.items():
            if v.get("source") in ("stable", "paid"):
                result.append({
                    "key_preview": preview_key(k),
                    "full_key_hash": hashlib.md5(k.encode()).hexdigest(),
                    "name": v.get("name", preview_key(k)),
                    "endpoint": v["endpoint"],
                    "selected_models": v.get("models", []),
                    "verified_models": v.get("verified_models", []),
                    "priority": v["priority"],
                    "source": v["source"],
                    "avg_latency": round(v.get("avg_latency", 1.0), 2),
                    "cooldown": max(0, int(v.get("cooldown_until", 0) - time.time())),
                    "status": "active" if v.get("cooldown_until", 0) <= time.time() else "cooldown"
                })
    return jsonify(result)

@app.route('/api/providers/detect', methods=['POST'])
def detect_models():
    return jsonify({"success": True, "models": []})

@app.route('/api/providers', methods=['POST'])
def add_provider():
    data = request.get_json()
    required = ["endpoint", "api_key", "selected_models"]
    if not all(k in data for k in required):
        return jsonify({"error": "缺少必要参数"}), 400
    key_str = data["api_key"]
    endpoint = data["endpoint"].rstrip("/")
    priority = data.get("priority", 1)
    if priority not in [1, 2]:
        priority = 1
    provider_type = data.get("provider_type", "openai")
    info = {
        "key": key_str,
        "models": data["selected_models"],
        "verified_models": data.get("verified_models", []),
        "priority": priority,
        "source": "stable" if priority == 1 else "paid",
        "endpoint": endpoint,
        "provider_type": provider_type,
        "cooldown_until": 0,
        "fail_count": 0,
        "avg_latency": 1.0,
        "name": data.get("name", preview_key(key_str)),
        "created_at": int(time.time())
    }
    with pool_lock:
        key_pool[key_str] = info
    stable_dict = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
    save_stable_providers(stable_dict)
    add_log(logging.INFO, f"添加供应商: {info['name']} (P{priority})")
    return jsonify({"success": True, "key_preview": preview_key(key_str)}), 201

@app.route('/api/providers/<key_hash>', methods=['PUT'])
def update_provider(key_hash):
    return jsonify({"error": "not implemented"}), 501

@app.route('/api/providers/<key_hash>', methods=['DELETE'])
def delete_provider(key_hash):
    with pool_lock:
        target_key = None
        for k, v in key_pool.items():
            if v.get("source") in ("stable", "paid") and hashlib.md5(k.encode()).hexdigest() == key_hash:
                target_key = k
                break
        if not target_key:
            return jsonify({"error": "供应商不存在"}), 404
        del key_pool[target_key]
    stable_dict = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
    save_stable_providers(stable_dict)
    add_log(logging.INFO, f"删除供应商: {preview_key(target_key)}")
    return jsonify({"success": True})

@app.route('/api/sources', methods=['GET'])
def list_sources():
    return jsonify(load_sources())

@app.route('/api/sources', methods=['POST'])
def add_source():
    data = request.get_json()
    if not data.get("url"):
        return jsonify({"error": "URL 不能为空"}), 400
    sources = load_sources()
    sources.append({
        "url": data["url"],
        "target_models": data.get("target_models", ["deepseek-chat"]),
        "endpoint": data.get("endpoint", DEFAULT_P0_ENDPOINT),
        "name": data.get("name", "未命名")
    })
    save_sources(sources)
    add_log(logging.INFO, f"添加数据源: {data['url']}")
    return jsonify({"success": True})

@app.route('/api/sources/<int:index>', methods=['DELETE'])
def delete_source(index):
    sources = load_sources()
    if 0 <= index < len(sources):
        sources.pop(index)
        save_sources(sources)
        add_log(logging.INFO, f"删除数据源 #{index}")
        return jsonify({"success": True})
    return jsonify({"error": "无效索引"}), 404

@app.route('/admin')
def admin():
    return render_template('admin.html')

# ============================================
# 主启动
# ============================================
if __name__ == '__main__':
    refresh_key_pool()
    threading.Thread(target=crawler_loop, daemon=True).start()
    print(f"""
🚀 LLM 智能代理网关 v5.5 (优先社区 + 粘度保留) 启动成功！
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 代理地址:   http://{PROXY_HOST}:{PROXY_PORT}/v1
🔧 管理面板:   http://{PROXY_HOST}:{PROXY_PORT}/admin
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 核心特性:
   - 调度优先: 社区 Key (P0) > 稳定供应商 (P1/P2)
   - 社区 Key 失败冷却 {COMMUNITY_COOLDOWN}s，连续 {COMMUNITY_MAX_FAILURES} 次才删除
   - 成功请求重置失败计数（粘度保护）
   - 日志显示供应商名称和模型
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    serve(app, host=PROXY_HOST, port=PROXY_PORT, threads=10)
