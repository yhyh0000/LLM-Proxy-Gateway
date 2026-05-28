"""
免费 LLM 智能代理网关 v6.0 (最终稳定版)
- 优先级: P0(公益) > P1(免费) > P2(付费)
- 同优先级下社区 Key 优先于手动添加
- 手动添加的供应商永不删除，仅冷却
- 社区 Key 智能删除：永久错误立即删，临时错误累计删除
- Token 统计（非流式请求自动统计）
- 所有时间为北京时间
启动：python proxy.py
管理面板：http://127.0.0.1:8800/admin
"""

import os
import json
import time
import threading
import logging
import hashlib
import gzip
import random
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
import brotli
import zstandard as zstd

import requests
from flask import Flask, request, jsonify, Response, render_template
from waitress import serve
from cryptography.fernet import Fernet

# ============================================
# 强制 UTF-8 编码
# ============================================
import sys
import io as sysio
sys.stdout = sysio.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = sysio.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ============================================
# 北京时间工具
# ============================================
BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now():
    return datetime.now(BEIJING_TZ)

def beijing_now_str():
    return beijing_now().isoformat(timespec='milliseconds')

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
PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8800"))
UPDATE_INTERVAL = 300
MAX_RETRIES = 10
REQUEST_TIMEOUT = 120
P1_P2_COOLDOWN = 60
COMMUNITY_COOLDOWN = 60
COMMUNITY_MAX_FAILURES = 3

DEFAULT_P0_ENDPOINT = "https://aiapiv2.pekpik.com/v1"

SOURCES_FILE = "sources.json"
DEFAULT_SOURCES = [
    {
        "url": "https://ql.suhm.top/files/discord/data/llm_keys.json",
        "target_models": ["deepseek-chat"],
        "endpoint": DEFAULT_P0_ENDPOINT,
        "name": "默认中转站"
    }
]

PROVIDERS_FILE = "providers.json"
PREFERRED_FILE = "preferred.json"

def load_preferred():
    if not os.path.exists(PREFERRED_FILE):
        return []
    try:
        with open(PREFERRED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []

def save_preferred(preferred_list):
    with open(PREFERRED_FILE, "w", encoding="utf-8") as f:
        json.dump(preferred_list, f, indent=2)

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
            "time": beijing_now_str(),
            "level": logging.getLevelName(level),
            "message": message
        })
        if len(recent_logs) > MAX_LOG_ENTRIES:
            recent_logs.pop(0)

# ============================================
# Token 使用统计
# ============================================
token_stats = {
    "total_tokens": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "request_count": 0
}
token_stats_lock = threading.Lock()

def update_token_usage(usage: dict):
    if not usage:
        return
    with token_stats_lock:
        token_stats["total_tokens"] += usage.get("total_tokens", 0)
        token_stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        token_stats["completion_tokens"] += usage.get("completion_tokens", 0)
        token_stats["request_count"] += 1
        add_log(logging.INFO, f"Token 统计 +{usage.get('total_tokens',0)} (累计: {token_stats['total_tokens']})")

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
# 稳定供应商加载/保存（支持 priority 0,1,2）
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
            priority = item.get("priority", 1)
            if priority in (0, 1):
                source = "stable"
            else:
                source = "paid"
            providers[key_str] = {
                "key": key_str,
                "models": item.get("selected_models", []),
                "name": item.get("name", preview_key(key_str)),
                "verified_models": item.get("verified_models", []),
                "priority": priority,
                "source": source,
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
# 爬虫线程（保留手动添加的 stable/paid，不覆盖）
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
                keep = {k: v for k, v in key_pool.items() if v.get("source") in ("stable", "paid")}
                for k, new_info in all_new_p0.items():
                    if k in keep:
                        continue
                    keep[k] = new_info
                key_pool.clear()
                key_pool.update(keep)
            add_log(logging.INFO, f"爬虫更新完成，社区 Key 数量: {len(all_new_p0)}")
            crawler_status["last_update"] = beijing_now_str()
            crawler_status["next_update"] = (beijing_now() + timedelta(seconds=UPDATE_INTERVAL)).isoformat(timespec='milliseconds')
            crawler_status["status"] = "success"
            crawler_status["key_count"] = len([k for k in key_pool if key_pool[k].get("priority")==0 and key_pool[k].get("source")=="community"])
        except Exception as e:
            add_log(logging.ERROR, f"爬虫异常: {e}")
            crawler_status["status"] = "error"
        time.sleep(UPDATE_INTERVAL)

# ============================================
# 调度与失败处理（关键修改）
# ============================================
def get_next_key(exclude_keys=None):
    with pool_lock:
        now = time.time()
        preferred_hashes = load_preferred()  # 存储的是供应商的 full_key_hash

        # 优先尝试指定的供应商（按顺序）
        for ph in preferred_hashes:
            for k, v in key_pool.items():
                if exclude_keys and k in exclude_keys:
                    continue
                # 只对手动添加的供应商生效
                if v.get("source") not in ("stable", "paid"):
                    continue
                if hashlib.md5(k.encode()).hexdigest() == ph:
                    if v.get("cooldown_until", 0) <= now:
                        add_log(logging.INFO, f"✅ 优先使用指定供应商: {get_provider_display(v)}")
                        return v
                    else:
                        add_log(logging.INFO, f"⏳ 优先供应商 {v.get('name')} 冷却中，跳过")
                    break  # 每个优先 hash 只检查一次

        # 原有逻辑：收集所有可用 Key
        available = []
        for k, v in key_pool.items():
            if exclude_keys and k in exclude_keys:
                continue
            if v.get("cooldown_until", 0) <= now:
                available.append(v)
        if not available:
            return None
        available.sort(key=lambda x: (x["priority"], 0 if x.get("source") == "community" else 1))
        best = available[0]
        add_log(logging.INFO, f"选中 Key: {get_provider_display(best)} (priority={best['priority']}, source={best.get('source')})")
        return best

def mark_success(key_info):
    with pool_lock:
        key_str = key_info["key"]
        if key_str in key_pool:
            key_pool[key_str]["fail_count"] = 0

def mark_failed(key_info, status_code=None):
    """智能失败处理：手动添加的永不删除；社区 Key 根据状态码决定"""
    with pool_lock:
        key_str = key_info["key"]
        if key_str not in key_pool:
            return
        
        # 手动添加的供应商（stable / paid）永不删除，只冷却
        if key_info.get("source") in ("stable", "paid"):
            cooldown_until = time.time() + P1_P2_COOLDOWN
            key_pool[key_str]["cooldown_until"] = cooldown_until
            add_log(logging.WARNING, f"稳定供应商冷却 {P1_P2_COOLDOWN}s: {key_info.get('name', preview_key(key_str))} (状态码: {status_code})")
            return
        
        # 社区 Key 逻辑
        if key_info.get("source") == "community":
            # 永久错误状态码：密钥无效、禁止访问、不存在等
            permanent_errors = {401, 403, 404, 410}
            if status_code in permanent_errors:
                del key_pool[key_str]
                add_log(logging.INFO, f"社区 Key 因永久错误 (HTTP {status_code}) 已删除: {preview_key(key_str)}")
                return
            
            # 临时错误：增加失败计数
            fail_count = key_pool[key_str].get("fail_count", 0) + 1
            key_pool[key_str]["fail_count"] = fail_count
            if fail_count >= COMMUNITY_MAX_FAILURES:
                del key_pool[key_str]
                add_log(logging.INFO, f"社区 Key 连续临时失败 {fail_count} 次，已删除: {preview_key(key_str)}")
            else:
                cooldown_until = time.time() + COMMUNITY_COOLDOWN
                key_pool[key_str]["cooldown_until"] = cooldown_until
                add_log(logging.WARNING, f"社区 Key 临时失败 ({fail_count}/{COMMUNITY_MAX_FAILURES})，冷却 {COMMUNITY_COOLDOWN}s: {preview_key(key_str)} (状态码: {status_code})")

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
        return endpoint
    else:
        if 'models.github.ai' in endpoint or 'models.inference.github.com' in endpoint:
            if endpoint.endswith('/chat/completions'):
                return endpoint
            return endpoint + '/chat/completions'
        if endpoint.endswith('/v1') or endpoint.endswith('/v1beta') or endpoint.endswith('/v4'):
            return endpoint + '/chat/completions'
        if 'gateway.ai.cloudflare.com' in endpoint and endpoint.endswith('/compat'):
            return endpoint + '/chat/completions'
        return endpoint


def anthropic_to_openai(anthropic_body: dict) -> dict:
    """将 Anthropic /v1/messages 请求体转换为 OpenAI /v1/chat/completions 格式"""
    openai_messages = []
    system_prompt = None

    # 处理 system 字段（可以是字符串或数组）
    if "system" in anthropic_body:
        sys_content = anthropic_body["system"]
        if isinstance(sys_content, list):
            # 提取所有 text 块
            texts = [item.get("text", "") for item in sys_content if item.get("type") == "text"]
            system_prompt = "\n".join(texts)
        else:
            system_prompt = sys_content

    # 处理 messages
    for msg in anthropic_body.get("messages", []):
        role = msg["role"]
        content = msg["content"]

        # 处理多模态 content（数组形式）
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                # 处理 image 块（简单丢弃，因为 OpenAI 格式不同，可以后续扩展）
            content = " ".join(text_parts) if text_parts else ""

        openai_messages.append({"role": role, "content": content})

    # 转换 tools（如果存在）
    openai_tools = None
    if "tools" in anthropic_body:
        openai_tools = []
        for tool in anthropic_body["tools"]:
            # Anthropic 的 input_schema 就是 OpenAI 的 parameters
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {})
                }
            })

    # 构建 OpenAI 请求
    openai_payload = {
        "model": anthropic_body.get("model", "auto"),
        "messages": openai_messages,
        "max_tokens": anthropic_body.get("max_tokens", 1024),
        "temperature": anthropic_body.get("temperature", 1.0),
        "stream": anthropic_body.get("stream", False),
    }
    if system_prompt:
        openai_payload["system"] = system_prompt
    if openai_tools:
        openai_payload["tools"] = openai_tools
        # 如果要求自动选择工具
        if anthropic_body.get("tool_choice"):
            openai_payload["tool_choice"] = anthropic_body["tool_choice"]

    return openai_payload


def openai_to_anthropic(openai_response: dict, original_model: str) -> dict:
    """将 OpenAI /v1/chat/completions 响应转换为 Anthropic /v1/messages 格式"""
    # 提取第一个 choice
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    stop_reason = choice.get("finish_reason", "")

    # 映射 stop_reason
    if stop_reason == "stop":
        stop_reason = "end_turn"
    elif stop_reason == "length":
        stop_reason = "max_tokens"
    elif stop_reason == "tool_calls":
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"

    # 处理工具调用
    tool_calls = []
    if "tool_calls" in message:
        for tc in message["tool_calls"]:
            tool_calls.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": tc["function"]["name"],
                "input": json.loads(tc["function"]["arguments"])
            })
        # 如果有 tool_calls，content 可能为空
        if tool_calls and not content:
            content = None

    anthropic_response = {
        "id": openai_response.get("id", ""),
        "type": "message",
        "role": "assistant",
        "model": original_model,
        "content": tool_calls if tool_calls else [{"type": "text", "text": content}] if content else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_response.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_response.get("usage", {}).get("completion_tokens", 0)
        }
    }
    return anthropic_response

def handle_chat_request():
    """处理请求，支持流式和非流式，非流式时统计 Token"""
    try:
        body = request.get_data(as_text=True)
        body = clean_multimodal(body)

        try:
            data = json.loads(body)
            is_auto = (data.get("model") == "auto")
            is_stream = data.get("stream", False)  # 获取流式标志
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

            # 构建上游 URL
            if provider_type == "google":
                url = build_upstream_url(key_info, chosen_model)
                if '?' in url:
                    url += f"&key={key_info['key']}"
                else:
                    url += f"?key={key_info['key']}"
                headers.pop("Authorization", None)
            elif provider_type == "cloudflare":
                base_url = build_upstream_url(key_info, chosen_model)
                if not base_url.endswith('/run'):
                    base_url = base_url.rstrip('/') + '/run'
                url = base_url + '/' + chosen_model.lstrip('/')
                headers = {"Authorization": f"Bearer {key_info['key']}"}
                if "Content-Type" in request.headers:
                    headers["Content-Type"] = request.headers["Content-Type"]
                else:
                    headers["Content-Type"] = "application/json"
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
                url = build_upstream_url(key_info, chosen_model)
                if not url.endswith('/chat/completions'):
                    url = url.rstrip('/') + '/chat/completions'
                headers["Authorization"] = f"Bearer {key_info['key']}"
                if "Content-Type" not in headers:
                    headers["Content-Type"] = "application/json"

            try:
                # 根据是否流式决定是否使用 stream=True
                use_stream = is_stream
                upstream_resp = requests.post(
                    url,
                    headers=headers,
                    data=body,
                    timeout=REQUEST_TIMEOUT,
                    stream=use_stream
                )
                if 200 <= upstream_resp.status_code < 300:
                    mark_success(key_info)

                    if is_stream:
                        # 流式响应：使用生成器透传，不统计 token
                        def generate():
                            try:
                                for chunk in upstream_resp.iter_content(8192):
                                    if chunk:
                                        yield chunk
                            except BrokenPipeError:
                                add_log(logging.WARNING, f"客户端断开连接，供应商: {get_provider_display(key_info)}")
                                return
                            except Exception as e:
                                add_log(logging.ERROR, f"透传异常: {e}")
                                raise
                        response = Response(generate(), status=upstream_resp.status_code)
                        # 复制响应头：对于手动供应商删除 Content-Encoding；社区 Key 保留原样
                        for k, v in upstream_resp.headers.items():
                            if k.lower() in ('connection', 'keep-alive', 'transfer-encoding'):
                                continue
                            if key_info.get("source") in ("stable", "paid") and k.lower() == 'content-encoding':
                                continue
                            response.headers[k] = v
                        add_log(logging.INFO, f"流式请求成功 供应商: {get_provider_display(key_info)} 模型: {chosen_model}")
                        return response
                    else:
                        # 非流式响应：获取完整内容，统计 token 后返回
                        content = upstream_resp.content
                        if len(content) == 0:
                            add_log(logging.WARNING, f"上游返回空内容，供应商: {get_provider_display(key_info)}")
                            mark_failed(key_info)
                            continue

                        # 统计 token：尝试解析 JSON 获取 usage
                        # 统计 token：仅当内容看起来是 JSON 时才解析
                        if len(content) > 0 and (content[0] == ord('{') or content[0] == ord('[')):
                            try:
                                resp_json = json.loads(content.decode('utf-8'))
                                usage = resp_json.get('usage')
                                if usage:
                                    update_token_usage(usage)
                            except Exception as e:
                                add_log(logging.WARNING, f"Token 统计解析失败: {e}")
                        else:
                            add_log(logging.INFO, "响应体非 JSON，跳过 Token 统计")

                        # 构建响应
                        response = Response(content, status=upstream_resp.status_code)
                        for k, v in upstream_resp.headers.items():
                            if k.lower() in ('connection', 'keep-alive', 'transfer-encoding'):
                                continue
                            if key_info.get("source") in ("stable", "paid") and k.lower() == 'content-encoding':
                                continue
                            response.headers[k] = v
                        response.headers['Content-Length'] = str(len(content))
                        if 'content-type' not in response.headers:
                            response.headers['Content-Type'] = 'application/json'
                        add_log(logging.INFO, f"非流式请求成功 供应商: {get_provider_display(key_info)} 模型: {chosen_model}")
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
# 模型探测辅助函数
# ============================================
def _detect_models(endpoint, api_key, provider_type):
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
            return True, [], "Cloudflare Workers AI 不支持自动探测，请手动填写模型名（如 @cf/meta/llama-3-8b-instruct）", "info"
        else:
            url = f"{endpoint}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                models = [m["id"] for m in resp.json().get("data", [])]
                return True, models, None, None
            else:
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
    
def openai_to_anthropic(openai_response: dict, original_model: str) -> dict:
    """OpenAI Chat Completion → Anthropic Messages 响应转换"""
    # 提取 choices
    choice = openai_response.get("choices", [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "")
    finish_reason = choice.get("finish_reason", "")

    # Anthropic 的 stop_reason 映射
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "content_filter"
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    # 处理 tool_calls
    content_blocks = []
    if "tool_calls" in message and message["tool_calls"]:
        for tc in message["tool_calls"]:
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{hash(tc['function']['name'])}"),
                "name": tc["function"]["name"],
                "input": json.loads(tc["function"]["arguments"])
            })
    # 如果有文本内容，也添加文本块
    if content:
        content_blocks.append({"type": "text", "text": content})

    # 如果没有 content_blocks，放一个空文本块（Anthropic 要求 content 至少是空列表）
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    # 获取 usage
    usage = openai_response.get("usage", {})
    anthropic_response = {
        "id": openai_response.get("id", f"msg_{int(time.time())}"),
        "type": "message",
        "role": "assistant",
        "model": original_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)
        }
    }
    return anthropic_response

def openai_to_anthropic_stream_chunk(openai_chunk: dict, model: str) -> dict | None:
    """将 OpenAI 流式 chunk 转换为 Anthropic 格式，增加防御性检查"""
    # 调试：打印原始 chunk（在生产环境可注释）
    # add_log(logging.DEBUG, f"Stream chunk: {json.dumps(openai_chunk)[:200]}")
    
    # 检查必要字段是否存在
    choices = openai_chunk.get("choices")
    if not choices or not isinstance(choices, list) or len(choices) == 0:
        # 有些后端会发送空 choices 或非列表，忽略这种 chunk
        return None
    
    choice = choices[0]
    delta = choice.get("delta", {})
    content = delta.get("content", "")
    finish_reason = choice.get("finish_reason", None)
    index = choice.get("index", 0)
    
    # 如果既没有内容也没有结束标志，忽略
    if not content and not finish_reason:
        return None
    
    # 有文本内容：返回 content_block_delta
    if content:
        return {
            "type": "content_block_delta",
            "index": index,
            "delta": {
                "type": "text_delta",
                "text": content
            }
        }
    
    # 有结束标志：返回 message_stop
    if finish_reason:
        return {
            "type": "message_stop"
        }
    
    return None
# ============================================
# Flask 路由
# ============================================
@app.route('/api/preferred', methods=['GET'])
def get_preferred():
    return jsonify(load_preferred())

@app.route('/api/preferred', methods=['POST'])
def set_preferred():
    if not check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "Expected list of provider hashes or names"}), 400
    save_preferred(data)
    add_log(logging.INFO, f"优先供应商列表已更新: {data}")
    return jsonify({"success": True})

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


@app.before_request
def require_admin_auth():
    if request.path.startswith('/admin') or (request.path.startswith('/api/') and request.path not in ('/api/providers/detect', '/api/stats/token', '/api/logs')):
        if not check_admin_auth():
            return Response('需要管理员凭证', 401, {'WWW-Authenticate': 'Basic realm="Admin Area"'})

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    if not check_api_token():
        return jsonify({"error": "Unauthorized"}), 401
    return handle_chat_request()


@app.route('/v1/messages', methods=['POST'])
def anthropic_messages():
    """Anthropic 风格 API 的适配器"""
    # 1. 认证（复用相同的 API token 检查）
    if not check_api_token():
        return jsonify({"error": "Unauthorized"}), 401

    # 2. 获取请求体
    try:
        anthropic_body = request.get_json()
        if not anthropic_body:
            return jsonify({"error": "Invalid JSON body"}), 400
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    # 3. 转换为 OpenAI 格式
    openai_payload = anthropic_to_openai(anthropic_body)
    is_stream = anthropic_body.get("stream", False)

    # 4. 调用内部的 /v1/chat/completions（通过本地 HTTP 请求）
    # 注意：需要构造正确的请求头，特别是 Authorization
    headers = {
        "Content-Type": "application/json"
    }
    # 传递原始请求中的 Authorization（如果有）
    auth_header = request.headers.get("Authorization")
    if auth_header:
        headers["Authorization"] = auth_header

    # 获取本地服务地址（使用当前监听的地址）
    local_url = f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions"

    try:
        # 使用 stream 模式请求内部端点
        upstream_resp = requests.post(
            local_url,
            headers=headers,
            json=openai_payload,
            timeout=REQUEST_TIMEOUT,
            stream=is_stream
        )

        if upstream_resp.status_code != 200:
            # 透传错误
            error_body = upstream_resp.text
            return Response(error_body, status=upstream_resp.status_code, content_type="application/json")

        if is_stream:
            # 从上游获取流式响应后，定义一个闭包来捕获 upstream_resp 和 model
            def generate():
                for line in upstream_resp.iter_lines():
                    if not line:
                        continue
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        data_str = line[6:]
                        if data_str == '[DONE]':
                            yield 'data: {"type": "message_stop"}\n\n'
                            break
                        try:
                            openai_chunk = json.loads(data_str)
                            anth_chunk = openai_to_anthropic_stream_chunk(openai_chunk, anthropic_body.get("model", "auto"))
                            if anth_chunk:
                                yield f'data: {json.dumps(anth_chunk)}\n\n'
                        except Exception as e:
                            add_log(logging.ERROR, f"流式转换错误: {e}")
                            continue

            response = Response(generate(), status=200)
            response.headers['Content-Type'] = 'text/event-stream'
            response.headers['Cache-Control'] = 'no-cache'
            return response
        else:
            # 非流式：转换完整响应
            openai_response = upstream_resp.json()
            anthropic_response = openai_to_anthropic(openai_response, anthropic_body.get("model", "auto"))
            return jsonify(anthropic_response)

    except requests.exceptions.RequestException as e:
        add_log(logging.ERROR, f"调用内部 chat completions 失败: {e}")
        return jsonify({"error": f"Internal proxy error: {str(e)}"}), 500


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

@app.route('/api/stats/token', methods=['GET'])
def get_token_stats():
    with token_stats_lock:
        stats = token_stats.copy()
    return jsonify(stats)

@app.route('/api/stats/token/reset', methods=['POST'])
def reset_token_stats():
    if not check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401
    with token_stats_lock:
        token_stats["total_tokens"] = 0
        token_stats["prompt_tokens"] = 0
        token_stats["completion_tokens"] = 0
        token_stats["request_count"] = 0
    add_log(logging.INFO, "Token 统计已被管理员重置")
    return jsonify({"success": True})

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    if not check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401
    with logs_lock:
        recent_logs.clear()
    add_log(logging.INFO, "日志已被管理员手动清空")
    return jsonify({"success": True})

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
    if priority not in [0, 1, 2]:
        priority = 0 if data.get("priority") == 0 else 1
    provider_type = data.get("provider_type")
    if not provider_type:
        provider_type = "google" if "googleapis" in endpoint else "openai"
    info = {
        "key": key_str,
        "models": data["selected_models"],
        "verified_models": data["selected_models"],
        "priority": priority,
        "source": "stable",
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

    if "name" in data and data["name"]:
        info["name"] = data["name"]
    if "endpoint" in data and data["endpoint"]:
        endpoint = data["endpoint"].rstrip("/")
        info["endpoint"] = endpoint
        if "provider_type" not in data or not data["provider_type"]:
            info["provider_type"] = "google" if "googleapis" in endpoint else "openai"
    if "priority" in data:
        priority = data["priority"]
        if priority in [0, 1, 2]:
            info["priority"] = priority
            info["source"] = "stable" if priority in (0,1) else "paid"
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
                "request_url": url
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
🚀 LLM 智能代理网关 v6.0 (最终稳定版) 启动成功！
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 代理地址:   http://{PROXY_HOST}:{PROXY_PORT}/v1
🔧 管理面板:   http://{PROXY_HOST}:{PROXY_PORT}/admin
📊 Token统计:  支持非流式请求累计
🕒 时区:       北京时间 (UTC+8)
💖 P0 公益:    手动添加优先级 0 的供应商，不会被爬虫覆盖
🔒 删除策略:   手动添加永不删除；社区 Key 智能删除（永久错误立即删，临时错误累计 3 次删）
⚖️ 调度优化:   同优先级下社区 Key 优先于手动添加
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
    serve(app, host=PROXY_HOST, port=PROXY_PORT, threads=10)
