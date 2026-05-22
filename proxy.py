"""
免费 LLM 智能代理网关 v3.2 (最终版)
特性：
  - 多数据源动态管理 (Web 面板可增删)
  - P0(爬虫中转) > P1(官方免费) > P2(付费兜底) 优先级调度
  - 热备用瞬间切换 + 粘性调度 + 自动切回 + 延迟感知
  - 模型探测与验证 (支持 OpenAI / Google 格式)
  - Web 管理面板 (供应商/数据源管理、Key加密)
  - 流式响应支持 (工具调用自动转为流式，谷歌除外)
  - 原生支持 Google Gemini API (无需外部转换)
  - 请求日志与统计 (分页接口)
  - 系统运行状态监控
  - 可选安全认证（环境变量控制，默认关闭）
启动：python proxy.py
管理面板：http://127.0.0.1:8800/admin
"""

import os
import json
import time
import threading
import random
import logging
import hashlib
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler

import requests
from flask import Flask, request, jsonify, Response, render_template
from waitress import serve
from cryptography.fernet import Fernet

# ============================================
# 强制 UTF-8 编码，避免汉字乱码
# ============================================
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============================================
# 安全配置（通过环境变量控制，默认关闭）
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
    if not auth or auth.username != ADMIN_USERNAME or auth.password != ADMIN_PASSWORD:
        return False
    return True

def check_api_token():
    if not ENABLE_API_AUTH:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.split(" ")[1] != API_ACCESS_TOKEN:
        return False
    return True

# ============================================
# 基础配置
# ============================================
PROXY_HOST = os.getenv("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8800"))
UPDATE_INTERVAL = 300          # 爬虫更新间隔(秒)
MAX_RETRIES = 2                # 最大重试次数(不含首次)
COOLDOWN_RATE_LIMIT = 60       # 429限流冷却
COOLDOWN_GENERIC = 10          # 通用错误冷却
REQUEST_TIMEOUT = 8            # 首次请求超时
RETRY_TIMEOUT = 3              # 重试/切换超时
BACKUP_PROBE_INTERVAL = 5      # 备用Key维护间隔(秒)

# 默认端点
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

# 日志设置（按天轮转，保留7天）
log_handler = TimedRotatingFileHandler("proxy.log", when="midnight", interval=1, backupCount=7, encoding="utf-8")
log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger = logging.getLogger("proxy")
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)

# 最近请求日志
MAX_LOG_ENTRIES = 200
recent_logs = []
logs_lock = threading.Lock()

# 系统启动时间
server_start_time = time.time()

# 爬虫状态
crawler_status = {
    "last_update": None,
    "next_update": None,
    "status": "pending",
    "key_count": 0
}

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
# 全局 Key 池
# ============================================
pool_lock = threading.Lock()
key_pool = {}

PROVIDERS_FILE = "providers.json"

# ============================================
# 手动供应商管理
# ============================================
def load_providers():
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
                "name": item.get("name", "未命名"),
                "verified_models": item.get("verified_models", []),
                "priority": item.get("priority", 1),
                "source": "stable" if item["priority"] == 1 else "paid",
                "endpoint": item.get("endpoint", "").rstrip("/"),
                "provider_type": item.get("provider_type", "openai"),
                "cooldown_until": 0,
                "avg_latency": 1.0,
                "rate_limit": "",
                "expiry": ""
            }
        return providers
    except Exception as e:
        add_log(logging.ERROR, f"加载供应商文件失败: {e}")
        return {}

def save_providers(providers_dict):
    data = []
    for key_str, info in providers_dict.items():
        if info.get("source") not in ("stable", "paid"):
            continue
        data.append({
            "name": info.get("name", "未命名"),
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
    add_log(logging.INFO, f"供应商数据已保存 ({len(data)} 个)")

def init_providers():
    providers = load_providers()
    with pool_lock:
        for key_str, info in providers.items():
            if key_str not in key_pool:
                info.setdefault("verified_models", [])
                info.setdefault("avg_latency", 1.0)
                info.setdefault("cooldown_until", 0)
                key_pool[key_str] = info
    return providers

# ============================================
# 热备用调度器
# ============================================
active_key = None
backup_key = None

def get_best_key(exclude_key=None):
    with pool_lock:
        now = time.time()
        available = []
        for k, v in key_pool.items():
            if v.get("cooldown_until", 0) <= now:
                if exclude_key and k == exclude_key:
                    continue
                if not v.get("verified_models"):
                    continue
                available.append(v)
        if not available:
            return None
        available.sort(key=lambda x: (x["priority"], x.get("avg_latency", 999)))
        top_n = available[:min(3, len(available))]
        return random.choice(top_n)

def is_key_alive(key_info, timeout=3):
    if not key_info:
        return False
    try:
        endpoint = key_info["endpoint"]
        model = key_info["verified_models"][0] if key_info["verified_models"] else "gpt-3.5-turbo"
        if key_info.get("provider_type") == "google":
            url = f"{endpoint}/models/{model}:generateContent?key={key_info['key']}"
            headers = {"Content-Type": "application/json"}
            data = {
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 1}
            }
        else:
            url = f"{endpoint}/chat/completions"
            headers = {"Authorization": f"Bearer {key_info['key']}"}
            data = {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1
            }
        resp = requests.post(url, headers=headers, json=data, timeout=(timeout, timeout))
        return resp.status_code == 200
    except Exception:
        return False

def maintain_backup():
    global backup_key, active_key
    while True:
        try:
            time.sleep(BACKUP_PROBE_INTERVAL)
            if active_key:
                better = get_best_key()
                if better and better["priority"] < active_key["priority"]:
                    if is_key_alive(better):
                        backup_key = better
                        add_log(logging.INFO, f"自动切回: 更高优先级Key可用 (P{better['priority']})")
                        continue
            candidate = get_best_key(exclude_key=active_key["key"] if active_key else None)
            if candidate:
                if not backup_key or candidate["key"] != backup_key["key"]:
                    if is_key_alive(candidate):
                        backup_key = candidate
                        add_log(logging.INFO, f"备用Key更新: {preview_key(candidate['key'])} (P{candidate['priority']})")
        except Exception as e:
            add_log(logging.ERROR, f"维护备用Key异常: {e}")

# ============================================
# 请求处理
# ============================================
def build_request_body(key_info, original_body):
    model = key_info["verified_models"][0] if key_info.get("verified_models") else \
            key_info["models"][0] if key_info.get("models") else "gpt-3.5-turbo"

    messages = original_body.get("messages", [])
    system_content = None
    user_messages = []
    for m in messages:
        if m["role"] == "system" and not system_content:
            system_content = m["content"]
        else:
            user_messages.append(m)

    if key_info.get("provider_type") == "google":
        contents = []
        for m in user_messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({
                "role": role,
                "parts": [{"text": m["content"]}]
            })
        req = {"contents": contents}
        if system_content:
            req["systemInstruction"] = {"parts": [{"text": system_content}]}
        if "tools" in original_body:
            add_log(logging.WARNING, "谷歌 API 暂不支持 tools，已忽略工具调用")
        return req, False
    else:
        if not any(m.get("role") == "system" for m in user_messages) and \
           not any(m.get("role") == "tool" for m in user_messages):
            user_messages = [{"role": "system", "content": "你是一个有帮助的助手，请始终使用中文回答。"}] + user_messages
        req = {
            "model": model,
            "messages": user_messages,
            "stream": original_body.get("stream", False)
        }
        if "tools" in original_body:
            req["tools"] = original_body["tools"]
        if "tool_choice" in original_body:
            req["tool_choice"] = original_body["tool_choice"]
        for key in ["temperature", "top_p", "max_tokens"]:
            if key in original_body:
                req[key] = original_body[key]
        return req, original_body.get("stream", False)

def handle_chat_request(body):
    global active_key, backup_key
    if not active_key:
        active_key = get_best_key()
        if not active_key:
            return jsonify({"error": "未配置任何可用Key"}), 503
    timeout = REQUEST_TIMEOUT
    attempted_keys = set()
    for attempt in range(MAX_RETRIES + 1):
        key_info = active_key
        if not key_info or key_info["key"] in attempted_keys:
            break
        attempted_keys.add(key_info["key"])
        start_time = time.time()
        try:
            endpoint = key_info["endpoint"]
            req_body, stream = build_request_body(key_info, body)

            if key_info.get("provider_type") == "google":
                model = req_body.get("model", key_info["verified_models"][0] if key_info.get("verified_models") else key_info["models"][0])
                url = f"{endpoint}/models/{model}:generateContent?key={key_info['key']}"
                headers = {"Content-Type": "application/json"}
                # 谷歌不支持流式，直接强制非流式
                stream = False
            else:
                url = f"{endpoint}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {key_info['key']}",
                    "Content-Type": "application/json"
                }

            resp = requests.post(
                url,
                headers=headers,
                json=req_body,
                timeout=(timeout, timeout),
                stream=stream
            )

            if resp.status_code == 200:
                latency = time.time() - start_time
                update_latency(key_info, latency)
                model_name = req_body.get("model", "unknown")
                add_log(logging.INFO, f"请求成功 | Key: {preview_key(key_info['key'])} | 模型: {model_name} | 延迟: {latency:.2f}s")

                if stream:
                    def generate():
                        for chunk in resp.iter_content(chunk_size=1024):
                            if chunk:
                                yield chunk
                    return Response(generate(), content_type="text/event-stream")
                else:
                    if key_info.get("provider_type") == "google":
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        content = ""
                        if candidates and "content" in candidates[0]:
                            parts = candidates[0]["content"].get("parts", [])
                            content = parts[0]["text"] if parts else ""
                        return jsonify({
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop"
                            }],
                            "model": "auto"
                        })
                    else:
                        try:
                            data = resp.json()
                            data["model"] = "auto"
                            return jsonify(data)
                        except Exception as json_err:
                            add_log(logging.ERROR, f"响应JSON解析失败: {json_err} | 原始响应: {resp.text[:200]}")
                            return jsonify({
                                "choices": [{
                                    "index": 0,
                                    "message": {"role": "assistant", "content": f"[模型返回非JSON响应]\n{resp.text[:500]}"},
                                    "finish_reason": "stop"
                                }],
                                "model": "auto"
                            })

            # 错误处理
            if resp.status_code == 429:
                mark_cooldown(key_info, COOLDOWN_RATE_LIMIT)
                add_log(logging.WARNING, f"429 限流 | Key: {preview_key(key_info['key'])}")
            elif resp.status_code in (401, 403):
                remove_key(key_info)
                add_log(logging.ERROR, f"认证失败，已删除 | Key: {preview_key(key_info['key'])}")
            else:
                mark_cooldown(key_info, COOLDOWN_GENERIC)
                add_log(logging.ERROR, f"{resp.status_code} 错误 | Key: {preview_key(key_info['key'])}")

        except requests.exceptions.Timeout:
            mark_cooldown(key_info, COOLDOWN_GENERIC)
            add_log(logging.ERROR, f"超时 | Key: {preview_key(key_info['key'])}")
        except Exception as e:
            mark_cooldown(key_info, COOLDOWN_GENERIC)
            add_log(logging.ERROR, f"请求异常: {e} | Key: {preview_key(key_info['key'])}")

        # 热备用切换
        if backup_key and backup_key != active_key:
            active_key = backup_key
            backup_key = None
            timeout = RETRY_TIMEOUT
            add_log(logging.INFO, f"热备用切换至: {preview_key(active_key['key'])}")
            threading.Thread(target=maintain_backup_once, daemon=True).start()
        else:
            new_key = get_best_key()
            if new_key and new_key["key"] not in attempted_keys:
                active_key = new_key
                timeout = RETRY_TIMEOUT
            else:
                break
    return jsonify({"error": "所有 API Key 暂时不可用，请稍后重试"}), 503

def maintain_backup_once():
    global backup_key
    candidate = get_best_key(exclude_key=active_key["key"] if active_key else None)
    if candidate and is_key_alive(candidate):
        backup_key = candidate

def update_latency(key_info, latency):
    key_info["avg_latency"] = key_info.get("avg_latency", 1.0) * 0.6 + latency * 0.4

def mark_cooldown(key_info, seconds):
    with pool_lock:
        if key_info["key"] in key_pool:
            key_pool[key_info["key"]]["cooldown_until"] = time.time() + seconds

def remove_key(key_info):
    key_str = key_info["key"]
    with pool_lock:
        if key_str in key_pool:
            del key_pool[key_str]
    global active_key, backup_key
    if active_key and active_key["key"] == key_str:
        active_key = None
    if backup_key and backup_key["key"] == key_str:
        backup_key = None

# ============================================
# 模型探测
# ============================================
def verify_key_models(key_str, info):
    endpoint = info["endpoint"]
    provider = info.get("provider_type", "openai")

    if provider == "google":
        url = f"{endpoint}/models?key={key_str}"
        try:
            resp = requests.get(url, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                all_models = [m["name"].replace("models/", "") for m in data.get("models", [])
                              if "generateContent" in m.get("supportedGenerationMethods", [])]
                verified = [m for m in all_models if "gemini" in m]
                if not verified and all_models:
                    verified = all_models[:1]
                info["verified_models"] = verified
                add_log(logging.INFO, f"模型探测成功: {preview_key(key_str)} -> {verified}")
                return
        except Exception:
            pass
        return
    else:
        try:
            resp = requests.get(f"{endpoint}/models", headers={"Authorization": f"Bearer {key_str}"}, timeout=8)
            if resp.status_code == 200:
                all_models = [m["id"] for m in resp.json().get("data", [])]
                verified = [m for m in all_models if m in TARGET_MODELS]
                if not verified and all_models:
                    verified = all_models[:1]
                info["verified_models"] = verified
                add_log(logging.INFO, f"模型探测成功: {preview_key(key_str)} -> {verified}")
                return
        except Exception:
            pass
        test_model = info.get("models", ["gpt-3.5-turbo"])[0]
        try:
            resp = requests.post(
                f"{endpoint}/chat/completions",
                headers={"Authorization": f"Bearer {key_str}"},
                json={"model": test_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                timeout=8
            )
            if resp.status_code == 200:
                info["verified_models"] = [test_model]
                add_log(logging.INFO, f"模型探测成功(降级): {preview_key(key_str)} -> {test_model}")
            else:
                add_log(logging.WARNING, f"模型探测失败: {preview_key(key_str)} 状态码 {resp.status_code}")
        except Exception as e:
            add_log(logging.ERROR, f"模型探测异常: {preview_key(key_str)} {e}")

def verify_new_keys(key_list):
    for key_str in key_list:
        with pool_lock:
            info = key_pool.get(key_str)
        if info and not info.get("verified_models"):
            verify_key_models(key_str, info)

# ============================================
# 爬虫线程（支持多源）
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
                    endpoint = src.get("endpoint", DEFAULT_P0_ENDPOINT)
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
                                    "verified_models": [],
                                    "priority": 0,
                                    "source": "community",
                                    "endpoint": endpoint,
                                    "provider_type": "openai",
                                    "cooldown_until": 0,
                                    "avg_latency": 1.0,
                                    "rate_limit": item.get("rate_limit", ""),
                                    "expiry": item.get("expiry", "")
                                }
                except Exception as e:
                    add_log(logging.ERROR, f"抓取源 {src['url']} 失败: {e}")
            # 合并到内存池
            with pool_lock:
                old_p0 = {k: v for k, v in key_pool.items() if v.get("priority") == 0}
                for k in list(key_pool.keys()):
                    if key_pool[k].get("priority") == 0:
                        del key_pool[k]
                for k, v in all_new_p0.items():
                    if k in old_p0:
                        if old_p0[k].get("verified_models"):
                            v["verified_models"] = old_p0[k]["verified_models"]
                        if old_p0[k].get("avg_latency"):
                            v["avg_latency"] = old_p0[k]["avg_latency"]
                    key_pool[k] = v
            add_log(logging.INFO, f"爬虫更新完成，P0 Key 数量: {len(all_new_p0)}")
            crawler_status["last_update"] = datetime.now().isoformat()
            crawler_status["next_update"] = (datetime.now() + timedelta(seconds=UPDATE_INTERVAL)).isoformat()
            crawler_status["status"] = "success"
            crawler_status["key_count"] = len(all_new_p0)
            new_keys = [k for k in all_new_p0 if not all_new_p0[k].get("verified_models")]
            if new_keys:
                threading.Thread(target=verify_new_keys, args=(new_keys,), daemon=True).start()
        except Exception as e:
            add_log(logging.ERROR, f"爬虫异常: {e}")
            crawler_status["status"] = "error"
        time.sleep(UPDATE_INTERVAL)

# ============================================
# Flask 应用
# ============================================
app = Flask(__name__)

# 全局认证钩子（管理接口）
@app.before_request
def require_admin_auth():
    if request.path.startswith('/admin') or (request.path.startswith('/api/') and request.path != '/api/providers/detect'):
        if not check_admin_auth():
            return Response('需要管理员凭证', 401, {'WWW-Authenticate': 'Basic realm="Admin Area"'})

# 聊天接口（可选API Token认证）
@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    if not check_api_token():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        body = request.get_json()
        if not body:
            return jsonify({"error": "无效请求"}), 400
        return handle_chat_request(body)
    except Exception as e:
        add_log(logging.ERROR, f"请求处理异常: {e}")
        return jsonify({"error": str(e)}), 500

# 模型列表接口
@app.route('/v1/models', methods=['GET'])
def list_models():
    model_set = set()
    with pool_lock:
        for v in key_pool.values():
            if v.get("cooldown_until", 0) <= time.time() and v.get("verified_models"):
                model_set.update(v["verified_models"])
    models = [{"id": m, "object": "model", "created": int(time.time()), "owned_by": "proxy"} for m in model_set]
    auto_model = {"id": "auto", "object": "model", "created": int(time.time()), "owned_by": "proxy"}
    models.insert(0, auto_model)
    return jsonify({"object": "list", "data": models})

# 健康检查
@app.route('/health')
def health():
    with pool_lock:
        now = time.time()
        active = sum(1 for v in key_pool.values() if v.get("cooldown_until", 0) <= now)
        p0 = sum(1 for v in key_pool.values() if v.get("priority") == 0)
        p1 = sum(1 for v in key_pool.values() if v.get("priority") == 1)
        p2 = sum(1 for v in key_pool.values() if v.get("priority") == 2)
    uptime_seconds = int(time.time() - server_start_time)
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{days}天 {hours}时 {minutes}分" if days > 0 else f"{hours}时 {minutes}分 {seconds}秒"
    active_info = None
    if active_key:
        active_info = {
            "preview": preview_key(active_key["key"]),
            "model": active_key["verified_models"][0] if active_key.get("verified_models") else (active_key.get("models", [""])[0]),
            "priority": active_key["priority"]
        }
    backup_info = None
    if backup_key:
        backup_info = {
            "preview": preview_key(backup_key["key"]),
            "model": backup_key["verified_models"][0] if backup_key.get("verified_models") else (backup_key.get("models", [""])[0]),
            "priority": backup_key["priority"]
        }
    with logs_lock:
        total_requests = len(recent_logs)
        success_count = sum(1 for l in recent_logs if "成功" in l["message"])
        success_rate = round(success_count / total_requests, 3) if total_requests else 0
        avg_latency = round(active_key["avg_latency"], 2) if active_key else 0
    return jsonify({
        "status": "ok",
        "total": len(key_pool),
        "active": active,
        "pool_stats": {"P0": p0, "P1": p1, "P2": p2},
        "active_key": active_info,
        "backup_key": backup_info,
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "crawler": {
            "last_update": crawler_status["last_update"],
            "next_update": crawler_status["next_update"],
            "status": crawler_status["status"],
            "key_count": crawler_status["key_count"]
        },
        "stats": {
            "total_requests": total_requests,
            "success_rate": success_rate,
            "avg_latency": avg_latency
        }
    })

# 分页日志
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
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": (total + limit - 1) // limit,
        "logs": page_logs
    })

# 供应商列表
@app.route('/api/providers', methods=['GET'])
def list_providers():
    result = []
    with pool_lock:
        for k, v in key_pool.items():
            if v.get("source") in ("stable", "paid"):
                result.append({
                    "key_preview": preview_key(k),
                    "full_key_hash": hashlib.md5(k.encode()).hexdigest(),
                    "name": v.get("name", "未命名"),
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

# 探测模型
@app.route('/api/providers/detect', methods=['POST'])
def detect_models():
    data = request.get_json()
    endpoint = data.get("endpoint", "").rstrip("/")
    api_key = data.get("api_key", "")
    if not endpoint or not api_key:
        return jsonify({"success": False, "error": "缺少参数"}), 400
    # 简单判断是否为谷歌
    is_google = "googleapis" in endpoint
    if is_google:
        try:
            url = f"{endpoint}/models?key={api_key}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                models_data = resp.json().get("models", [])
                all_models = [m["name"].replace("models/", "") for m in models_data
                              if "generateContent" in m.get("supportedGenerationMethods", [])]
                return jsonify({"success": True, "models": all_models})
            else:
                return jsonify({"success": False, "error": f"谷歌API请求失败 (HTTP {resp.status_code})"}), 400
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400
    else:
        try:
            resp = requests.get(f"{endpoint}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
            if resp.status_code == 200:
                all_models = [m["id"] for m in resp.json().get("data", [])]
                return jsonify({"success": True, "models": all_models})
            else:
                for m in ["gpt-3.5-turbo", "gpt-4o-mini"]:
                    try:
                        t_resp = requests.post(
                            f"{endpoint}/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}"},
                            json={"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                            timeout=8
                        )
                        if t_resp.status_code == 200:
                            return jsonify({"success": True, "models": [m], "note": "降级探测"})
                    except Exception:
                        continue
                return jsonify({"success": False, "error": f"无法探测模型 (HTTP {resp.status_code})"}), 400
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

# 添加供应商
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
    # 自动判断谷歌
    provider_type = data.get("provider_type", "")
    if not provider_type:
        provider_type = "google" if "googleapis" in endpoint else "openai"
    info = {
        "key": key_str,
        "models": data["selected_models"],
        "verified_models": data.get("verified_models", []),
        "priority": priority,
        "source": "stable" if priority == 1 else "paid",
        "endpoint": endpoint,
        "provider_type": provider_type,
        "cooldown_until": 0,
        "avg_latency": 1.0,
        "name": data.get("name", "未命名"),
        "created_at": int(time.time())
    }
    with pool_lock:
        key_pool[key_str] = info
    providers = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
    save_providers(providers)
    threading.Thread(target=verify_key_models, args=(key_str, info), daemon=True).start()
    add_log(logging.INFO, f"添加供应商: {preview_key(key_str)} (P{priority})")
    return jsonify({"success": True, "key_preview": preview_key(key_str)}), 201

# 修改供应商
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
        if "endpoint" in data and data["endpoint"]:
            info["endpoint"] = data["endpoint"].rstrip("/")
        if "selected_models" in data:
            info["models"] = data["selected_models"]
            if not data.get("verified_models"):
                threading.Thread(target=verify_key_models, args=(target_key, info), daemon=True).start()
        if "api_key" in data and data["api_key"]:
            new_key = data["api_key"]
            del key_pool[target_key]
            global active_key, backup_key
            if active_key and active_key["key"] == target_key:
                active_key = None
            if backup_key and backup_key["key"] == target_key:
                backup_key = None
            info["key"] = new_key
            key_pool[new_key] = info
            target_key = new_key
        if "priority" in data:
            info["priority"] = data["priority"]
            info["source"] = "stable" if data["priority"] == 1 else "paid"
        if "name" in data:
            info["name"] = data["name"]
        if "verified_models" in data:
            info["verified_models"] = data["verified_models"]
    providers = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
    save_providers(providers)
    add_log(logging.INFO, f"修改供应商: {preview_key(target_key)}")
    return jsonify({"success": True})

# 删除供应商
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
    global active_key, backup_key
    if active_key and active_key["key"] == target_key:
        active_key = None
    if backup_key and backup_key["key"] == target_key:
        backup_key = None
    providers = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
    save_providers(providers)
    add_log(logging.INFO, f"删除供应商: {preview_key(target_key)}")
    return jsonify({"success": True})

# 数据源列表
@app.route('/api/sources', methods=['GET'])
def list_sources():
    return jsonify(load_sources())

# 添加数据源
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

# 删除数据源
@app.route('/api/sources/<int:index>', methods=['DELETE'])
def delete_source(index):
    sources = load_sources()
    if 0 <= index < len(sources):
        sources.pop(index)
        save_sources(sources)
        add_log(logging.INFO, f"删除数据源 #{index}")
        return jsonify({"success": True})
    return jsonify({"error": "无效索引"}), 404

# 管理面板
@app.route('/admin')
def admin():
    return render_template('admin.html')

# ============================================
# 主启动
# ============================================
if __name__ == '__main__':
    init_providers()
    threading.Thread(target=crawler_loop, daemon=True).start()
    # 等待爬虫首次抓取
    wait_start = time.time()
    while time.time() - wait_start < 10:
        with pool_lock:
            if any(v.get("priority") == 0 for v in key_pool.values()):
                break
        time.sleep(0.5)
    with pool_lock:
        unverified = [k for k, v in key_pool.items() if not v.get("verified_models")]
    if unverified:
        threading.Thread(target=verify_new_keys, args=(unverified,), daemon=True).start()
    threading.Thread(target=maintain_backup, daemon=True).start()
    print(f"""
🚀 免费 LLM 智能代理网关 v3.2 启动成功！
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 代理地址:   http://{PROXY_HOST}:{PROXY_PORT}/v1
🔧 管理面板:   http://{PROXY_HOST}:{PROXY_PORT}/admin
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Key 池状态: (爬虫与供应商加载中...)
   访问 /health 或管理面板查看详情
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    serve(app, host=PROXY_HOST, port=PROXY_PORT, threads=10)
