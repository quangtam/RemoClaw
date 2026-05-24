# 🦞 RemoClaw

<p align="center">
  <img src="assets/logo.svg" alt="RemoClaw Logo" width="200" />
</p>

<p align="center">
  <strong>Điều khiển từ xa mọi AI coding agent ngay trên điện thoại của bạn.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/Telegram-bot-26A5E4.svg" alt="Telegram bot">
  <img src="https://img.shields.io/badge/agents-Kiro%20%C2%B7%20Claude%20%C2%B7%20Gemini%20%C2%B7%20Codex-7c3aed.svg" alt="Supported agents">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <strong>Tiếng Việt</strong>
</p>

<p align="center">
  <a href="docs/usage.md">Sử dụng</a> ·
  <a href="docs/voice.md">Voice</a> ·
  <a href="docs/architecture.md">Kiến trúc</a> ·
  <a href="docs/deployment-guide.md">Triển khai</a> ·
  <a href="docs/development-guide.md">Đóng góp</a>
</p>

---

## RemoClaw là gì?

Các AI coding agent của bạn (Kiro, Claude Code, Gemini, Codex) chạy trên máy dev. RemoClaw giúp bạn điều khiển chúng từ điện thoại — thông qua Telegram, với phản hồi streaming, voice in/out, và forward các quyết định (Y/n) về chat.

**Bạn sẽ dùng nó khi:**

- 🛋️ Review PR từ ghế sofa
- 🚌 Triage bug trên đường đi làm
- 🎤 Pair-program rảnh tay khi đang nấu cơm
- 🔀 Quản lý nhiều project song song qua các thread khác nhau

## Nó hoạt động thế nào?

<p align="center">
  <img src="assets/how-it-works.svg" alt="System diagram" width="800" />
</p>

Bạn gửi tin nhắn trên Telegram → RemoClaw forward sang AI agent đang chạy trên máy dev của bạn → phản hồi của agent stream ngược về điện thoại theo thời gian thực. Voice message, prompt quyết định (Y/n), và screenshot đều đi qua cùng đường ống này.

Chi tiết kỹ thuật xem [tài liệu kiến trúc](docs/architecture.md).

<p align="center">
  <img src="assets/RemoClaw.jpg" alt="RemoClaw on Telegram" width="360" />
</p>

## Bắt đầu nhanh

```bash
git clone https://github.com/quangtam/remoclaw.git
cd remoclaw
bash setup.sh           # Windows: setup.bat
./remoclaw start        # Windows: remoclaw start
```

Setup wizard sẽ tự lo Python venv, dependencies, Telegram token, và `.env`. Sau khi xong, bạn login CLI một lần (`kiro-cli login` / `claude login` / `gemini auth` / `codex login`) rồi bắt đầu chat.

> Cần setup thủ công hoặc kiểm soát chi tiết hơn? Xem [hướng dẫn triển khai](docs/deployment-guide.md).

## Agent được hỗ trợ

| Agent | Xác thực |
| ----- | -------- |
| [Kiro CLI](docs/setup-kiro.md) | `kiro-cli login` |
| [Claude Code](docs/setup-claude.md) | `claude login` |
| [Gemini CLI](docs/setup-gemini.md) | `gemini auth` |
| [OpenAI Codex](docs/setup-codex.md) | `codex login` |

Tất cả agent đều xác thực qua browser login trên máy host. RemoClaw dùng session local đó — **không cần API key** cho agent.

## Tính năng

<p align="center">
  <img src="assets/features.svg" alt="Features" width="800" />
</p>

- 📡 **Multi-agent** — driver pluggable cho Kiro, Claude Code, Gemini, Codex
- ⚡ **Streaming thời gian thực** — message edit progressive, UX kiểu ChatGPT
- 🎤 **Voice in/out** — Whisper transcription + TTS readback, có backend local miễn phí
- 📂 **Multi-project** — bind các thread vào project khác nhau, đổi nhanh bằng `/projects`
- ⚠️ **Decision forwarding** — CLI hỏi Y/n thì forward về chat, bạn trả lời → pipe ngược lại CLI
- 📸 **Screenshot tự động** — đường dẫn ảnh trong CLI output được gửi thành Telegram photo
- 🧵 **Concurrent sessions** — nhiều thread chạy song song, idle tự dọn dẹp
- 💾 **Config bền vững** — thread binding và preference sống sót qua bot restart

## Lệnh thường dùng

```text
/help                    Xem toàn bộ lệnh
/info                    Chi tiết session hiện tại
/sessions                Danh sách session đang chạy
/project /path/to/repo   Bind thread vào project
/provider claude         Đổi CLI provider
/voice                   Bật/tắt voice output
/git status              Chạy git status trên project
```

Reference đầy đủ → [`docs/usage.md`](docs/usage.md).

## Tài liệu

| Chủ đề | File |
| ------ | ---- |
| Sử dụng & toàn bộ lệnh | [docs/usage.md](docs/usage.md) |
| Cấu hình voice | [docs/voice.md](docs/voice.md) |
| Kiến trúc | [docs/architecture.md](docs/architecture.md) |
| Triển khai (systemd, Docker) | [docs/deployment-guide.md](docs/deployment-guide.md) |
| Thêm CLI provider mới | [docs/development-guide.md](docs/development-guide.md) |

## Giới hạn

- Mỗi Telegram token chỉ được 1 instance bot (nếu không sẽ 409 Conflict)
- Voice speed chính xác nhất trên OpenAI TTS; edge-tts chuyển sang dạng rate-percent
- Các AI IDE không có headless CLI (Cursor, Windsurf, VS Code) chưa được hỗ trợ — RemoClaw chỉ bridge tới CLI agent

## Giấy phép

[MIT](LICENSE) — tự do sử dụng, chỉnh sửa, và phân phối.
