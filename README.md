# OpenCode Go 桥接

让 **WorkBuddy** 客户端能用上 **OpenCode Go** 订阅里的模型。

一个单文件 Node 代理，无第三方依赖。它解决的问题很小：客户端只让你填三格，而 OpenCode Go 非要一个请求头。

> 非官方项目：与 WorkBuddy、OpenCode 两边都没有隶属关系，只是把两个公开接口接起来。
>
> **只用于 WorkBuddy 客户端。** 这个代理只为补一个缺口而存在 —— WorkBuddy 的「自定义模型」只给三格、没地方填请求头。OpenCode 自家的客户端和 CLI 本来就能带 `x-opencode-session`，不需要这一层，它也不是给它们设计的；拿去做通用 OpenCode 代理会缺功能。许可证只覆盖本仓库里我写的代理脚本与文档，全文见 [LICENSE](LICENSE)。

## 目录

- [为什么要这一层](#为什么要这一层)
- [请求怎么走](#请求怎么走)
- [安装](#安装)
- [跑不起来时](#跑不起来时)
- [换端口 / 换上游](#换端口--换上游)
- [安全边界](#安全边界)
- [文件](#文件)
- [日志不进库](#日志不进库)
- [环境要求](#环境要求)
- [许可证](#许可证)

---

## 为什么要这一层

WorkBuddy 的「自定义模型」只有三个输入框：**接口地址、API Key、模型名称**，没有填请求头的入口。

而 OpenCode Go 的 `/inference/go/*` 端点强制要求请求头 `x-opencode-session`，缺了它在鉴权之前就被拒，返回 `400 MissingSessionID`。

所以中间得加一层：收下客户端的请求，补上这个头，再转给上游。

## 请求怎么走

蓝点是请求，青点是响应。代理除补头之外还做三件事，都在本机完成：定会话身份（多个对话不挤到同一条上游会话）、补思考字段（DeepSeek 思考模式缺 `reasoning_content` 会整条被拒）、留一份失败转储方便排查。

数据流图见 [`docs/architecture.svg`](docs/architecture.svg)。

## 安装

需要 **Node.js 18+**（WorkBuddy 自带的 node 也能用，脚本会自己找）。

```bash
git clone https://github.com/foryys/workbuddy-z-opencode-bridge.git
cd workbuddy-z-opencode-bridge
```

Windows 双击 `启动代理.cmd`，会开一个终端窗口，显示：

```
OpenCode Go 启动器 v4.5.x
listening http://127.0.0.1:8787
```

**这个窗口要一直开着**，关掉就断。最小化可以。

然后在 WorkBuddy 里：**左下角头像 → 设置 → 模型 → 自定义模型 → 添加模型**，提供商选「自定义」，填：

| 字段 | 值 |
|---|---|
| 接口地址 | `http://127.0.0.1:8787/v1/chat/completions` |
| API Key | 你在 OpenCode Console 建的 Key |
| 模型名称 | `deepseek-v4.1-flash` |

模型名填**裸 id**，不要带 `opencode-go/` 前缀 —— 那个前缀是 OpenCode 客户端配置文件里的写法，直接调接口只认裸名。

高级选项建议勾上：工具调用、图片输入、思考模式、允许关闭思考；思考强度选「中」。

## 跑不起来时

| 现象 | 含义 | 处理 |
|---|---|---|
| `400 MissingSessionID` | 请求没经过代理 | 检查地址是不是 `127.0.0.1:8787`，代理窗口还开着吗 |
| `401 Invalid credential` | 到了上游，Key 不对 | 重新复制 Key，注意首尾空格 |
| `402 Insufficient account funds` | 地址填成了按量计费那个端点 | 换回 `127.0.0.1:8787` |
| `403 error code 1010` | Cloudflare 拦 UA，说明是直连上游 | 同上，请求没走代理 |
| 连接被拒绝 | 代理没起来 | 双击启动脚本 |
| 端口被占用 | 8787 被别的程序占了 | 见下 |

## 换端口 / 换上游

不用改代码，环境变量即可：

```bash
set PORT=8788
set UPSTREAM_BASE=https://opencode.ai/inference/go
node opencode-go-proxy.mjs
```

可调项在脚本开头的注释里列全了：`PROXY_URL`（出站代理，默认 `http://127.0.0.1:7890`，填 `direct` 直连）、`FIRST_BYTE_TIMEOUT_MS`、`PROXY_RETRIES`、`KEEPALIVE_TIMEOUT_MS` 等。

**出站走不走本地代理**：有些网络直连上游抖动很大，走本地代理更稳。脚本启动时探测一次那个端口，通了就用隧道，不通就直连 —— 不会每个连接白试一遍。**本机没有代理也不影响使用**，探测失败会自动回退直连。

## 安全边界

- **不记录 Key**：日志里只有状态码、路径、耗时、session 前 8 位。
- **只监听 `127.0.0.1`**：局域网和公网都访问不到。
- **出站只有一个目标**：`UPSTREAM_BASE` 指向的那一个域名，Key 只发给它的签发方。
- **读本地会话库是只读**：为了让窗口里的任务名和客户端一致，会以只读方式查一次会话标题。不写、不外传。
- **状态都在同目录**：`proxy.log`、`reasoning-cache.json`、`proxy-fail-*.json`，全部在 `.gitignore` 里，不会被提交。

## 文件

```
opencode-go-proxy.mjs   代理主体，单文件
启动代理.cmd            双击启动（简版）
start-proxy.cmd         启动（带端口占用检查与 node 三级查找）
说明.md                 原始中文说明，含更细的排错
docs/architecture.svg   数据流图
```

## 日志不进库

`proxy.log`、`proxy-fail-*.json`、`reasoning-cache.json` 一律留在本机，已写进 `.gitignore`。
这些文件里有请求原文与会话内容，**不随仓库发布**。

## 环境要求

- **Node.js 18+**（18 以下用不了 `node:sqlite`，会话标题会退化为按正文猜，其余功能正常）
- 仅面向 **WorkBuddy** 客户端；桥接本身是通用的 OpenAI 兼容代理，理论上别的客户端也能用，但没有为此测试

## 许可证

**署名—非商业性使用—相同方式共享 4.0**（CC BY-NC-SA 4.0）。全文见 [LICENSE](LICENSE)。

要商用请另外联系我。

## 使用范围

本项目仅用于协议学习、研究与个人自用，禁止商业转售。具体授权条款见 [LICENSE](LICENSE)。
