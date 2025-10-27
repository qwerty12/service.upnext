# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

import json
import sqlite3
import urllib.request
import xbmcvfs

class JellyfinHack:
    __jellyfin_db_path = None
    _jellyfin_server = None
    _jellyfin_apikey = None

    @staticmethod
    def get_credits_start_seconds(kodi_id, media_type):
        try:
            if jellyfin_itemid := JellyfinHack._get_jf_item_id_from_kodi_id(kodi_id, media_type):
                if not JellyfinHack._jellyfin_server:
                    with open(xbmcvfs.translatePath("special://profile/addon_data/plugin.video.jellyfin/data.json")) as f:
                        jf_servers = json.load(f)
                    JellyfinHack._jellyfin_apikey = jf_servers["Servers"][0]["AccessToken"]
                    JellyfinHack._jellyfin_server = jf_servers["Servers"][0]["address"]

                req = urllib.request.Request(f"{JellyfinHack._jellyfin_server}/MediaSegments/{jellyfin_itemid}?includeSegmentTypes=Outro", headers={
                    "Accept": "application/json",
                    "Authorization": f"MediaBrowser Token=\"{JellyfinHack._jellyfin_apikey}\"",
                })
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)["Items"][0]["StartTicks"] / 10000000
        except Exception:
            pass

        return 0

    @staticmethod
    def _get_jf_item_id_from_kodi_id(kodi_id, media_type):
        if kodi_id and media_type:
            with sqlite3.connect(f"file:{JellyfinHack._jellyfin_db_path()}?mode=ro", uri=True) as conn:
                try:
                    if (cursor := conn.cursor()) and cursor.execute("SELECT jellyfin_id FROM jellyfin WHERE kodi_id = ? AND media_type = ?", (kodi_id, media_type,)):
                        if row := cursor.fetchone():
                            return row[0]
                finally:
                    if cursor:
                        cursor.close()

        return None

    @staticmethod
    def _jellyfin_db_path():
        if JellyfinHack.__jellyfin_db_path is not None:
            return JellyfinHack.__jellyfin_db_path

        db_path = None
        try:
            with open(xbmcvfs.translatePath("special://home/addons/plugin.video.jellyfin/jellyfin_kodi/objects/obj_map.json")) as f:
                obj_map = json.load(f)
            db_path = obj_map["jellyfin"]
        except Exception:
            pass

        if not db_path:
            db_path = "special://database/jellyfin.db"

        JellyfinHack.__jellyfin_db_path = xbmcvfs.translatePath(db_path)
        return JellyfinHack.__jellyfin_db_path
