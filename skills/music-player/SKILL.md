---
name: music-player
description: 搜索并播放音乐。从B站搜索歌曲、下载音频、用mpv播放器播放。支持缓存、队列、收藏、黑名单。当用户要求播放音乐、放歌、听歌时使用此skill。
---

# Music Player (mpv版)

搜索并播放音乐，从B站下载音频，使用mpv播放器播放。
mpv是命令行播放器，不弹窗、可精确控制进程。

## 依赖

- yt-dlp: `pip install yt-dlp`
- mpv播放器: 已安装在 `C:\Program Files\MPV Player\mpv.exe`

## 使用方式

脚本路径：`skills/music-player/music_player.py`（相对于workspace）

### 基本播放

```bash
python skills/music-player/music_player.py play "歌曲名"
python skills/music-player/music_player.py play "歌手 歌名"
python skills/music-player/music_player.py stop
python skills/music-player/music_player.py status
python skills/music-player/music_player.py next
```

### 播放流程

1. 用户说"播放xxx" → `play "xxx"`
2. 用户说"现在放的什么" → `status`
3. 用户说"停" → `stop`
4. 用户说"下一首" → `next`（需要队列中有歌）
5. 用户说"不喜欢这首" → `status` 获取歌名 → `dislike-song` → `stop` 或 `next`

### 收藏管理

```bash
python skills/music-player/music_player.py fav-song "歌名" "歌手"
python skills/music-player/music_player.py unfav-song "歌名"
python skills/music-player/music_player.py fav-artist "歌手"
python skills/music-player/music_player.py unfav-artist "歌手"
python skills/music-player/music_player.py fav-list
python skills/music-player/music_player.py fav-play
python skills/music-player/music_player.py fav-play-artist "歌手"
```

### 播放队列

```bash
python skills/music-player/music_player.py queue-add "歌名" "歌手"
python skills/music-player/music_player.py queue-list
python skills/music-player/music_player.py queue-play [--wait]
python skills/music-player/music_player.py queue-clear
python skills/music-player/music_player.py queue-stop
python skills/music-player/music_player.py queue-skip
```

队列播放时自动跳过黑名单歌曲。

### 播放监控

```bash
python skills/music-player/music_player.py monitor-start
python skills/music-player/music_player.py monitor-stop
```

### 黑名单（不喜欢）

```bash
python skills/music-player/music_player.py dislike-song "歌名" "歌手"
python skills/music-player/music_player.py undislike-song "歌名"
python skills/music-player/music_player.py dislike-artist "歌手"
python skills/music-player/music_player.py undislike-artist "歌手"
python skills/music-player/music_player.py dislike-list
```

### Python调用

```python
import sys
sys.path.insert(0, "skills/music-player")
from music_player import (
    search_and_play, stop_playing, get_status, next_song,
    play_file, _is_player_running,
    fav_add_song, fav_remove_song, fav_list,
    fav_add_artist, fav_remove_artist,
    fav_play_random_song, fav_play_artist,
    is_disliked, dislike_song, dislike_artist, dislike_list,
    queue_add, queue_list, queue_play, queue_clear,
)
```

## 技术细节

- 播放器：mpv（`--no-video --really-quiet --no-terminal`）
- mpv作为独立子进程运行，通过进程控制实现停止/状态检测
- stop命令会terminate子进程 + taskkill确保清理
- status通过检查mpv.exe进程判断是否在播放
- 音频缓存在 `skills/music_cache/`，同一首歌不重复下载
- 收藏：`skills/music-player/favorites.json`
- 黑名单：`skills/music-player/blacklist.json`
- 队列：`skills/music-player/queue.json`
- 播放状态：`skills/music-player/player_state.json`
- yt-dlp下载超时300秒
