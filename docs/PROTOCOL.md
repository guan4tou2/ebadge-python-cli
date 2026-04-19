# ZRun E87 電子吧唧 — BLE 協議完整說明

本文件整理 ZRun E87 徽章的 BLE 協議。基於對官方 Android APK (`com.zijun.zrun` v2.1.7)、hybridherbst/web-bluetooth-e87 以及實機抓包的反向工程。

---

## 1. 硬體 / 連線概況

- **晶片：** 杰里 (Jieli) JL 系列（RCSP 協議 + jl_fatfs 檔案系統）
- **螢幕：** 360 × 360 實體像素 / 368 × 368 畫面 buffer（圓形顯示區）
- **儲存：** 內部 FAT 檔案系統，可用空間實測約 2–4 MB
- **廣播名：** `E87` 或 `E87-xxxx` 前綴
- **連線：** BLE 5.0（macOS 端 address 為 CoreBluetooth UUID，不是 MAC）

### GATT 結構

| Service | 用途 |
|---|---|
| `0000AE00-...-0805F9B34FB` | **Jieli RCSP**（auth + 資料傳輸） |
| `C2E6FD00-E966-1000-8000-BEF9C223DF6A` | **9E 傳統協議**（控制、badge-info、狀態推送） |
| `0000180F-...-0805F9B34FB` | BLE 標準 Battery Service（存在但 **不填值**） |

### Characteristics

| UUID | 屬性 | 用途 |
|---|---|---|
| `0000AE01-...` | write-without-response | **write_ae01** — Jieli 認證、E87 格式資料寫入 |
| `0000AE02-...` | notify | AE02 認證通知 |
| `C2E6FD01-...` | notify | FD01 通知（9E frame） |
| `C2E6FD02-...` | write, write-w/o-response | **write_fd02** — 控制指令（9E frame） |
| `C2E6FD03-...` | notify, read, write | **request 通道**（app 的「請求設定類」） |
| `C2E6FD04-...` | write-without-response | - |
| `C2E6FD05-...` | notify | FD05 通知 |
| `00002A19-...` | read, notify | 標準 Battery Level（**此 firmware 永遠空值**） |

---

## 2. 兩種 frame 格式

協議混用 **9E 格式**（舊 qix library）與 **E87 格式**（Jieli RCSP），必須分清楚。

### 9E frame（用於 C2E6FD01/02/03/05）

```
9E [checksum] [flag] [cmd] [len_lo] [len_hi] [payload...]
```

- `checksum` = `(flag + cmd + len_lo + len_hi + sum(payload)) & 0xFF`
- `flag` 位元（來自 `BTCommandManager.getFlagStatus`）：
  - bit 7：**request** flag（`sendRequestConfigCommandData` = 0x80）
  - bit 6-3：serial number（0-15 循環）
  - bit 2：long flag（payload + 6 > 20）
  - bit 1：**always 1**
  - bit 0：encryption（未見使用）

典型 flag：
- 正常寫：`0x02` + (serial<<3)
- request 寫：`0x82` + (serial<<3)
- 回應推送：`0x00`、`0x08`、`0x09`（device 產生）

### E87 frame（用於 AE01/AE02，Jieli RCSP）

```
FE DC BA [flag] [cmd] [len_hi] [len_lo] [body...] EF
```

- **大端** len
- 固定前綴 `FE DC BA`、固定後綴 `EF`
- `flag = 0xC0`（app→device request）
- Body = `[seq, ...params]` 或 `[seq, status, ...params]`（response）

---

## 3. 認證（Jieli RCSP）

**必須先完成 auth，否則所有 RCSP 命令（含 upload、bind、battery）都沒回應。**

```
→ write_ae01  [0x00 | 16 byte random]                     # TX step1
← notify      [0x01 | 16 byte device response]            # RX step1 (17 bytes)

→ write_ae01  [0x02, 0x70, 0x61, 0x73, 0x73]              # TX step2 "pass"
← notify      [0x00 | 16 byte device challenge]           # RX step2 (17 bytes)

→ write_ae01  [encrypted_response via jl_auth]            # TX step3
← notify      [0x02, 0x70, 0x61, 0x73, 0x73]              # RX step3 "pass"
```

加密演算法 AES-ECB：見 `ebadge_cli/jl_auth.py`。

---

## 4. 上傳流程（單圖 / AVI）

完整流程共 10 階段，見 `rcsp_transfer.py::run_e87_upload`。

| Phase | 動作 | Frame |
|---|---|---|
| 1 | cmd 0x06 重置 auth flag | AE01 E87 `C0 06 02 00 01` + FD02 `9E BD 0B 60 0D 00 03` |
| 2 | 時間同步 via FD02 | `9E 45 08 02 07 00 [year_l year_h mon day 00 hour min]` |
| 3 | cmd 0x03 `GetTargetInfo`（device 回版本資訊） | AE01 `C0 03 [seq FF FF FF FF 01]` |
| 4 | cmd 0x07 `GetSysInfo`（device 回屬性集合） | AE01 `C0 07 [seq FF FF FF FF FF]` |
| 5 | FD02 bootstrap：cmd 0x29 / 0xC6 / 0xDC | **觸發裝置推送狀態（含電量 cmd 0x27）** |
| 6 | cmd 0x21 begin upload | AE01 `C0 21 [seq 00]` |
| 7 | cmd 0x27 transfer params | AE01 `C0 27 [seq 00 00 00 00 02 01]` |
| 8 | cmd 0x1B file metadata（size + CRC16 + 路徑） | AE01 `C0 1B [...]` |
| 9 | cmd 0x01 windowed data transfer | AE01，每窗 4096B，裝置 `0xBB` ack |
| 10 | cmd 0x20 路徑回應 + cmd 0x1C finalize | 狀態 byte 0x00 = 成功 |

### 檔案格式

- **圖片**：JPEG（**≤ 16000 bytes**，否則設備拒收）。`image_converter.encode_badge_jpeg` 用品質 bracketing 壓到這個大小內
- **影片/動畫**：AVI MJPEG, 368×368, fps 建議 12（可選 24/16/12/8）。單幀用固定品質 85（不要 bracketing，否則品質過低會被 firmware 拒收回傳 `Device error 0x01`）
- **總大小上限**：裝置剩餘空間（見第 5 節 BadgeInfo）

### 路徑命名

```
\u555c{YYYYMMDDHHMMSS}.jpg   (image / qr)
\u555c{YYYYMMDDHHMMSS}.avi   (video)
```
UTF-16LE 編碼 + NULL 結尾。

---

## 5. BadgeInfo（無需 auth 的查詢）

唯一在 **pre-auth** 就能用的查詢指令。走 FD02，9E 格式：

**TX：** `9E D3 0B C6 01 00 01`（cmd 0xC6 payload=[0x01]）
**RX：** cmd 0xC7，payload 13 bytes：

```
[0] valid (=1)
[1-2]  width         LE  (實體像素)
[3-4]  height        LE
[5-6]  pictureWidth  LE  (畫面 buffer)
[7-8]  pictureHeight LE
[9-12] memory        LE  (單位 KB ← Android 顯示用)
```

**注意：** CLI 早期版本誤解為 BE 導致回 1.2 GB，正確是 **LE + 單位 KB**。

---

## 6. 電量查詢（關鍵！）

**此 firmware 不接受獨立的 cmd 0x27 查詢指令**。解法：複製 upload 的 Phase 1–5，裝置在 bootstrap 完成後會**主動推送** cmd 0x27（含電量）。

流程（見 `rcsp_probe.probe_battery`）：

1. 連線 + AE01 完整 **auth**
2. Phase 1（cmd 0x06）
3. Phase 2（時間同步）
4. Phase 3（cmd 0x03）
5. Phase 4（cmd 0x07）
6. Phase 5：寫 `9E B5 0B 29 01 00 80` 到 FD02
7. 監聽 notify，會收到 9E frame cmd 0x27：
   ```
   payload[0] = charge mode (0 = 放電)
   payload[1] = battery percentage
   ```

其他嘗試過的路徑（**全部失敗**）：
- pre-auth cmd 0x27 via FD02：無回應
- post-auth cmd 0x27 via FD02/FD03/AE01：無回應
- BLE 標準 Battery Service `0x180F / 0x2A19`：read 空值、無 notify push
- cmd 0x07 GetSysInfo attr type=0 data：永遠返回 0x00（假值）

---

## 7. bind / serial / firmware version

**此 firmware 沒開** 獨立 bind 查詢。試過：

- pre-auth cmd 0x60 → 無回應
- post-auth cmd 0x60 via AE01 (E87 格式) → 無回應
- post-auth 9E bind frame via FD02 → 只回 1-byte ACK `0x02`，不是完整 bind response

→ **OTA 無解**：Android 端 `ota_api.py` 需要 `device_serial_num` + `firmware_version`，兩者皆由 bind response 提供，但此 firmware 不給。用 fake 值打雲端 API 回 `CD000003 處理失敗`。

對應的模組 `ota_api.py`、`ota_update.py`、`rcsp_probe.probe_device()` 保留在源碼，供其他 firmware 使用。

---

## 8. 不支援功能清單（此 firmware）

| 功能 | 狀態 | 原因 |
|---|---|---|
| OTA 韌體更新 | ✗ | 需 bind（fw/serial）才能查雲端 |
| 時間同步獨立指令 | ✗ | cmd 0x02 pre-auth 不回應 |
| 綁定 / 解綁 | ✗ | cmd 0x60 / 0x62 不回應 |
| 鬧鐘、健康設定等 | ✗ | qix library 設定族群全部不回應 |

可動：**媒體上傳（單圖/多圖/影片/GIF/文字/QR/圖案）**、**badge-info**、**電量（需 bootstrap）**。

---

## 9. 參考來源

本文件的協議還原建立在以下工作之上：

- **[hybridherbst/web-bluetooth-e87](https://github.com/hybridherbst/web-bluetooth-e87)** — Web Bluetooth 版 E87 上傳工具。**第 4 節的 10-phase 上傳流程主要是從這個 repo 反向推導得出**，包括 auth 握手時序、phase 順序、FD02 bootstrap 指令序列、windowed data transfer 的視窗大小。若無此 repo，實作時間可能要多數倍。
- Android app `com.zijun.zrun` v2.1.7（`com/qix/library/`, `com/jieli/jl_rcsp/`）— 9E 協議、BadgeInfo、電量推送觸發點的來源
- Jieli `jl_rcsp-3.0.aar` SDK — RCSP 指令、AttrBean / TargetInfoResponse 結構規格
- 實機 HCI 及 bleak notify 流量分析 — 交叉驗證上述來源
