"""
音乐搜索与播放模块 (mpv版)
- 从B站搜索歌曲并下载音频
- 使用mpv播放器播放（精确进程控制）
- 支持播放队列、收藏、不喜欢（黑名单）
- 支持状态查询、暂停、继续、下一首
"""

import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse
import threading
import json
import time
import signal

# ============ mpv 播放器路径 ============
MPV_PATH = r"C:\Program Files\MPV Player\mpv.exe"

# ============ 播放器状态 ============
_current_player = None  # mpv子进程
_player_lock = threading.Lock()
_current_song_info = {"name": "", "artist": "", "file": ""}
_play_state_lock = threading.Lock()

# ============ 播放队列 ============
_queue_lock = threading.Lock()
_queue_thread = None
_queue_stop = threading.Event()
QUEUE_FILE = os.path.join(os.path.dirname(__file__), "queue.json")

# ============ 播放监控 ============
_monitor_thread = None
_monitor_stop = threading.Event()
_on_playback_finished = None

# ============ 数据文件路径 ============
MUSIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "music_cache")
os.makedirs(MUSIC_DIR, exist_ok=True)
FAVORITES_FILE = os.path.join(os.path.dirname(__file__), "favorites.json")
BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "blacklist.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "player_state.json")
STOP_SIGNAL_FILE = os.path.join(os.path.dirname(__file__), ".stop_signal")


# ============ JSON 工具 ============
def _load_json(filepath, default=None):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}


def _save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============ 队列管理 ============
def _load_queue() -> list:
    return _load_json(QUEUE_FILE, [])


def _save_queue(q: list):
    _save_json(QUEUE_FILE, q)


# ============ mpv 播放核心 ============
def _is_player_running() -> bool:
    """检测mpv是否还在播放"""
    with _player_lock:
        if _current_player is not None:
            if _current_player.poll() is None:
                return True
    # 后备：检查系统中是否有mpv进程在运行
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/fi", "IMAGENAME eq mpv.exe"],
                capture_output=True, text=True, encoding="gbk", errors="replace", timeout=5
            )
            if "mpv.exe" in r.stdout.lower():
                return True
        except Exception:
            pass
    return False


def _save_state():
    """持久化当前播放状态"""
    with _play_state_lock:
        state = dict(_current_song_info)
    state["playing"] = _is_player_running()
    _save_json(STATE_FILE, state)


def _set_current_song(name: str = "", artist: str = "", filepath: str = ""):
    """设置当前播放歌曲信息"""
    with _play_state_lock:
        _current_song_info["name"] = name
        _current_song_info["artist"] = artist
        _current_song_info["file"] = filepath
    _save_state()


def _write_stop_signal():
    """写入停止信号文件，通知其他进程中的队列/监控停止"""
    try:
        with open(STOP_SIGNAL_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _check_stop_signal() -> bool:
    """检查是否有停止信号（5秒内有效）"""
    try:
        if os.path.exists(STOP_SIGNAL_FILE):
            with open(STOP_SIGNAL_FILE, "r") as f:
                ts = float(f.read().strip())
            if time.time() - ts < 5:
                return True
            # 过期了，清理
            os.remove(STOP_SIGNAL_FILE)
    except Exception:
        pass
    return False


def _clear_stop_signal():
    """清理停止信号"""
    try:
        if os.path.exists(STOP_SIGNAL_FILE):
            os.remove(STOP_SIGNAL_FILE)
    except Exception:
        pass


def stop_playing():
    """停止当前正在播放的音乐"""
    global _current_player
    # 写停止信号（通知其他进程中的队列/监控）
    _write_stop_signal()
    # 设置线程事件（通知本进程中的队列/监控）
    _queue_stop.set()
    _monitor_stop.set()
    with _player_lock:
        if _current_player is not None:
            try:
                _current_player.terminate()
                _current_player.wait(timeout=3)
            except Exception:
                try:
                    _current_player.kill()
                    _current_player.wait(timeout=2)
                except Exception:
                    pass
            _current_player = None
    # 确保杀掉所有mpv进程
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "mpv.exe"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass
    # 清理状态
    _set_current_song()
    print("[Music] 已停止播放")


def play_file(filepath: str) -> bool:
    """用mpv播放音频文件，播放前先停止之前的音乐"""
    global _current_player
    if not os.path.exists(filepath):
        print(f"[Music] 文件不存在: {filepath}")
        return False

    # 先停止之前的播放
    stop_playing()
    # 清理停止信号（因为这是主动播放新歌）
    _clear_stop_signal()
    _queue_stop.clear()

    try:
        # Windows: CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW 确保mpv独立于父进程
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        with _player_lock:
            _current_player = subprocess.Popen(
                [
                    MPV_PATH,
                    "--no-video",          # 纯音频，不弹窗
                    "--really-quiet",      # 减少输出
                    "--no-terminal",       # 不占用终端
                    filepath,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        print(f"[Music] 正在播放: {os.path.basename(filepath)}")
        return True
    except FileNotFoundError:
        print(f"[Music] 找不到mpv播放器: {MPV_PATH}")
        print("[Music] 请确认mpv已安装")
        return False
    except Exception as e:
        print(f"[Music] 播放失败: {e}")
        return False


def pause_playing():
    """暂停/继续播放（通过向mpv进程发送信号实现不了，需要IPC）
    简单实现：暂停=停止，记住位置留给未来IPC版本"""
    # mpv支持通过input-ipc-server来控制，未来可以扩展
    print("[Music] 暂停功能暂未实现（需要mpv IPC模式）")
    return "暂停功能暂未实现"


def get_status() -> str:
    """获取当前播放状态"""
    playing = _is_player_running()
    with _play_state_lock:
        name = _current_song_info["name"]
        artist = _current_song_info["artist"]

    if playing and name:
        status = f"正在播放: {name}"
        if artist:
            status += f" - {artist}"
        return status
    elif playing:
        return "播放器正在运行(未知曲目)"
    else:
        # 尝试从持久化状态读取上一首
        state = _load_json(STATE_FILE, {})
        if state.get("name"):
            return f"已停止。上一首: {state['name']}"
        return "当前没有在播放"


# ============ B站搜索与下载 ============
def search_bilibili(keyword: str, max_results: int = 3) -> list[str]:
    """在B站搜索关键词，返回BV号列表"""
    encoded = urllib.parse.quote(keyword)
    url = f"https://search.bilibili.com/all?keyword={encoded}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8")
        bvs = re.findall(r"(BV[a-zA-Z0-9]+)", html)
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

    # 缓存检查
    for ext in [".m4a", ".mp3", ".wav", ".aac", ".opus", ".webm"]:
        cached = output_path + ext
        if os.path.exists(cached):
            print(f"[Music] 命中缓存: {cached}")
            return cached

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "yt_dlp",
                "--no-check-certificates",
                url, "-x",
                "-o", output_path + ".%(ext)s",
                "--no-playlist",
            ],
            capture_output=True, text=True,
            timeout=300, encoding="utf-8", errors="replace",
        )
        if result.stdout:
            print(result.stdout[-500:])
        if result.stderr:
            print(result.stderr[-300:])

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


def search_and_play(song_name: str) -> bool:
    """搜索并播放歌曲（主入口）"""
    print(f"[Music] 搜索: {song_name}")

    bvs = search_bilibili(song_name)
    if not bvs:
        print("[Music] 未找到相关歌曲")
        return False

    print(f"[Music] 找到 {len(bvs)} 个结果，下载第一个: {bvs[0]}")

    safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', song_name)
    filepath = download_audio(bvs[0], safe_name)
    if not filepath:
        print("[Music] 下载失败")
        return False

    ok = play_file(filepath)
    if ok:
        _set_current_song(name=song_name, filepath=filepath)
    return ok


def search_and_play_async(song_name: str, callback=None):
    """异步搜索并播放"""
    def _worker():
        ok = search_and_play(song_name)
        if callback:
            callback(ok)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def next_song() -> str:
    """跳过当前歌曲，播放队列中的下一首"""
    stop_playing()
    with _queue_lock:
        q = _load_queue()
        if not q:
            return "队列已空，没有下一首了"
        song = q.pop(0)
        _save_queue(q)
    query = f"{song.get('artist', '')} {song['name']}".strip()
    print(f"[Music] 下一首: {query}")
    ok = search_and_play(query)
    if ok:
        _set_current_song(song['name'], song.get('artist', ''))
        return f"正在播放: {song['name']} - {song.get('artist', '')}"
    return f"播放失败: {song['name']}"


# ============ 收藏管理 ============
def _load_favorites() -> dict:
    return _load_json(FAVORITES_FILE, {"songs": [], "artists": []})


def _save_favorites(data: dict):
    _save_json(FAVORITES_FILE, data)


def fav_add_song(song_name: str, artist: str = "") -> str:
    data = _load_favorites()
    for s in data["songs"]:
        if s["name"] == song_name and s.get("artist", "") == artist:
            return f"'{song_name}' 已经在收藏里了"
    data["songs"].append({"name": song_name, "artist": artist})
    _save_favorites(data)
    return f"已收藏歌曲 '{song_name}'"


def fav_remove_song(song_name: str) -> str:
    data = _load_favorites()
    before = len(data["songs"])
    data["songs"] = [s for s in data["songs"] if s["name"] != song_name]
    if len(data["songs"]) < before:
        _save_favorites(data)
        return f"已取消收藏 '{song_name}'"
    return f"收藏里没有 '{song_name}'"


def fav_add_artist(artist_name: str) -> str:
    data = _load_favorites()
    if artist_name in data["artists"]:
        return f"'{artist_name}' 已经在收藏里了"
    data["artists"].append(artist_name)
    _save_favorites(data)
    return f"已收藏音乐家 '{artist_name}'"


def fav_remove_artist(artist_name: str) -> str:
    data = _load_favorites()
    if artist_name in data["artists"]:
        data["artists"].remove(artist_name)
        _save_favorites(data)
        return f"已取消收藏 '{artist_name}'"
    return f"收藏里没有 '{artist_name}'"


def fav_list() -> str:
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
def _load_blacklist() -> dict:
    return _load_json(BLACKLIST_FILE, {"songs": [], "artists": []})


def _save_blacklist(data: dict):
    _save_json(BLACKLIST_FILE, data)


def dislike_song(song_name: str, artist: str = "") -> str:
    data = _load_blacklist()
    for s in data["songs"]:
        if s["name"] == song_name:
            return f"'{song_name}' 已经在不喜欢列表里了"
    data["songs"].append({"name": song_name, "artist": artist})
    _save_blacklist(data)
    fav_data = _load_favorites()
    fav_data["songs"] = [s for s in fav_data["songs"] if s["name"] != song_name]
    _save_favorites(fav_data)
    return f"已标记不喜欢 '{song_name}'"


def undislike_song(song_name: str) -> str:
    data = _load_blacklist()
    before = len(data["songs"])
    data["songs"] = [s for s in data["songs"] if s["name"] != song_name]
    if len(data["songs"]) < before:
        _save_blacklist(data)
        return f"已取消不喜欢 '{song_name}'"
    return f"不喜欢列表里没有 '{song_name}'"


def dislike_artist(artist_name: str) -> str:
    data = _load_blacklist()
    if artist_name in data["artists"]:
        return f"'{artist_name}' 已经在不喜欢列表里了"
    data["artists"].append(artist_name)
    _save_blacklist(data)
    fav_data = _load_favorites()
    if artist_name in fav_data["artists"]:
        fav_data["artists"].remove(artist_name)
        _save_favorites(fav_data)
    return f"已标记不喜欢音乐家 '{artist_name}'"


def undislike_artist(artist_name: str) -> str:
    data = _load_blacklist()
    if artist_name in data["artists"]:
        data["artists"].remove(artist_name)
        _save_blacklist(data)
        return f"已取消不喜欢 '{artist_name}'"
    return f"不喜欢列表里没有 '{artist_name}'"


def dislike_list() -> str:
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
    data = _load_blacklist()
    if song_name:
        for s in data["songs"]:
            if s["name"] == song_name:
                return True
    if artist_name and artist_name in data["artists"]:
        return True
    return False


# ============ 播放队列 ============
def queue_add(song_name: str, artist: str = "") -> str:
    with _queue_lock:
        q = _load_queue()
        q.append({"name": song_name, "artist": artist})
        _save_queue(q)
        pos = len(q)
    return f"已添加 '{song_name}' 到队列第{pos}首"


def queue_add_list(songs: list[dict]) -> str:
    with _queue_lock:
        q = _load_queue()
        q.extend(songs)
        _save_queue(q)
    names = ", ".join(s["name"] for s in songs)
    return f"已添加 {len(songs)} 首歌到队列: {names}"


def queue_clear() -> str:
    with _queue_lock:
        _save_queue([])
    return "播放队列已清空"


def queue_list() -> str:
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
    """播放队列"""
    global _queue_thread
    with _queue_lock:
        q = _load_queue()
        if not q:
            return "播放队列是空的"
        count = len(q)

    _queue_stop.clear()
    _clear_stop_signal()

    def _worker():
        if wait_current and _is_player_running():
            print("[队列] 等待当前歌曲播完...")
            while _is_player_running() and not _queue_stop.is_set():
                time.sleep(1)
            if _queue_stop.is_set():
                return
            print("[队列] 当前歌曲已播完，开始队列")

        while not _queue_stop.is_set():
            # 检查跨进程停止信号
            if _check_stop_signal():
                print("[队列] 收到停止信号")
                break

            with _queue_lock:
                q = _load_queue()
                if not q:
                    break
                song = q.pop(0)
                _save_queue(q)

            # 黑名单检查
            if is_disliked(song_name=song.get("name", ""), artist_name=song.get("artist", "")):
                print(f"[队列] 跳过(不喜欢): {song.get('name', '')}")
                continue

            query = f"{song.get('artist', '')} {song['name']}".strip()
            print(f"\n[队列] 播放: {query}")
            ok = search_and_play(query)
            if ok:
                _set_current_song(song['name'], song.get('artist', ''))
                # 等mpv播放完
                while not _queue_stop.is_set() and _is_player_running():
                    if _check_stop_signal():
                        print("[队列] 收到停止信号")
                        _queue_stop.set()
                        break
                    time.sleep(1)
                if _queue_stop.is_set():
                    break
                print("[队列] 当前歌曲播完")

        if _queue_stop.is_set():
            print("[队列] 已停止")
        else:
            print("[队列] 全部播完")

    _queue_thread = threading.Thread(target=_worker, daemon=True)
    _queue_thread.start()
    wait_str = "(等当前歌播完后开始)" if wait_current else ""
    return f"开始播放队列，共{count}首{wait_str}"


def queue_stop_playing() -> str:
    _queue_stop.set()
    stop_playing()
    return "已停止队列播放"


def queue_skip() -> str:
    """跳过当前歌，播下一首"""
    return next_song()


# ============ 播放监控 ============
def start_monitor(on_finished=None) -> str:
    global _monitor_thread, _on_playback_finished
    _monitor_stop.clear()
    _on_playback_finished = on_finished

    def _monitor():
        was_playing = _is_player_running()
        print("[监控] 已启动播放监控")
        while not _monitor_stop.is_set():
            is_playing = _is_player_running()
            if was_playing and not is_playing:
                # 检查是否是主动停止
                if _check_stop_signal():
                    print("[监控] 收到停止信号，不自动播下一首")
                    was_playing = False
                    continue
                print("[监控] 检测到播放结束")
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
                    if is_disliked(song_name=song.get("name", ""), artist_name=song.get("artist", "")):
                        print(f"[监控] 跳过(不喜欢): {song.get('name', '')}")
                    else:
                        query = f"{song.get('artist', '')} {song['name']}".strip()
                        search_and_play(query)
                        _set_current_song(song['name'], song.get('artist', ''))
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
    _monitor_stop.set()
    return "播放监控已停止"


# ============ 命令行入口 ============
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("用法:")
        print("  play <歌名>              搜索并播放")
        print("  stop                     停止播放")
        print("  status                   查看播放状态")
        print("  next                     下一首")
        print("  fav-song <歌名> [歌手]   收藏歌曲")
        print("  unfav-song <歌名>        取消收藏")
        print("  fav-artist <歌手>        收藏音乐家")
        print("  unfav-artist <歌手>      取消收藏")
        print("  fav-list                 查看收藏")
        print("  fav-play                 随机播放收藏")
        print("  fav-play-artist [歌手]   播放音乐家")
        print("  dislike-song <歌名> [歌手]  标记不喜欢")
        print("  undislike-song <歌名>    取消不喜欢")
        print("  dislike-artist <歌手>    标记不喜欢")
        print("  undislike-artist <歌手>  取消不喜欢")
        print("  dislike-list             查看不喜欢列表")
        print("  queue-add <歌名> [歌手]  添加到队列")
        print("  queue-list               查看队列")
        print("  queue-play [--wait]      播放队列")
        print("  queue-clear              清空队列")
        print("  queue-stop               停止队列")
        print("  queue-skip               跳到下一首")
        print("  monitor-start            启动播放监控")
        print("  monitor-stop             停止监控")
        sys.exit(0)

    cmd = args[0]
    if cmd == "play":
        search_and_play(args[1] if len(args) > 1 else "乌兰巴托的夜")
    elif cmd == "stop":
        stop_playing()
    elif cmd == "status":
        print(get_status())
    elif cmd == "next":
        print(next_song())
    elif cmd == "fav-song":
        print(fav_add_song(args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else ""))
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
        print(dislike_song(args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else ""))
    elif cmd == "undislike-song":
        print(undislike_song(args[1] if len(args) > 1 else ""))
    elif cmd == "dislike-artist":
        print(dislike_artist(args[1] if len(args) > 1 else ""))
    elif cmd == "undislike-artist":
        print(undislike_artist(args[1] if len(args) > 1 else ""))
    elif cmd == "dislike-list":
        print(dislike_list())
    elif cmd == "queue-add":
        print(queue_add(args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else ""))
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
        try:
            while not _monitor_stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            stop_monitor()
    elif cmd == "monitor-stop":
        print(stop_monitor())
    else:
        search_and_play(" ".join(args))
