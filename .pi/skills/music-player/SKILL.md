---
name: music-player
description: 搜索并播放音乐。从B站搜索歌曲、下载音频、用系统播放器播放。支持缓存已下载的歌曲。当用户要求播放音乐、放歌、听歌时使用此skill。
---

# Music Player

搜索并播放音乐，从B站下载音频并用系统播放器播放。

## 依赖

需要 yt-dlp：
```bash
pip install yt-dlp
```

## 使用方式

### 搜索并播放歌曲

```bash
python .pi/skills/music-player/music_player.py play "歌曲名"
python .pi/skills/music-player/music_player.py stop
```

### 收藏管理

```bash
# 收藏歌曲（可选带歌手名）
python .pi/skills/music-player/music_player.py fav-song "乌兰巴托的夜" "谭维维"

# 取消收藏歌曲
python .pi/skills/music-player/music_player.py unfav-song "乌兰巴托的夜"

# 收藏音乐家/歌手
python .pi/skills/music-player/music_player.py fav-artist "毛不易"

# 取消收藏音乐家
python .pi/skills/music-player/music_player.py unfav-artist "毛不易"

# 查看所有收藏
python .pi/skills/music-player/music_player.py fav-list

# 随机播放一首收藏的歌
python .pi/skills/music-player/music_player.py fav-play

# 播放收藏的音乐家的歌（可指定名字或随机）
python .pi/skills/music-player/music_player.py fav-play-artist "毛不易"
```

### 播放队列

```bash
# 添加歌曲到队列
python .pi/skills/music-player/music_player.py queue-add "隐形的翅膀" "张韶涵"
python .pi/skills/music-player/music_player.py queue-add "遗失的美好" "张韶涵"

# 查看队列
python .pi/skills/music-player/music_player.py queue-list

# 播放队列（立即开始）
python .pi/skills/music-player/music_player.py queue-play

# 播放队列（等当前歌播完后开始）
python .pi/skills/music-player/music_player.py queue-play --wait

# 跳过当前歌，播下一首
python .pi/skills/music-player/music_player.py queue-skip

# 清空队列
python .pi/skills/music-player/music_player.py queue-clear

# 停止队列播放
python .pi/skills/music-player/music_player.py queue-stop
```

### 播放监控

```bash
# 启动监控：检测到当前歌播完后自动播放队列下一首
python .pi/skills/music-player/music_player.py monitor-start

# 停止监控
python .pi/skills/music-player/music_player.py monitor-stop
```

监控模式会每秒检测播放器状态，一旦发现播放器关闭就自动播放队列中的下一首歌。用法：先往队列加歌，再启动监控即可。

### 不喜欢管理

```bash
# 标记不喜欢的歌曲（会自动从收藏移除）
python .pi/skills/music-player/music_player.py dislike-song "晴天" "周杰伦"

# 取消不喜欢
python .pi/skills/music-player/music_player.py undislike-song "捴天"

# 标记不喜欢的音乐家（会自动从收藏移除）
python .pi/skills/music-player/music_player.py dislike-artist "周杰伦"

# 取消不喜欢音乐家
python .pi/skills/music-player/music_player.py undislike-artist "周杰伦"

# 查看不喜欢列表
python .pi/skills/music-player/music_player.py dislike-list
```

### 在代码中调用

```python
import sys
sys.path.insert(0, ".pi/skills/music-player")
from music_player import (
    search_and_play, stop_playing,
    fav_add_song, fav_remove_song,
    fav_add_artist, fav_remove_artist,
    fav_list, fav_play_random_song, fav_play_artist,
)
```

## 说明

- 音频缓存在 `music_cache/` 目录，同一首歌不会重复下载
- 收藏数据保存在 `favorites.json` 文件中
- 不喜欢数据保存在 `blacklist.json` 文件中
- 标记不喜欢时会自动从收藏中移除
- 随机播放收藏时会跳过不喜欢的内容
- 使用系统默认播放器播放音频
- 从B站搜索，无需YouTube访问
- 播放新歌前会自动停止之前的播放
