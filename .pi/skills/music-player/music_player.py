"""
音乐搜索与播放模块
- 从B站搜索歌曲并下载音频
- 使用系统播放器播放
- 支持播放队列、收藏、不喜欢
"""

import os
import re
import subprocess
import sys
import urllib.request
import threading


# 当前播放进程
_current_player = None
_player_lock = threading.Lock()

# 播放队列
_queue_lock = threading.Lock()
_queue_thread = None
_queue_stop = threading.Event()
QUEUE_FILE = os.path.join(os.path.dirname(__file__), "queue.json")

# 播放监控
_monitor_thread = None
_monitor_stop = threading.Event()
_on_playback_finished = None  # 播放结束回调


def _load_queue() -> list:
    import json
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_queue(q: list):
    import json
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)


def _is_player_running() -> bool:
    """检测系统播放器是否还在运行"""
    if sys.platform == "win32":
        players = ["PotPlayerMini64.exe", "PotPlayerMini.exe", "wmplayer.exe",
                   "MediaPlayer.exe", "Music.UI.exe", "vlc.exe", "mpv.exe"]
        for p in players:
            r = subprocess.run(["tasklist", "/fi", f"IMAGENAME eq {p}"],
                             capture_output=True, text=True, encoding="gbk", errors="replace")
            if p.lower().replace(".exe", "") in r.stdout.lower():
                return True
    return False


def stop_playing():
    """停止当前正在播放的音乐"""
    global _current_player
    with _player_lock:
        if _current_player is not None:
            try:
                _current_player.terminate()
                _current_player.wait(timeout=3)
            except Exception:
                try:
                    _current_player.kill()
                except Exception:
                    pass
            _current_player = None
    # Windows: 关闭可能由 os.startfile 打开的播放器
    if sys.platform == "win32":
        for player in ["PotPlayerMini64.exe", "PotPlayerMini.exe", "wmplayer.exe",
                       "msedge.exe", "Music.UI.exe", "MediaPlayer.exe",
                       "vlc.exe", "mpv.exe"]:
            try:
                # 循环杀直到没有残留
                for _ in range(5):
                    r = subprocess.run(["taskkill", "/f", "/im", player],
                                     capture_output=True, text=True, encoding="gbk",
                                     errors="replace", timeout=5)
                    if r.returncode != 0:
                        break
            except Exception:
                pass


MUSIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "music_cache")
os.makedirs(MUSIC_DIR, exist_ok=True)

# 收藏数据文件
FAVORITES_FILE = os.path.join(os.path.dirname(__file__), "favorites.json")


def _load_favorites() -> dict:
    """加载收藏数据"""
    import json
    if os.path.exists(FAVORITES_FILE):
        try:
            with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"songs": [], "artists": []}


def _save_favorites(data: dict):
    """保存收藏数据"""
    import json
    with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fav_add_song(song_name: str, artist: str = "") -> str:
    """收藏一首歌曲，返回结果描述"""
    data = _load_favorites()
    for s in data["songs"]:
        if s["name"] == song_name and s.get("artist", "") == artist:
            return f"'{song_name}' 已经在收藏里了"
    data["songs"].append({"name": song_name, "artist": artist})
    _save_favorites(data)
    return f"已收藏歌曲 '{song_name}'"


def fav_remove_song(song_name: str) -> str:
    """取消收藏一首歌曲"""
    data = _load_favorites()
    before = len(data["songs"])
    data["songs"] = [s for s in data["songs"] if s["name"] != song_name]
    if len(data["songs"]) < before:
        _save_favorites(data)
        return f"已取消收藏 '{song_name}'"
    return f"收藏里没有 '{song_name}'"


def fav_add_artist(artist_name: str) -> str:
    """收藏一位音乐家/歌手"""
    data = _load_favorites()
    if artist_name in data["artists"]:
        return f"'{artist_name}' 已经在收藏里了"
    data["artists"].append(artist_name)
    _save_favorites(data)
    return f"已收藏音乐家 '{artist_name}'"


def fav_remove_artist(artist_name: str) -> str:
    """取消收藏一位音乐家/歌手"""
    data = _load_favorites()
    if artist_name in data["artists"]:
        data["artists"].remove(artist_name)
        _save_favorites(data)
        return f"已取消收藏 '{artist_name}'"
    return f"收藏里没有 '{artist_name}'"


def fav_list() -> str:
    """列出所有收藏，返回文字描述"""
    data = _load_favorites()
    parts = []
    if data["songs"]:
        song_list = ", ".join(
            f"{s['name']}({s['artist']})" if s.get("artist") else s["name"]
            for s in data["songs"]
        )
        parts.append(f"收藏歌曲: {song_list}")
    if data["artists"]:
        parts.append(f"收藏音乐家: {', '.join(data['artists'])}")
    if not parts:
        return "收藏夹是空的"
    return "。".join(parts)


def fav_play_random_song() -> bool:
    """随机播放一首收藏的歌曲"""
    import random
    data = _load_favorites()
    if not data["songs"]:
        print("[Music] 收藏夹没有歌曲")
        return False
    song = random.choice(data["songs"])
    query = f"{song.get('artist', '')} {song['name']}".strip()
    print(f"[Music] 随机播放收藏: {query}")
    return search_and_play(query)


def fav_play_artist(artist_name: str = "") -> bool:
    """播放收藏的音乐家的歌，可指定名字或随机选一位"""
    import random
    data = _load_favorites()
    if not data["artists"]:
        print("[Music] 没有收藏的音乐家")
        return False
    if artist_name:
        if artist_name not in data["artists"]:
            print(f"[Music] '{artist_name}' 不在收藏里")
            return False
        name = artist_name
    else:
        name = random.choice(data["artists"])
    print(f"[Music] 播放音乐家: {name}")
    return search_and_play(name)


# ============ 不喜欢（黑名单） ============

BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "blacklist.json")


def _load_blacklist() -> dict:
    """加载黑名单数据"""
    import json
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"songs": [], "artists": []}


def _save_blacklist(data: dict):
    """保存黑名单数据"""
    import json
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def dislike_song(song_name: str, artist: str = "") -> str:
    """标记不喜欢的歌曲"""
    data = _load_blacklist()
    for s in data["songs"]:
        if s["name"] == song_name:
            return f"'{song_name}' 已经在不喜欢列表里了"
    data["songs"].append({"name": song_name, "artist": artist})
    _save_blacklist(data)
    # 同时从收藏中移除
    fav_data = _load_favorites()
    fav_data["songs"] = [s for s in fav_data["songs"] if s["name"] != song_name]
    _save_favorites(fav_data)
    return f"已标记不喜欢 '{song_name}'"


def undislike_song(song_name: str) -> str:
    """取消不喜欢的歌曲"""
    data = _load_blacklist()
    before = len(data["songs"])
    data["songs"] = [s for s in data["songs"] if s["name"] != song_name]
    if len(data["songs"]) < before:
        _save_blacklist(data)
        return f"已取消不喜欢 '{song_name}'"
    return f"不喜欢列表里没有 '{song_name}'"


def dislike_artist(artist_name: str) -> str:
    """标记不喜欢的音乐家/歌手"""
    data = _load_blacklist()
    if artist_name in data["artists"]:
        return f"'{artist_name}' 已经在不喜欢列表里了"
    data["artists"].append(artist_name)
    _save_blacklist(data)
    # 同时从收藏中移除
    fav_data = _load_favorites()
    if artist_name in fav_data["artists"]:
        fav_data["artists"].remove(artist_name)
        _save_favorites(fav_data)
    return f"已标记不喜欢音乐家 '{artist_name}'"


def undislike_artist(artist_name: str) -> str:
    """取消不喜欢的音乐家/歌手"""
    data = _load_blacklist()
    if artist_name in data["artists"]:
        data["artists"].remove(artist_name)
        _save_blacklist(data)
        return f"已取消不喜欢 '{artist_name}'"
    return f"不喜欢列表里没有 '{artist_name}'"


def dislike_list() -> str:
    """列出所有不喜欢的歌曲和音乐家"""
    data = _load_blacklist()
    parts = []
    if data["songs"]:
        song_list = ", ".join(
            f"{s['name']}({s['artist']})" if s.get("artist") else s["name"]
            for s in data["songs"]
        )
        parts.append(f"不喜欢的歌曲: {song_list}")
    if data["artists"]:
        parts.append(f"不喜欢的音乐家: {', '.join(data['artists'])}")
    if not parts:
        return "不喜欢列表是空的"
    return "。".join(parts)


def is_disliked(song_name: str = "", artist_name: str = "") -> bool:
    """检查歌曲或音乐家是否在黑名单中"""
    data = _load_blacklist()
    if song_name:
        for s in data["songs"]:
            if s["name"] == song_name:
                return True
    if artist_name and artist_name in data["artists"]:
        return True
    return False


def search_bilibili(keyword: str, max_results: int = 3) -> list[str]:
    """在B站搜索关键词，返回BV号列表"""
    encoded = urllib.parse.quote(keyword)
    url = f"https://search.bilibili.com/all?keyword={encoded}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8")
        bvs = re.findall(r"(BV[a-zA-Z0-9]+)", html)
        # 去重保持顺序
        seen = set()
        unique = []
        for bv in bvs:
            if bv not in seen:
                seen.add(bv)
                unique.append(bv)
            if len(unique) >= max_results:
                break
        return unique
    except Exception as e:
        print(f"[Music] B站搜索失败: {e}")
        return []


def download_audio(bv_id: str, filename: str) -> str | None:
    """用yt-dlp从B站下载音频，返回文件路径"""
    url = f"https://www.bilibili.com/video/{bv_id}"
    output_path = os.path.join(MUSIC_DIR, filename)

    # 如果已有缓存直接返回
    for ext in [".m4a", ".mp3", ".wav", ".aac"]:
        cached = output_path + ext
        if os.path.exists(cached):
            print(f"[Music] 命中缓存: {cached}")
            return cached

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "yt_dlp",
                "--no-check-certificates",
                url,
                "-x",
                "-o", output_path + ".%(ext)s",
                "--no-playlist",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        print(result.stdout[-500:] if result.stdout else "")
        if result.stderr:
            print(result.stderr[-300:])

        # 查找下载的文件
        for ext in [".m4a", ".mp3", ".wav", ".aac", ".opus", ".webm"]:
            path = output_path + ext
            if os.path.exists(path):
                print(f"[Music] 下载完成: {path}")
                return path
    except subprocess.TimeoutExpired:
        print("[Music] 下载超时")
    except Exception as e:
        print(f"[Music] 下载失败: {e}")

    return None


def play_file(filepath: str):
    """用系统默认播放器播放音频文件，播放前先停止之前的音乐"""
    global _current_player
    if not os.path.exists(filepath):
        print(f"[Music] 文件不存在: {filepath}")
        return False

    # 先停止之前的播放
    stop_playing()

    try:
        if sys.platform == "win32":
            # 用 PowerShell 的 Start-Process 启动，可以拿到进程控制
            _current_player = subprocess.Popen(
                ["powershell", "-Command", f'Start-Process "{filepath}"'],
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            )
        else:
            _current_player = subprocess.Popen(["xdg-open", filepath])
        print(f"[Music] 正在播放: {filepath}")
        return True
    except Exception as e:
        print(f"[Music] 播放失败: {e}")
        return False


def search_and_play(song_name: str) -> bool:
    """搜索并播放歌曲（主入口）"""
    print(f"[Music] 搜索: {song_name}")

    # 1. 搜索
    bvs = search_bilibili(song_name)
    if not bvs:
        print("[Music] 未找到相关歌曲")
        return False

    print(f"[Music] 找到 {len(bvs)} 个结果，下载第一个: {bvs[0]}")

    # 2. 下载
    safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', song_name)
    filepath = download_audio(bvs[0], safe_name)
    if not filepath:
        print("[Music] 下载失败")
        return False

    # 3. 播放
    return play_file(filepath)


def search_and_play_async(song_name: str, callback=None):
    """异步搜索并播放"""
    def _worker():
        ok = search_and_play(song_name)
        if callback:
            callback(ok)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


# ============ 播放队列 ============

def queue_add(song_name: str, artist: str = "") -> str:
    """添加歌曲到播放队列"""
    with _queue_lock:
        q = _load_queue()
        q.append({"name": song_name, "artist": artist})
        _save_queue(q)
        pos = len(q)
    return f"已添加 '{song_name}' 到队列第{pos}首"


def queue_add_list(songs: list[dict]) -> str:
    """批量添加歌曲到队列"""
    with _queue_lock:
        q = _load_queue()
        q.extend(songs)
        _save_queue(q)
    names = ", ".join(s["name"] for s in songs)
    return f"已添加 {len(songs)} 首歌到队列: {names}"


def queue_clear() -> str:
    """清空播放队列"""
    with _queue_lock:
        _save_queue([])
    return "播放队列已清空"


def queue_list() -> str:
    """查看播放队列"""
    with _queue_lock:
        q = _load_queue()
        if not q:
            return "播放队列是空的"
        items = []
        for i, s in enumerate(q, 1):
            name = f"{s['artist']} {s['name']}" if s.get("artist") else s["name"]
            items.append(f"{i}.{name}")
    return f"播放队列({len(items)}首): " + ", ".join(items)


def queue_play(wait_current: bool = False) -> str:
    """开始播放队列。wait_current=True时等当前歌播完再开始"""
    global _queue_thread
    with _queue_lock:
        q = _load_queue()
        if not q:
            return "播放队列是空的"
        count = len(q)

    _queue_stop.clear()

    def _worker():
        import time
        if wait_current and _is_player_running():
            print("[队列] 等待当前歌曲播完...")
            while _is_player_running() and not _queue_stop.is_set():
                time.sleep(1)
            if _queue_stop.is_set():
                return
            print("[队列] 当前歌曲已播完，开始队列")

        while not _queue_stop.is_set():
            with _queue_lock:
                q = _load_queue()
                if not q:
                    break
                song = q.pop(0)
                _save_queue(q)

            query = f"{song.get('artist', '')} {song['name']}".strip()
            print(f"\n[队列] 播放: {query}")
            search_and_play(query)

            # 等播放器启动
            time.sleep(2)
            # 监控播放器，播完自动下一首
            while not _queue_stop.is_set():
                if not _is_player_running():
                    print("[队列] 当前歌曲播完")
                    break
                time.sleep(1)

        if _queue_stop.is_set():
            print("[队列] 已停止")
        else:
            print("[队列] 全部播完")

    _queue_thread = threading.Thread(target=_worker, daemon=True)
    _queue_thread.start()
    wait_str = "(等当前歌播完后开始)" if wait_current else ""
    return f"开始播放队列，共{count}首{wait_str}"


def queue_stop_playing() -> str:
    """停止队列播放"""
    _queue_stop.set()
    stop_playing()
    return "已停止队列播放"


def queue_skip() -> str:
    """跳过当前歌曲，播放队列下一首"""
    stop_playing()
    with _queue_lock:
        q = _load_queue()
        if not q:
            return "队列已空，没有下一首了"
        next_song = q[0]
    name = f"{next_song.get('artist', '')} {next_song['name']}".strip()
    return f"跳过当前歌，下一首: {name}"


# ============ 播放监控 ============

def start_monitor(on_finished=None) -> str:
    """启动播放监控，检测到播放器关闭时自动播放队列下一首或触发回调"""
    global _monitor_thread, _on_playback_finished
    _monitor_stop.clear()
    _on_playback_finished = on_finished

    def _monitor():
        import time
        was_playing = _is_player_running()
        print("[监控] 已启动播放监控")
        while not _monitor_stop.is_set():
            is_playing = _is_player_running()
            if was_playing and not is_playing:
                print("[监控] 检测到播放结束")
                # 检查队列是否有下一首
                with _queue_lock:
                    q = _load_queue()
                if q:
                    song = q[0]
                    name = f"{song.get('artist', '')} {song['name']}".strip()
                    print(f"[监控] 自动播放下一首: {name}")
                    with _queue_lock:
                        q = _load_queue()
                        if q:
                            song = q.pop(0)
                            _save_queue(q)
                    query = f"{song.get('artist', '')} {song['name']}".strip()
                    search_and_play(query)
                    time.sleep(2)
                elif _on_playback_finished:
                    _on_playback_finished()
                else:
                    print("[监控] 队列已空，播放全部结束")
            was_playing = is_playing
            time.sleep(1)
        print("[监控] 已停止")

    _monitor_thread = threading.Thread(target=_monitor, daemon=True)
    _monitor_thread.start()
    return "播放监控已启动"


def stop_monitor() -> str:
    """停止播放监控"""
    _monitor_stop.set()
    return "播放监控已停止"


if __name__ == "__main__":
    import urllib.parse
    args = sys.argv[1:]
    if not args:
        print("用法:")
        print("  python music_player.py play <歌名>        搜索并播放")
        print("  python music_player.py stop               停止播放")
        print("  python music_player.py fav-song <歌名> [歌手]  收藏歌曲")
        print("  python music_player.py unfav-song <歌名>  取消收藏歌曲")
        print("  python music_player.py fav-artist <歌手>  收藏音乐家")
        print("  python music_player.py unfav-artist <歌手> 取消收藏音乐家")
        print("  python music_player.py fav-list           查看收藏")
        print("  python music_player.py fav-play           随机播放收藏")
        print("  python music_player.py fav-play-artist [歌手] 播放音乐家")
        print("  python music_player.py dislike-song <歌名> [歌手]  标记不喜欢歌曲")
        print("  python music_player.py undislike-song <歌名>  取消不喜欢歌曲")
        print("  python music_player.py dislike-artist <歌手>  标记不喜欢音乐家")
        print("  python music_player.py undislike-artist <歌手> 取消不喜欢音乐家")
        print("  python music_player.py dislike-list          查看不喜欢列表")
        print("  python music_player.py queue-add <歌名> [歌手]  添加到队列")
        print("  python music_player.py queue-list            查看队列")
        print("  python music_player.py queue-play [--wait]   播放队列")
        print("  python music_player.py queue-clear           清空队列")
        print("  python music_player.py queue-stop            停止队列")
        print("  python music_player.py queue-skip            跳到下一首")
        print("  python music_player.py monitor-start         启动播放监控(自动播放队列)")
        print("  python music_player.py monitor-stop          停止监控")
        sys.exit(0)

    cmd = args[0]
    if cmd == "play":
        search_and_play(args[1] if len(args) > 1 else "乌兰巴托的夜")
    elif cmd == "stop":
        stop_playing()
        print("已停止播放")
    elif cmd == "fav-song":
        name = args[1] if len(args) > 1 else ""
        artist = args[2] if len(args) > 2 else ""
        print(fav_add_song(name, artist))
    elif cmd == "unfav-song":
        print(fav_remove_song(args[1] if len(args) > 1 else ""))
    elif cmd == "fav-artist":
        print(fav_add_artist(args[1] if len(args) > 1 else ""))
    elif cmd == "unfav-artist":
        print(fav_remove_artist(args[1] if len(args) > 1 else ""))
    elif cmd == "fav-list":
        print(fav_list())
    elif cmd == "fav-play":
        fav_play_random_song()
    elif cmd == "fav-play-artist":
        fav_play_artist(args[1] if len(args) > 1 else "")
    elif cmd == "dislike-song":
        name = args[1] if len(args) > 1 else ""
        artist = args[2] if len(args) > 2 else ""
        print(dislike_song(name, artist))
    elif cmd == "undislike-song":
        print(undislike_song(args[1] if len(args) > 1 else ""))
    elif cmd == "dislike-artist":
        print(dislike_artist(args[1] if len(args) > 1 else ""))
    elif cmd == "undislike-artist":
        print(undislike_artist(args[1] if len(args) > 1 else ""))
    elif cmd == "dislike-list":
        print(dislike_list())
    elif cmd == "queue-add":
        name = args[1] if len(args) > 1 else ""
        artist = args[2] if len(args) > 2 else ""
        print(queue_add(name, artist))
    elif cmd == "queue-list":
        print(queue_list())
    elif cmd == "queue-play":
        wait = "--wait" in args
        print(queue_play(wait_current=wait))
        if _queue_thread:
            _queue_thread.join()
    elif cmd == "queue-clear":
        print(queue_clear())
    elif cmd == "queue-stop":
        print(queue_stop_playing())
    elif cmd == "queue-skip":
        print(queue_skip())
    elif cmd == "monitor-start":
        print(start_monitor())
        # 保持进程运行
        try:
            while not _monitor_stop.is_set():
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            stop_monitor()
    elif cmd == "monitor-stop":
        print(stop_monitor())
    else:
        # 兼容旧用法：直接传歌名
        search_and_play(" ".join(args))
