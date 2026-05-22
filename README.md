# LLM-Proxy-Gateway

> 一个能自动抓取、智能调度免费大模型API，并支持热备用瞬间切换的零成本、高可用网关。

## ✨ 核心特性

- 🕷️ **多源爬虫**：定时从社区中转站抓取免费Key，数据源可在Web面板动态增删。
- ⚡ **热备用瞬间切换**：预维护备用Key，当前Key失效时零延迟接管，用户无感知。
- 🧠 **智能模型探测**：启动时自动探测每个Key的可用模型，请求时按优先级自动选择。
- 📊 **三级优先级调度**：P0(社区中转) → P1(官方免费) → P2(付费兜底)，优先消耗不稳定的免费资源。
- 🖥️ **Web管理面板**：供应商增删改查、数据源管理、分页日志、系统状态监控，Key加密存储。
- 🔌 **OpenAI兼容API**：提供 `/v1/chat/completions`、`/v1/models` 等标准接口，无缝对接AstrBot、OpenClaw等客户端。
- 🌐 **原生支持Google Gemini**：无需额外转换服务，自动适配谷歌原生格式。
- 📝 **请求日志与统计**：按天轮转日志，内存保留最近200条，支持分页查询。

## 📊 架构概览

```
┌─────────────┐
│  AstrBot /  │
│  OpenClaw   │  ── model: "auto"
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────────────────┐
│              proxy.py (单文件)                │
│                                              │
│  ┌──────────┐    ┌────────────────────────┐  │
│  │ 爬虫线程 │───▶│  内存 KeyPool (优先级)  │  │
│  │(定时抓取) │    │  P0: 中转 / P1: 免费   │  │
│  └──────────┘    │  P2: 付费              │  │
│                   └───────────┬────────────┘  │
│                               │               │
│                   ┌───────────▼────────────┐  │
│                   │   热备用调度器 + 粘性   │  │
│                   │   active / backup key  │  │
│                   └───────────┬────────────┘  │
│                               │               │
│                   ┌───────────▼────────────┐  │
│                   │   格式适配 & 请求转发   │  │
│                   │   (OpenAI / Google)    │  │
│                   └───────────┬────────────┘  │
│                               │               │
│                   ┌───────────▼────────────┐  │
│                   │    Flask + Waitress    │  │
│                   │   对外开放 API          │  │
│                   └────────────────────────┘  │
└──────────────────────────────────────────────┘
```

## 🚀 快速开始

### 环境要求
- Python 3.8+
- pip

### 1. 下载代码
```bash
git clone <仓库地址> /opt/llm-proxy
cd /opt/llm-proxy
```

### 2. 安装依赖
```bash
python3 -m venv venv
source venv/bin/activate
pip install flask requests waitress cryptography
```

### 3. 启动服务
```bash
# 后台运行
nohup python proxy.py > proxy.output.log 2>&1 &
```

或者使用 systemd 守护（推荐）：
```bash
sudo cat > /etc/systemd/system/llm-proxy.service << EOF
[Unit]
Description=LLM API Proxy Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/llm-proxy
Environment="PATH=/opt/llm-proxy/venv/bin"
ExecStart=/opt/llm-proxy/venv/bin/python /opt/llm-proxy/proxy.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now llm-proxy
```

默认服务监听 `http://127.0.0.1:8800`。

### 4. 配置 Nginx 反向代理（可选）
创建 `api.yourdomain.com` 站点，配置SSL后添加反向代理：
```nginx
location / {
    proxy_pass http://127.0.0.1:8800;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### 5. 接入机器人
在 AstrBot / OpenClaw 提供商设置中：
- **API 地址**：`https://api.yourdomain.com/v1`
- **API Key**：任意填写（本项目默认不验证）
- **模型**：选择 `auto` （列表第一个）

## 🔧 管理面板
访问 `https://api.yourdomain.com/admin` 可以：
- 查看系统运行状态、Key池统计、请求成功率
- 增删改查手动供应商（P1/P2）
- 探测可用模型、修改优先级
- 管理爬虫数据源（P0来源）
- 查看分页请求日志

**注意**：管理面板默认无认证，建议搭配Nginx IP白名单或开启环境变量认证。

## 🔌 API 接口
| 接口 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 聊天补全（OpenAI兼容） |
| `/v1/models` | GET | 获取可用模型列表（包含auto） |
| `/health` | GET | 健康检查与系统状态 |
| `/api/providers` | GET/POST | 供应商列表/添加 |
| `/api/providers/detect` | POST | 探测端点可用模型 |
| `/api/providers/<hash>` | PUT/DELETE | 修改/删除供应商 |
| `/api/sources` | GET/POST | 数据源列表/添加 |
| `/api/sources/<index>` | DELETE | 删除数据源 |
| `/api/logs` | GET | 分页请求日志（`?page=1&limit=50`） |

## 🛡️ 安全配置（可选）
通过环境变量可开启管理面板的Basic认证和API Token验证：
```bash
export ENABLE_ADMIN_AUTH=true
export ADMIN_USERNAME=your_name
export ADMIN_PASSWORD=your_password

export ENABLE_API_AUTH=true
export API_ACCESS_TOKEN=your_secret_token
```
若开启API认证，机器人配置的API Key需填写该Token。

## 📝 如何添加免费Key？
本项目内置了一个默认的社区中转站数据源，会自动抓取 `deepseek-chat` 等模型的免费Key。你也可以在管理面板中手动添加官方免费API：

| 平台 | 每日免费额度 | 接口地址 |
|------|-------------|---------|
| Google Gemini | 1500次/天 | `https://generativelanguage.googleapis.com/v1beta` |
| 硅基流动 (SiliconFlow) | 2000万Tokens(永久) | `https://api.siliconflow.cn/v1` |
| Groq | 14400次/天 | `https://api.groq.com/openai/v1` |
| OpenRouter | 200次/天 | `https://openrouter.ai/api/v1` |
| Cerebras | 100万Tokens/天 | `https://api.cerebras.ai/v1` |

将申请到的Key填入管理面板，系统会自动按P0→P1→P2优先级调度。

## 🛠️ 常用管理命令
```bash
# 查看进程
ps aux | grep proxy.py

# 重启服务
kill $(pgrep -f proxy.py)
cd /opt/llm-proxy && source venv/bin/activate && nohup python proxy.py > proxy.output.log 2>&1 &

# 查看日志
tail -f /opt/llm-proxy/proxy.output.log

# 查看实时请求日志（容器/进程内）
curl http://127.0.0.1:8800/api/logs?limit=20
```

## 📄 项目文件说明
```
/opt/llm-proxy/
├── proxy.py              # 主程序
├── templates/
│   └── admin.html        # Web管理面板
├── providers.json        # 手动供应商数据（加密存储Key）
├── sources.json          # 爬虫数据源配置
├── .secret_key           # 加密密钥（自动生成，勿泄露）
└── proxy.log             # 运行日志（按天轮转）
```

## 🤝 贡献与反馈
欢迎提交Issue和PR，一起让免费大模型惠及更多人。

## 📜 开源协议
MIT License

---

**祝你薅羊毛愉快！** 🐑