# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

from __future__ import absolute_import, division, unicode_literals
import constants
import detector
import playbackmanager
import utils


class UpNextTracker(object):  # pylint: disable=useless-object-inheritance
    """UpNext playback tracker class"""

    def __init__(self, monitor, player, state):
        self.monitor = monitor
        self.player = player
        self.state = state

        self.thread = None
        self.detector = None
        self.playbackmanager = None

        self.running = False
        self.sigstop = False
        self.sigterm = False

        self.log('Init')

    @classmethod
    def log(cls, msg, level=utils.LOGINFO):
        utils.log(msg, name=cls.__name__, level=level)

    def _detector_check(self, play_time):
        # Detector starts before normal popup request time
        detect_time = self.state.get_detect_time()
        if not detect_time or play_time < detect_time:
            return

        # Start detector if not already started
        if not self.detector:
            self.detector = detector.UpNextDetector(
                monitor=self.monitor,
                player=self.player,
                state=self.state
            )
            self.detector.start()

        # Otherwise check whether credits have been detected
        elif self.detector.detected():
            # Stop detector but keep processed hashes
            self.detector.stop()
            self.log('Credits detected')
            self.state.set_popup_time(
                detected_time=self.detector.update_timestamp(play_time)
            )
            self.state.set_detect_time()

    def _detector_post_run(self, playback_cancelled):
        if not self.detector:
            tracker_restart = False
            return tracker_restart

        # If credits were (in)correctly detected and popup is cancelled
        # by the user, then restart tracking loop to allow detector to
        # restart, or to launch popup at default time
        if self.detector.credits_detected and playback_cancelled:
            # Re-start detector and reset match counts
            self.detector.start(reset=True)
            tracker_restart = True
        else:
            # Store hashes and timestamp for current video
            self.detector.store_data()
            # Stop detector and release resources
            self.detector.stop(terminate=True)
            tracker_restart = False

        return tracker_restart

    def _run(self):
        # Only track playback if old tracker is not running
        if self.running:
            return
        self.log('Started')
        self.running = True

        # If tracker was (re)started, ensure detector is also restarted
        if self.detector and not self.detector.detected():
            self.detector.start(restart=True)

        # Loop unless abort requested
        while not (self.monitor.abortRequested() or self.sigterm):
            # Exit loop if stop requested or if tracking stopped
            if self.sigstop or not self.state.is_tracking():
                self.log('Stopping')
                break

            # Get video details, exit if nothing playing
            with self.player as check_fail:
                current_file = self.player.getPlayingFile()
                total_time = self.player.getTotalTime()
                play_time = self.player.getTime()
                check_fail = False
            if check_fail:
                self.log('No file is playing')
                self.state.set_tracking(False)
                continue
            # New stream started without tracking being updated
            if self.state.get_tracked_file() != current_file:
                self.log('Error: unknown file playing', utils.LOGWARNING)
                self.state.set_tracking(False)
                continue

            # Check detector status and update detected popup time
            self._detector_check(play_time)

            popup_time = self.state.get_popup_time()
            # Media hasn't reach popup time yet, waiting a bit longer
            if play_time < popup_time:
                self.monitor.waitForAbort(min(1, popup_time - play_time))
                continue

            # Stop second thread and popup from being created after next file
            # has been requested but not yet loaded
            self.state.set_tracking(False)
            self.sigstop = True

            # Start UpNext to handle playback of next file
            self.log('Popup at {0}s of {1}s'.format(
                popup_time, total_time
            ))
            self.playbackmanager = playbackmanager.UpNextPlaybackManager(
                monitor=self.monitor,
                player=self.player,
                state=self.state
            )
            # Check if there was a video to play next
            can_play_next = self.playbackmanager.start()
            # And whether playback was cancelled by the user
            playback_cancelled = can_play_next and not self.state.playing_next

            # Cleanup detector and restart tracker if credits were incorrectly
            # detected
            tracker_restart = self._detector_post_run(playback_cancelled)
            if tracker_restart:
                self.state.set_popup_time(total_time)
                self.state.set_detect_time()
                self.state.set_tracking(current_file)
                self.sigstop = False
                continue

            # Exit tracking loop once all processing is complete
            break
        else:
            self.log('Abort', utils.LOGWARNING)

        # Reset thread signals
        self.log('Stopped')
        self.running = False
        self.sigstop = False
        self.sigterm = False

    def start(self, called=[False]):  # pylint: disable=dangerous-default-value
        # Exit if tracking disabled or start tracking previously requested
        if not self.state.is_tracking() or called[0]:
            return
        # Stop any existing tracker loop/thread/timer
        self.stop()
        called[0] = True

        # Schedule a threading.Timer to check playback details only when popup
        # is expected to be shown. Experimental mode, more testing required.
        if self.state.tracker_mode == constants.TRACKER_MODE_TIMER:
            # Playtime needs some time to update correctly after seek/skip
            # Try waiting 1s for update, longer delay may be required
            self.monitor.waitForAbort(1)
            with self.player as check_fail:
                # Use VideoPlayer.Time infolabel over xbmc.Player.getTime(), as
                # the infolabel appears to update quicker
                play_time = self.player.getTime(use_infolabel=True)
                speed = self.player.get_speed()
                check_fail = False
            # Exit if not playing, paused, or rewinding
            if check_fail or not play_time or speed < 1:
                self.log('Skip tracker start: nothing playing')
                called[0] = False
                return

            # Determine play time left until popup is required
            popup_time = self.state.get_popup_time()
            detect_time = self.state.get_detect_time()

            # Convert to delay and scale to real time minus a 10s offset
            delay = (detect_time if detect_time else popup_time) - play_time
            delay = max(0, delay // speed - 10)
            self.log('Starting at {0}s in {1}s'.format(
                detect_time if detect_time else popup_time, delay
            ))

            # Schedule tracker to start when required
            self.thread = utils.run_after(self._run, delay)

        # Use while not abortRequested() loop in a separate threading.Thread to
        # continuously poll playback details while callbacks continue to be
        # processed in main service thread. Default mode.
        elif self.state.tracker_mode == constants.TRACKER_MODE_THREAD:
            self.thread = utils.run_threaded(self._run)

        # Use while not abortRequested() loop in main service thread. Old mode.
        else:
            if self.running:
                self.sigstop = False
            else:
                self._run()

        called[0] = False

    def stop(self, terminate=False):
        # Set terminate or stop signals if tracker is running
        if terminate:
            self.sigterm = self.running
            if self.detector:
                # Stop detector and release resources
                self.detector.stop(terminate=True)
            if self.playbackmanager:
                # Stop playbackmanager, close popup and release resources
                self.playbackmanager.stop(terminate=True)
        else:
            self.sigstop = self.running
            if self.detector:
                # Stop detector and release resources
                self.detector.stop(terminate=True)
            if self.playbackmanager:
                # Stop playbackmanager and close popup
                self.playbackmanager.stop()

        # Exit if tracker thread has not been created
        if not self.thread:
            return

        # Wait for thread to complete
        if self.running:
            self.thread.join()
        # Or if tracker has not yet started on timer then cancel old timer
        elif self.state.tracker_mode == constants.TRACKER_MODE_TIMER:
            self.thread.cancel()

        # Free resources
        del self.thread
        self.thread = None
        del self.detector
        self.detector = None
        del self.playbackmanager
        self.playbackmanager = None
