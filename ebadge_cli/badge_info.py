"""BadgeInfo 解析與請求。

基於 ReceiveCommandParse.badgeInfoReturnParse 的 payload 格式：
- bArr[0]==1: 有效
- width:         [1-2] LE
- height:        [3-4] LE
- pictureWidth:  [5-6] LE
- pictureHeigh:  [7-8] LE
- memory:        [9-12] LE (單位: KB)  ← Java 端用 getMemory() + "KB" 顯示
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class BadgeInfo:
    """吧唧顯示參數。"""

    width: int
    height: int
    picture_width: int
    picture_height: int
    memory: int  # 單位: KB (剩餘可用儲存)

    @property
    def memory_bytes(self) -> int:
        return self.memory * 1024

    @property
    def memory_mb(self) -> float:
        return self.memory / 1024.0

    @property
    def resolution(self) -> tuple[int, int]:
        """建議的圖片解析度 (寬, 高)。"""
        w = self.picture_width if self.picture_width > 0 else 368
        h = self.picture_height if self.picture_height > 0 else 368
        return (w, h)


def parse_badge_info(payload: list[int]) -> Optional[BadgeInfo]:
    """解析 BadgeInfo 回應 payload (cmd 0xC7)。"""
    if len(payload) < 13 or payload[0] != 1:
        return None
    width = payload[1] | (payload[2] << 8)
    height = payload[3] | (payload[4] << 8)
    picture_width = payload[5] | (payload[6] << 8)
    picture_height = payload[7] | (payload[8] << 8)
    # memory 也是 LE (和 width/height 同格式)，單位 KB
    memory = (payload[9] | (payload[10] << 8)
              | (payload[11] << 16) | (payload[12] << 24))
    return BadgeInfo(
        width=width,
        height=height,
        picture_width=picture_width,
        picture_height=picture_height,
        memory=memory,
    )
