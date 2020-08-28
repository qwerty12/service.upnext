# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

from __future__ import absolute_import, division, unicode_literals
from xbmc import Monitor
from api import (
    dequeue_next_item, handle_just_watched, play_addon_item, play_kodi_item,
    queue_next_item
)
from dialog import UpNextPopup
from utils import (
    addon_path, clear_property, event, get_int, log as ulog, set_property
)


class PlaybackManager:

    def __init__(self, player, state):
        self.player = player
        self.state = state
        self.log('Init', 2)

    @classmethod
    def log(cls, msg, level=2):
        ulog(msg, name=cls.__name__, level=level)

    def launch_up_next(self):
        episode, playlist_item = self.state.get_next()

        # No episode get out of here
        if not episode:
            self.log('Error: no episode to play next...exiting', 1)
            return

        # Shouldn't get here if playlist setting is disabled, but just in case
        if playlist_item and not self.state.enable_playlist:
            self.log('Playlist handling disabled', 2)
            return

        # Show popup and get new playback state
        play_next, keep_playing = self.launch_popup(episode, playlist_item)
        self.state.playing_next = play_next

        # Dequeue and stop playback if not playing next file
        if not play_next and self.state.queued:
            self.state.queued = dequeue_next_item()
        if not keep_playing:
            self.log('Stopping playback', 2)
            self.player.stop()

    def launch_popup(self, episode, playlist_item):
        episodeid = get_int(episode, 'episodeid')
        watched = self.state.unwatched_only and self.state.playcount
        if (episodeid != -1 and self.state.episodeid == episodeid or watched):
            self.log('Exit launch_popup early: already watched file', 2)
            play_next = False
            keep_playing = True
            return play_next, keep_playing

        # Add next file to playlist if existing playlist is not being used
        if not playlist_item:
            self.state.queued = queue_next_item(self.state.data, episode)

        # Only use Still Watching? popup if played limit has been reached
        show_upnext = self.state.played_in_a_row < self.state.played_limit
        # Allow auto play if enabled in settings and showing Up Next popup
        auto_play = self.state.auto_play and show_upnext

        self.log('Played in a row setting: %s' % self.state.played_limit, 2)
        self.log('Played in a row: %s' % self.state.played_in_a_row, 2)

        # Filename for dialog XML
        filename = 'script-upnext{0}{1}.xml'.format(
            '-upnext' if show_upnext else '-stillwatching',
            '-simple' if self.state.simple_mode else ''
        )
        # Create Kodi dialog to show Up Next or Still Watching? popup
        dialog = UpNextPopup(filename, addon_path(), 'default', '1080i')

        # Send episode info to popup
        dialog.set_item(episode)

        # Show popup and check that it has not been terminated early
        abort_popup = not self.show_popup_and_wait(dialog, auto_play)

        # Close dialog once we are done with it
        dialog.close()
        clear_property('service.upnext.dialog')

        if abort_popup:
            self.log('Exit launch_popup early: current file not playing', 2)
            play_next = False
            keep_playing = True
            return play_next, keep_playing

        # Update new playback state details
        auto_play = auto_play and not dialog.is_cancel()
        play_now = dialog.is_playnow()

        # Update played in a row count
        if play_now:
            self.state.played_in_a_row = 1
        elif auto_play:
            self.state.played_in_a_row += 1

        if not auto_play and not play_now:
            self.log('Exit launch_popup early: no playback option selected', 2)
            play_next = False
            # Keep playing if Cancel button was clicked on popup
            # Stop After Close functionality is handled by dialog
            keep_playing = dialog.is_cancel()
            return play_next, keep_playing

        # Request playback of next file
        # Primary method is to play next playlist item
        if playlist_item or self.state.queued:
            # Can't just seek to end of file as this triggers inconsistent Kodi
            # behaviour:
            # - Will sometimes continue playing past the end of the file
            #   preventing next file from playing
            # - Will sometimes play the next file correctly then play it again
            #   resulting in loss of UpNext state
            # - Will sometimes play the next file immediately without
            #   onPlayBackStarted firing resulting in tracking not activating
            # - Will sometimes work just fine
            # Can't just wait for next file to play as VideoPlayer closes all
            # video threads when the current file finishes
            if play_now or (auto_play and self.state.popup_cue):
                # xbmc.Player().playnext() does not allow for control of resume
                # PlayMedia builtin can't target now playing playlist
                # PlayMedia('',[playoffset=xx],[resume],[noresume])
                # JSON Player.Open is too slow (further testing required)
                # Stick to playnext() for now, without possibility for resuming
                self.player.playnext()

        # Fallback addon playback option, used if addon provides play_info
        elif self.state.has_addon_data():
            play_addon_item(self.state.data, self.state.encoding)

        # Fallback library playback option, not normally used
        else:
            play_kodi_item(episode)

        # Signal to trakt previous episode watched
        event(
            message='NEXTUPWATCHEDSIGNAL',
            data=dict(episodeid=self.state.episodeid),
            encoding='base64'
        )

        # Increase playcount and reset resume point
        # TODO: Add settings to control whether file is marked as watched and
        #       resume point is reset when next file is played
        handle_just_watched(
            episodeid=self.state.episodeid,
            playcount=self.state.playcount,
            reset_resume=True
        )

        # Determine playback method. Used for logging purposes
        msg = 'Playback: requested - using{0}{1}{2} method'
        msg = msg.format(
            ' play_now' if play_now else
            ' auto_play_on_cue' if (auto_play and self.state.popup_cue) else
            ' auto_play',
            ' play_url' if (self.state.has_addon_data() == 1) else
            ' play_info' if (self.state.has_addon_data() == 2) else
            ' library' if (isinstance(episodeid, int) and episodeid != -1) else
            ' file',
            ' playlist' if playlist_item else
            ' queue' if self.state.queued else
            ' direct'
        )
        self.log(msg, 2)

        play_next = True
        keep_playing = True
        return play_next, keep_playing

    def show_popup_and_wait(self, dialog, auto_play):
        if not self.player.isPlaying():
            popup_done = False
            return popup_done

        total_time = self.player.getTotalTime()
        play_time = self.player.getTime()
        # If cue point was provided then Up Next will auto play after a fixed
        # delay time, rather than waiting for the end of the file
        if auto_play and self.state.popup_cue:
            popup_start = max(play_time, self.state.get_popup_time())
            popup_duration = self.state.auto_play_delay
            total_time = min(popup_start + popup_duration, total_time)
        remaining = total_time - play_time

        wait_time = 0.5
        dialog.set_progress_step_size(remaining, wait_time)
        dialog.show()
        set_property('service.upnext.dialog', 'true')

        monitor = Monitor()
        while not monitor.abortRequested():
            # Current file can stop, or next file can start, while in loop
            # Abort popup update
            if not self.player.isPlaying() or self.state.starting:
                popup_done = False
                return popup_done

            remaining = total_time - self.player.getTime()
            dialog.update_progress_control(remaining)

            wait_time = min(wait_time, remaining - 1)
            if remaining <= 1 or dialog.is_cancel() or dialog.is_playnow():
                break
            monitor.waitForAbort(wait_time)

        popup_done = True
        return popup_done
