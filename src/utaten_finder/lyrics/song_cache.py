"""Local JSON song URL cache helpers."""

import json

from utaten_finder.core import config


def load_cache():
    """讀取本地的歌曲暫存檔案"""
    cache_path = config.SONG_CACHE_FILE
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 讀取暫存檔失敗: {e}")
            return {}
    return {}


def save_cache(cache_data):
    """將資料寫入暫存檔案"""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.SONG_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ 寫入暫存檔失敗: {e}")


def get_cached_data(song_id):
    """取得暫存的詳細資料字典"""
    cache = load_cache()
    return cache.get(song_id)


def set_cached_data(song_id, url, status, artist, song):
    """
    存入暫存資料
    status 建議分為:
    - "perfect" : 完全吻合
    - "partial" : 只找到第一個 Utaten 連結但名稱沒完全對上
    - "not_found" : 沒找到 Utaten，丟了 Google 搜尋連結
    """
    cache = load_cache()
    cache[song_id] = {"artist": artist, "song": song, "status": status, "url": url}
    save_cache(cache)
