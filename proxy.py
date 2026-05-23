"""
免费 LLM 智能代理网关 v5.9 (完整修复版)
- 修复供应商编辑时 info 未定义错误
- 修复模型探测返回值解包错误
- 优化 Cloudflare Workers AI 支持（使用 prompt 格式，自动补全 /run）
- 所有供应商类型均支持
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
UPDATE_INTERVAL = 300
MAX_RETRIES = 10
REQUEST_TIMEOUT = 30
P1_P2_COOLDOWN = 60
COMMUNITY_COOLDOWN = 60
COMMUNITY_MAX_FAILURES = 3

DEFAULT_P0_ENDPOINT = "#"

SOURCES_FILE = "sources.json"
DEFAULT_SOURCES = [
    {
        "url": "#",
        "target_models": ["deepseek-chat"],
        "endpoint": DEFAULT_P0_ENDPOINT,
        "name": "默认中转站"
    }
]

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
# 稳定供应商加载/保存
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
    stable = load_stable_providers()
    with pool_lock:
        for k, v in stable.items():
            if k not in key_pool:
                key_pool[k] = v
            else:
                key_pool[k].update(v)
    add_log(logging.INFO, f"稳定供应商已刷新，总数: {len(stable)}")

# ============================================
# 爬虫线程
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
                                    "fail_count": 0,
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
            with pool_lock:
                keep = {k: v for k, v in key_pool.items() if v.get("priority", 0) > 0}
                for k, new_info in all_new_p0.items():
                    if k in keep:
                        old = keep[k]
                        new_info["cooldown_until"] = old.get("cooldown_until", 0)
                        new_info["fail_count"] = old.get("fail_count", 0)
                    keep[k] = new_info
                key_pool.clear()
                key_pool.update(keep)
            add_log(logging.INFO, f"爬虫更新完成，社区 Key 数量: {len(all_new_p0)}")
            crawler_status["last_update"] = datetime.now().isoformat()
            crawler_status["next_update"] = (datetime.now() + timedelta(seconds=UPDATE_INTERVAL)).isoformat()
            crawler_status["status"] = "success"
            crawler_status["key_count"] = len([k for k in key_pool if key_pool[k].get("priority")==0])
        except Exception as e:
            add_log(logging.ERROR, f"爬虫异常: {e}")
            crawler_status["status"] = "error"
        time.sleep(UPDATE_INTERVAL)

# ============================================
# 调度与失败处理
# ============================================
def get_next_key(exclude_keys=None):
    with pool_lock:
        now = time.time()
        available = []
        for k, v in key_pool.items():
            if exclude_keys and k in exclude_keys:
                continue
            if v.get("cooldown_until", 0) <= now:
                available.append(v)
        if not available:
            return None
        available.sort(key=lambda x: x["priority"])
        best = available[0]
        add_log(logging.INFO, f"选中 Key: {get_provider_display(best)} (priority={best['priority']})")
        return best

def mark_success(key_info):
    with pool_lock:
        key_str = key_info["key"]
        if key_str in key_pool:
            key_pool[key_str]["fail_count"] = 0

def mark_failed(key_info):
    with pool_lock:
        key_str = key_info["key"]
        if key_str not in key_pool:
            return
        if key_info.get("source") == "community":
            fail_count = key_pool[key_str].get("fail_count", 0) + 1
            key_pool[key_str]["fail_count"] = fail_count
            if fail_count >= COMMUNITY_MAX_FAILURES:
                del key_pool[key_str]
                add_log(logging.INFO, f"社区 Key 连续失败 {fail_count} 次，已删除: {preview_key(key_str)}")
            else:
                cooldown_until = time.time() + COMMUNITY_COOLDOWN
                key_pool[key_str]["cooldown_until"] = cooldown_until
                add_log(logging.WARNING, f"社区 Key 失败 ({fail_count}/{COMMUNITY_MAX_FAILURES})，冷却 {COMMUNITY_COOLDOWN}s: {preview_key(key_str)}")
        else:
            cooldown_until = time.time() + P1_P2_COOLDOWN
            key_pool[key_str]["cooldown_until"] = cooldown_until
            add_log(logging.WARNING, f"稳定供应商冷却 {P1_P2_COOLDOWN}s: {key_info.get('name', preview_key(key_str))}")

# ============================================
# 请求处理
# ============================================
def clean_multimodal(body_str):
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
    elif provider_type == "cloudflare":
        # Workers AI 基础 URL，模型名在请求时单独拼接
        return endpoint
    else:
        # 专门为 GitHub Models 处理路径
        if 'models.github.ai' in endpoint or 'models.inference.github.com' in endpoint:
            if endpoint.endswith('/chat/completions'):
                return endpoint
            return endpoint + '/chat/completions'
        # 标准 OpenAI 兼容端点
        if endpoint.endswith('/v1') or endpoint.endswith('/v1beta') or endpoint.endswith('/v4'):
            return endpoint + '/chat/completions'
        # 兼容 Cloudflare AI Gateway 的 compat 端点
        if 'gateway.ai.cloudflare.com' in endpoint and endpoint.endswith('/compat'):
            return endpoint + '/chat/completions'
        # 其他情况直接使用
        return endpoint

def handle_chat_request():
    """处理请求，优先社区 Key，支持 auto 无感切换，支持 OpenAI/Google/Cloudflare Workers AI"""
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
        max_attempts = MAX_RETRIES if not is_auto else 50

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
            headers = {k: v for k, v in request.headers if k.lower() != 'host'}

            # ========== 根据供应商类型构建 URL 和请求体 ==========
            if provider_type == "google":
                url = build_upstream_url(key_info, chosen_model)
                if '?' in url:
                    url += f"&key={key_info['key']}"
                else:
                    url += f"?key={key_info['key']}"
                headers.pop("Authorization", None)
                # 假设客户端已发送 Gemini 格式，直接透传 body
            elif provider_type == "cloudflare":
                base_url = build_upstream_url(key_info, chosen_model)
                # 确保 URL 以 /run 结尾
                if not base_url.endswith('/run'):
                    base_url = base_url.rstrip('/') + '/run'
                url = base_url + '/' + chosen_model.lstrip('/')
                headers = {"Authorization": f"Bearer {key_info['key']}"}
                if "Content-Type" in request.headers:
                    headers["Content-Type"] = request.headers["Content-Type"]
                else:
                    headers["Content-Type"] = "application/json"
                # 将 messages 转换为 prompt
                try:
                    original_data = json.loads(body)
                    messages = original_data.get("messages", [])
                    prompt = ""
                    for msg in messages:
                        if msg.get("role") == "user":
                            prompt += msg.get("content", "") + "\n"
                    prompt = prompt.strip()
                    if not prompt:
                        prompt = "Hello"
                    req_body = {"prompt": prompt}
                    body = json.dumps(req_body)
                except:
                    pass
            else:
                # OpenAI 兼容
                url = build_upstream_url(key_info, chosen_model)
                if not url.endswith('/chat/completions'):
                    url = url.rstrip('/') + '/chat/completions'
                headers["Authorization"] = f"Bearer {key_info['key']}"
                if "Content-Type" not in headers:
                    headers["Content-Type"] = "application/json"

            # ========== 发送请求 ==========
            try:
                upstream_resp = requests.post(
                    url,
                    headers=headers,
                    data=body,
                    timeout=REQUEST_TIMEOUT,
                    stream=True
                )
                if 200 <= upstream_resp.status_code < 300:
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
# Flask 应用
# ============================================
app = Flask(__name__)

# ============================================
# 模型探测辅助函数（统一返回 4 个值）
# ============================================
def _detect_models(endpoint, api_key, provider_type):
    """探测模型核心逻辑，返回 (success, models, error, note)"""
    try:
        if provider_type == "google":
            url = f"{endpoint}/models?key={api_key}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                models_data = resp.json().get("models", [])
                models = [m["name"].replace("models/", "") for m in models_data if "generateContent" in m.get("supportedGenerationMethods", [])]
                return True, models, None, None
            else:
                return True, [], f"Google API 返回 {resp.status_code}，请手动填写模型", "warning"
        elif provider_type == "cloudflare":
            # Cloudflare Workers AI 无模型列表接口
            return True, [], "Cloudflare Workers AI 不支持自动探测，请手动填写模型名（如 @cf/meta/llama-3-8b-instruct）", "info"
        else:
            # OpenAI 兼容
            url = f"{endpoint}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                models = [m["id"] for m in resp.json().get("data", [])]
                return True, models, None, None
            else:
                # 降级：尝试常见模型
                test_models = ["gpt-3.5-turbo", "deepseek-chat", "glm-5", "llama3.1-8b"]
                found = []
                for m in test_models:
                    try:
                        test_resp = requests.post(
                            f"{endpoint}/chat/completions",
                            headers=headers,
                            json={"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                            timeout=5
                        )
                        if test_resp.status_code == 200:
                            found.append(m)
                    except:
                        pass
                if found:
                    return True, found, None, None
                return True, [], f"无法自动探测模型（HTTP {resp.status_code}），请手动填写模型名", "warning"
    except Exception as e:
        return False, None, str(e), "error"

# ============================================
# 模型探测 API（新增模式）
# ============================================
@app.route('/api/providers/detect', methods=['POST'])
def detect_models():
    data = request.get_json()
    endpoint = data.get("endpoint", "").rstrip('/')
    api_key = data.get("api_key", "")
    provider_type = data.get("provider_type", "openai")
    if not endpoint or not api_key:
        return jsonify({"success": False, "error": "缺少参数"}), 400
    success, models, error, note = _detect_models(endpoint, api_key, provider_type)
    if success:
        return jsonify({"success": True, "models": models, "note": note})
    else:
        return jsonify({"success": False, "error": error}), 400

# ============================================
# 模型探测 API（编辑模式，使用已保存的供应商信息）
# ============================================
@app.route('/api/providers/detect/<key_hash>', methods=['POST'])
def detect_provider_models(key_hash):
    with pool_lock:
        target_key = None
        for k, v in key_pool.items():
            if v.get("source") in ("stable", "paid") and hashlib.md5(k.encode()).hexdigest() == key_hash:
                target_key = k
                break
        if not target_key:
            return jsonify({"success": False, "error": "供应商不存在"}), 404
        info = key_pool[target_key]
    endpoint = info["endpoint"]
    api_key = info["key"]
    provider_type = info.get("provider_type", "openai")
    success, models, error, note = _detect_models(endpoint, api_key, provider_type)
    if success:
        return jsonify({"success": True, "models": models, "note": note})
    else:
        return jsonify({"success": False, "error": error}), 400

# ============================================
# Flask 路由
# ============================================
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
    return jsonify({"object": "list", "data": [{"id": "auto", "object": "model", "created": int(time.time()), "owned_by": "proxy"}]})

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
    return jsonify({"total": total, "page": page, "limit": limit, "total_pages": (total + limit - 1) // limit, "logs": page_logs})

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
                    "status": "active" if v.get("cooldown_until", 0) <= time.time() else "cooldown",
                    "provider_type": v.get("provider_type", "openai")
                })
    return jsonify(result)

@app.route('/api/providers', methods=['POST'])
def add_provider():
    data = request.get_json()
    required = ["endpoint", "api_key", "selected_models", "priority"]
    if not all(k in data for k in required):
        return jsonify({"error": "缺少必要参数"}), 400
    key_str = data["api_key"]
    endpoint = data["endpoint"].rstrip("/")
    priority = data["priority"]
    if priority not in [1, 2]:
        priority = 1
    # 优先使用前端传来的 provider_type
    provider_type = data.get("provider_type")
    if not provider_type:
        provider_type = "google" if "googleapis" in endpoint else "openai"
    info = {
        "key": key_str,
        "models": data["selected_models"],
        "verified_models": data["selected_models"],
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
    data = request.get_json()
    with pool_lock:
        target_key = None
        for k, v in key_pool.items():
            if v.get("source") in ("stable", "paid") and hashlib.md5(k.encode()).hexdigest() == key_hash:
                target_key = k
                break
        if not target_key:
            return jsonify({"error": "供应商不存在"}), 404
        info = key_pool[target_key]

    # 更新字段
    if "name" in data and data["name"]:
        info["name"] = data["name"]
    if "endpoint" in data and data["endpoint"]:
        endpoint = data["endpoint"].rstrip("/")
        info["endpoint"] = endpoint
        if "provider_type" not in data or not data["provider_type"]:
            info["provider_type"] = "google" if "googleapis" in endpoint else "openai"
    if "priority" in data:
        priority = data["priority"]
        if priority in [1, 2]:
            info["priority"] = priority
            info["source"] = "stable" if priority == 1 else "paid"
    if "selected_models" in data and data["selected_models"]:
        info["models"] = data["selected_models"]
        info["verified_models"] = data["selected_models"]
    if "provider_type" in data and data["provider_type"]:
        info["provider_type"] = data["provider_type"]
    if "api_key" in data and data["api_key"]:
        new_key = data["api_key"]
        with pool_lock:
            if target_key in key_pool:
                del key_pool[target_key]
            info["key"] = new_key
            key_pool[new_key] = info
            target_key = new_key

    # 保存到文件
    stable_dict = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
    save_stable_providers(stable_dict)
    add_log(logging.INFO, f"更新供应商: {info.get('name', preview_key(target_key))}")
    return jsonify({"success": True})

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

# ============================================
# 供应商测试接口
# ============================================
@app.route('/api/providers/test/<key_hash>', methods=['POST'])
def test_provider(key_hash):
    with pool_lock:
        target_key = None
        for k, v in key_pool.items():
            if v.get("source") in ("stable", "paid") and hashlib.md5(k.encode()).hexdigest() == key_hash:
                target_key = k
                break
        if not target_key:
            return jsonify({"success": False, "error": "供应商不存在"}), 404
        info = key_pool[target_key]

    model_list = info.get("verified_models") or info.get("models") or []
    if not model_list:
        return jsonify({"success": False, "error": "该供应商未配置任何模型"}), 400
    test_model = model_list[0]

    provider_type = info.get("provider_type", "openai")
    base_url = info["endpoint"].rstrip('/')
    api_key = info["key"]

    # 根据供应商类型构造 URL 和请求体
    if provider_type == "google":
        url = f"{base_url}/models/{test_model}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        test_body = {
            "contents": [{"role": "user", "parts": [{"text": "Say 'OK' if you are working."}]}],
            "generationConfig": {"maxOutputTokens": 10}
        }
    elif provider_type == "cloudflare":
        if not base_url.endswith('/run'):
            base_url = base_url + '/run'
        url = base_url + '/' + test_model.lstrip('/')
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        test_body = {"prompt": "Say 'OK' if you are working."}
    else:
        # OpenAI 兼容（包括 deepseek, nvidia, github 等）
        if not base_url.endswith('/chat/completions'):
            if base_url.endswith('/v1') or base_url.endswith('/v4'):
                url = base_url + '/chat/completions'
            else:
                url = base_url
        else:
            url = base_url
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        test_body = {
            "model": test_model,
            "messages": [{"role": "user", "content": "Say 'OK' if you are working."}],
            "max_tokens": 10,
            "stream": False
        }

    try:
        start_time = time.time()
        resp = requests.post(url, headers=headers, json=test_body, timeout=15)
        elapsed_ms = round((time.time() - start_time) * 1000)
        elapsed_s = round(elapsed_ms / 1000, 2)

        if resp.status_code == 200:
            # 更新平均延迟
            old_avg = info.get("avg_latency", 1.0)
            new_avg = old_avg * 0.6 + elapsed_s * 0.4
            info["avg_latency"] = new_avg
            stable_dict = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
            save_stable_providers(stable_dict)

            if provider_type == "google":
                data = resp.json()
                content = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            elif provider_type == "cloudflare":
                data = resp.json()
                content = data.get("result", {}).get("response", "")
                if not content:
                    content = str(data)[:100]
            else:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return jsonify({
                "success": True,
                "status_code": resp.status_code,
                "latency_ms": elapsed_ms,
                "response_preview": content[:100],
                "request_url": url  # 可选，调试用
            })
        else:
            error_body = resp.text[:200]
            return jsonify({
                "success": False,
                "status_code": resp.status_code,
                "error": error_body,
                "latency_ms": elapsed_ms,
                "request_url": url
            }), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200

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
🚀 LLM 智能代理网关 v5.9 (完整修复版) 启动成功！
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 代理地址:   http://{PROXY_HOST}:{PROXY_PORT}/v1
🔧 管理面板:   http://{PROXY_HOST}:{PROXY_PORT}/admin
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 核心特性:
   - 调度优先: 社区 Key (P0) > 稳定供应商 (P1/P2)
   - 社区 Key 失败冷却 {COMMUNITY_COOLDOWN}s，连续 {COMMUNITY_MAX_FAILURES} 次才删除
   - 支持供应商手动测试 (POST /api/providers/test/<hash>)
   - 模型探测自动识别 Google API / Cloudflare Workers AI
   - 日志显示供应商名称和模型
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    serve(app, host=PROXY_HOST, port=PROXY_PORT, threads=10)
