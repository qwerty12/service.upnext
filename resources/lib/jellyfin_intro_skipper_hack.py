# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

import json
import urllib.request
import xbmcvfs

class JellyfinHack:
    def __init__(self):
        self.jellyfin_itemid = None
        self._jellyfin_server = None
        self._jellyfin_apikey = None

    def event_handler_jellyfin_userdatachanged(self, _, **kwargs):
        if kwargs.get("sender") != "plugin.video.jellyfin":
            return

        try:
            self.jellyfin_itemid = json.loads(kwargs["data"])[0]["UserDataList"][0]["ItemId"]
        except Exception:
            self.jellyfin_itemid = None

    def get_credits_time(self):
        ret = 0
        try:
            if self.jellyfin_itemid:
                if not self._jellyfin_server:
                    with open(xbmcvfs.translatePath("special://profile/addon_data/plugin.video.jellyfin/data.json"), "rb") as f:
                        jf_servers = json.load(f)
                    self._jellyfin_apikey = jf_servers["Servers"][0]["AccessToken"]
                    self._jellyfin_server = jf_servers["Servers"][0]["address"]

                req = urllib.request.Request(f"{self._jellyfin_server}/Episode/{self.jellyfin_itemid}/IntroTimestamps/v1?mode=Credits", headers={
                    "Accept": "application/json",
                    "Authorization": f"MediaBrowser Token={self._jellyfin_apikey}",
                })
                with urllib.request.urlopen(req, timeout=5) as response:
                    ret = json.load(response)["IntroStart"] # don't use ShowSkipPromptAt because UpNext has its own prompt time customisation
        except Exception:
            pass
        finally:
            self.jellyfin_itemid = None
            return ret
