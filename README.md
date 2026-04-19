# ebadge-cli — ZRun E87 電子吧唧 Python 工具

反向工程 ZRun E87 徽章的 BLE 協議，提供 **CLI** 與 **PySide6 GUI** 操作介面。

> 協議細節見 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

---

## 功能

| 功能 | CLI | GUI | 備註 |
|---|---|---|---|
| 掃描 / 連線 E87 | ✓ | ✓ | GUI 掃到就自動連 |
| 裝置資訊（解析度、剩餘空間） | ✓ (`badge-info`) | ✓ | 連線後自動查 |
| 電量查詢 | ✗ | ✓ | 需完整 RCSP bootstrap（見協議） |
| 單張圖片上傳 | ✓ (`push-image`) | ✓ | fit 模式 + 縮放 |
| 多張輪播（slideshow） | ✓ (`push-images`) | ✓ | 每張獨立 fit / zoom / 時長 |
| 影片 / GIF 上傳 | ✓ (`push-video`) | ✓ | FPS 24/16/12/8，品質高/中/低 |
| 文字動畫 | ✓ (`push-danmaku`) | ✓ | 滾動 / 靜置 / 圓形旋轉 / 脈衝 / 波浪 |
| QR Code | ✓ (`push-qr`) | ✓ | 前/背景色、縮放 |
| 內建圖案動畫 | ✓ (`push-pattern`) | ✓ | gradient / pulse / checker / rainbow / wave |
| WYSIWYG 368×368 圓形預覽 | - | ✓ | 上傳前即時看結果 |
| 上傳前空間預檢 | - | ✓ | 超過裝置剩餘自動擋下 |

---

## 安裝（使用 [uv](https://github.com/astral-sh/uv)）

```bash
git clone https://github.com/guan4tou2/ebadge-python-cli.git
cd ebadge-python-cli
uv sync
```

`uv sync` 會依 `uv.lock` 建立 `.venv` 並裝好 PySide6 / bleak / Pillow / qrcode / cryptography。

> macOS 需首次執行時授權 Bluetooth（系統偏好設定 → 隱私權 → 藍牙）。
> 影片/GIF 上傳需系統安裝 `ffmpeg`（`brew install ffmpeg`）。

---

## 使用

### GUI

```bash
uv run ebadge-gui
# 或
.venv/bin/python -m ebadge_cli.gui_qt
```

### CLI

```bash
uv run ebadge-cli --help

# 掃描
uv run ebadge-cli scan --timeout 5

# 上傳圖片
uv run ebadge-cli push-image --file pic.png --name E87

# 上傳影片
uv run ebadge-cli push-video --file clip.mp4 --name E87 --duration 10 --fps 12

# 上傳文字（跑馬燈）
uv run ebadge-cli push-danmaku --text "Hello E87" --duration 5

# 查詢裝置
uv run ebadge-cli badge-info --name E87
```

---

## 專案結構

```
ebadge-python-cli/
├── ebadge_cli/
│   ├── __main__.py         CLI 進入點
│   ├── cli.py              argparse + 各 subcommand
│   ├── gui_qt.py           PySide6 GUI
│   ├── ble_constants.py    GATT service / characteristic UUID
│   ├── ble_session.py      bleak 包裝（scan / run_session / run_write_only）
│   ├── badge_frame.py      9E frame 編解碼
│   ├── rcsp_frame.py       E87 frame 編解碼 (FE DC BA ... EF)
│   ├── jl_auth.py          Jieli RCSP AES 認證
│   ├── rcsp_transfer.py    完整 10-phase 上傳流程
│   ├── rcsp_probe.py       auth + bootstrap + 電量/bind 查詢
│   ├── image_converter.py  圖片 / 影片 / 彈幕 / QR / pattern → JPEG/AVI
│   ├── avi_builder.py      MJPEG AVI 容器組裝
│   ├── crc16.py            CRC16 (檔案完整性)
│   ├── badge_info.py       BadgeInfo cmd 0xC6/0xC7 解析
│   ├── bind_response.py    bind cmd 0x61 response 解析
│   ├── battery_command.py  legacy battery cmd 0x27
│   ├── ota_api.py          OTA 雲端 API（RSA+AES header）
│   ├── ota_update.py       OTA 韌體推送流程
│   └── file_browse.py      裝置檔案列表
├── docs/
│   └── PROTOCOL.md         完整協議說明
├── tests/                  pytest 單元測試
├── pyproject.toml          uv 專案定義
├── uv.lock                 鎖定版本
└── README.md
```

---

## 已確認限制（此韌體版本）

基於實測，此批 E87 firmware **不開放**：
- OTA 更新（bind response 不回 serial/fw，雲端 API 無從查詢）
- 時間同步 / 綁定 / 解綁等 qix legacy 設定族群

**可用功能：** 所有媒體上傳 + badge-info + 電量（bootstrap 後）。

其他 firmware 版本可能完整支援 OTA 等功能，相關模組保留在源碼可直接使用。

---

## 測試

```bash
uv run pytest
```

---

## License

MIT
