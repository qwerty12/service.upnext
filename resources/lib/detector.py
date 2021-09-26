# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

from __future__ import absolute_import, division, unicode_literals
import json
import os.path
import timeit
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageMorph
import xbmc
from settings import SETTINGS
import constants
import file_utils
import utils
try:
    import queue
except ImportError:
    import Queue as queue

# Create directory where all stored hashes will be saved
_SAVE_PATH = file_utils.translate_path(SETTINGS.detector_save_path)
file_utils.create_directory(_SAVE_PATH)


class UpNextHashStore(object):
    """Class to store/save/load hashes used by UpNextDetector"""

    __slots__ = (
        'version',
        'hash_size',
        'seasonid',
        'episode_number',
        'data',
        'timestamps'
    )

    def __init__(self, **kwargs):
        self.version = kwargs.get('version', 0.2)
        self.hash_size = kwargs.get('hash_size', (8, 8))
        self.seasonid = kwargs.get('seasonid', '')
        self.episode_number = kwargs.get(
            'episode_number', constants.UNDEFINED
        )
        self.data = kwargs.get('data', {})
        self.timestamps = kwargs.get('timestamps', {})

    @staticmethod
    def int_to_hash(val, hash_size):
        return tuple([  # pylint: disable=consider-using-generator
            1 if bit_val == "1" else 0
            for bit_val in bin(val)[2:].zfill(hash_size)
        ])

    @staticmethod
    def hash_to_int(image_hash):
        return sum(
            (bit_val or 0) << i
            for i, bit_val in enumerate(reversed(image_hash))
        )

    @classmethod
    def log(cls, msg, level=utils.LOGDEBUG):
        utils.log(msg, name=cls.__name__, level=level)

    def is_valid(self, seasonid=None, episode_number=None):
        # Non-episodic video is being played
        if not self.seasonid or self.episode_number == constants.UNDEFINED:
            return False

        # No new episode details, assume current hashes are still valid
        if seasonid is None and episode_number is None:
            return True

        # Current episode matches, current hashes are still valid
        if self.seasonid == seasonid and self.episode_number == episode_number:
            return True

        # New video is being played, invalidate old hashes
        return False

    def invalidate(self):
        self.seasonid = ''
        self.episode_number = constants.UNDEFINED

    def load(self, identifier):
        filename = file_utils.make_legal_filename(identifier, suffix='.json')
        target = os.path.join(_SAVE_PATH, filename)
        try:
            with open(target, mode='r', encoding='utf-8') as target_file:
                hashes = json.load(target_file)
        except (IOError, OSError, TypeError, ValueError):
            self.log('Could not load stored hashes from {0}'.format(target))
            return False

        if not hashes:
            return False

        self.version = float(hashes.get('version', self.version))
        self.hash_size = hashes.get('hash_size', self.hash_size)
        if 'data' in hashes:
            hash_size = self.hash_size[0] * self.hash_size[1]
            self.data = {
                tuple([utils.get_int(i) for i in key[1:-1].split(', ')]):  # pylint: disable=consider-using-generator
                self.int_to_hash(hashes['data'][key], hash_size)
                for key in hashes['data']
            }
        if 'timestamps' in hashes:
            self.timestamps = {
                utils.get_int(episode_number):
                    hashes['timestamps'][episode_number]
                for episode_number in hashes['timestamps']
            }

        self.log('Hashes loaded from {0}'.format(target))
        return True

    def save(self, identifier):
        output = {
            'version': self.version,
            'hash_size': self.hash_size,
            'data': {
                str(hash_index): self.hash_to_int(self.data[hash_index])
                for hash_index in self.data
                if hash_index[-1] != constants.UNDEFINED
            },
            'timestamps': self.timestamps
        }

        filename = file_utils.make_legal_filename(identifier, suffix='.json')
        target = os.path.join(_SAVE_PATH, filename)
        try:
            with open(target, mode='w', encoding='utf-8') as target_file:
                json.dump(output, target_file, indent=4)
                self.log('Hashes saved to {0}'.format(target))
        except (IOError, OSError, TypeError, ValueError):
            self.log('Could not save hashes to {0}'.format(target),
                     utils.LOGWARNING)
        return output

    def window(self, hash_index, size, include_all=False):
        # Get all hash indexes for episodes where the hash timestamps are
        # approximately equal (+/- an index offset)
        invalid_idx = constants.UNDEFINED
        current_idx = constants.UNDEFINED if include_all else hash_index[2]
        # Matching time period from start of file
        min_start_time = hash_index[1] - size
        max_start_time = hash_index[1] + size
        # Matching time period from end of file
        min_end_time = hash_index[0] - size
        max_end_time = hash_index[0] + size

        # Limit selection criteria for old hash storage format
        if self.version < 0.2:
            invalid_idx = 0
            min_start_time = constants.UNDEFINED
            max_start_time = constants.UNDEFINED

        return {
            hash_index: self.data[hash_index]
            for hash_index in self.data
            if invalid_idx != hash_index[-1] != current_idx
            and (
                min_start_time <= hash_index[-2] <= max_start_time
                or min_end_time <= hash_index[0] <= max_end_time
            )
        }


class UpNextDetector(object):
    """Detector class used to detect end credits in playing video"""

    __slots__ = (
        # Instances
        'hashes',
        'past_hashes',
        'player',
        'state',
        # Settings
        'match_number',
        'mismatch_number',
        'significance_level',
        # Variables
        'capture_size',
        'capture_ar',
        'capture_interval',
        'hash_index',
        'match_counts',
        # Worker pool
        'queue',
        'workers',
        # Signals
        '_lock',
        '_running',
        '_sigstop',
        '_sigterm'
    )

    def __init__(self, player, state):
        self.log('Init')

        self.player = player
        self.state = state
        self.queue = queue.Queue(maxsize=SETTINGS.detector_threads)
        self.workers = []

        self.match_counts = {
            'hits': 0,
            'misses': 0
        }
        self._lock = utils.create_lock()
        self._init_hashes()

        self._running = utils.create_event()
        self._sigstop = utils.create_event()
        self._sigterm = utils.create_event()

    @staticmethod
    def _calc_median(vals):
        """Method to calculate median value of a list of values by sorting and
            indexing the list"""

        num_vals = len(vals)
        pivot = num_vals // 2
        vals = sorted(vals)
        if num_vals % 2:
            return vals[pivot]
        return (vals[pivot] + vals[pivot - 1]) / 2

    @staticmethod
    def _calc_similarity(hash1, hash2):
        """Method to compare the similarity between two image hashes"""

        # Check that hashes are not empty and that dimensions are equal
        if not hash1 or not hash2:
            return 0
        num_pixels = len(hash1)
        if num_pixels != len(hash2):
            return 0

        # Check whether each pixel is equal
        bits_eq = sum(map(UpNextDetector._eq, hash1, hash2))
        bits_xor = map(UpNextDetector._xor, hash1, hash2)
        bits_xor_hash1 = sum(map(UpNextDetector._and, bits_xor, hash1)) * 0.25
        bits_xor_hash2 = sum(map(UpNextDetector._and, bits_xor, hash2)) * 0.75

        ignored_bits = hash1.count(None)
        bit_compare = bits_eq - bits_xor_hash1 - bits_xor_hash2

        # Evaluate similarity as a percentage of un-ignored pixels in the hash
        return max(0, 100 * bit_compare / (num_pixels - ignored_bits))

    @staticmethod
    def _and(bit1, bit2):
        return bool(bit1 and bit2)

    @staticmethod
    def _eq(bit1, bit2):
        return bit1 == bit2

    @staticmethod
    def _mul(bit1, bit2):
        return bit1 * bit2

    @staticmethod
    def _xor(bit1, bit2):
        return bool((bit1 or bit2) and (bit1 != bit2))

    @staticmethod
    def _capture_resolution(max_size=None):
        """Method to detect playing video resolution and aspect ratio and
           return a scaled down resolution tuple and aspect ratio for use in
           capturing the video frame buffer at a specific size/resolution"""

        aspect_ratio = float(xbmc.getInfoLabel('Player.Process(VideoDAR)'))
        width = xbmc.getInfoLabel('Player.Process(VideoWidth)')
        width = int(width.replace(',', ''))
        height = xbmc.getInfoLabel('Player.Process(VideoHeight)')
        height = int(height.replace(',', ''))

        # Capturing render buffer at higher resolution captures more detail
        # depending on Kodi scaling function used, but slows down processing.
        # Limit captured data to max_size (in kB)
        if max_size:
            max_size = max_size * 8 * 1024
            height = min(int((max_size / aspect_ratio) ** 0.5), height)
            width = min(int(height * aspect_ratio), width)

        return (width, height), aspect_ratio

    @staticmethod
    def _generate_initial_hash(width, height):
        blank_token = (0, )
        pixel_token = (1, )
        border_token = (0, )
        ignore_token = (None, )

        padding_1 = min(3 * width // 16, 3)
        padding_2 = min(2 * width // 16, 2)

        return (
            border_token * width * 2
            + (
                border_token
                + blank_token * 2 * padding_1
                + ignore_token * (width - 4 * padding_1 - 2)
                + blank_token * 2 * padding_1
                + border_token
            )
            + ((
                border_token
                + blank_token * padding_1
                + ignore_token * padding_1
                + pixel_token * (width - 4 * padding_1 - 2)
                + ignore_token * padding_1
                + blank_token * padding_1
                + border_token
            ) + (
                border_token
                + blank_token * padding_2
                + ignore_token * padding_2
                + pixel_token * (width - 4 * padding_2 - 2)
                + ignore_token * padding_2
                + blank_token * padding_2
                + border_token
            )) * ((height - 6) // 2)
            + (
                border_token
                + blank_token * 2 * padding_1
                + ignore_token * (width - 4 * padding_1 - 2)
                + blank_token * 2 * padding_1
                + border_token
            )
            + border_token * width * 2
        )

    @staticmethod
    def _generate_mask(image_hash):
        fuzzy_value = None
        masked_value = 0

        mask = len(image_hash) / image_hash.count(masked_value)
        fuzzy_mask = 0.25
        min_mask = 0.1

        return tuple(
            mask if pixel == masked_value else
            fuzzy_mask if pixel == fuzzy_value else
            min_mask
            for pixel in image_hash
        )

    @staticmethod
    def _image_auto_contrast(image, *_args, **_kwargs):
        histogram = image.histogram()

        values = [value for value, num in enumerate(histogram) if num]
        min_value = values[0]
        max_value = values[-1]
        if max_value <= min_value:
            return image

        scale = 255 / (max_value - min_value)
        offset = min_value * scale

        return image.point([min(255, max(0, int(i * scale - offset)))
                            for i in range(256)])

    @staticmethod
    def _image_brightness(image, factor, **_kwargs):
        return ImageEnhance.Brightness(image).enhance(factor)

    @staticmethod
    def _image_contrast(image, factor, **_kwargs):
        return ImageEnhance.Contrast(image).enhance(factor)

    @staticmethod
    def _image_find_edges(image, *_args, **_kwargs):
        # Basic contour filtering to emphasise blocks of text when hashing
        image = image.filter(ImageFilter.FIND_EDGES)

        return image

    @staticmethod
    def _image_format(image, buffer_size):
        # Convert captured image data from BGRA to RGBA
        image[0::4], image[2::4] = image[2::4], image[0::4]

        # Convert to greyscale to reduce size of data by a factor of 4
        image = Image.frombuffer(
            'RGBA', buffer_size, image, 'raw', 'RGBA', 0, 1
        ).convert('L')

        return image

    @staticmethod
    def _image_morph(image, *patterns_list, **_kwargs):
        for patterns in patterns_list:
            _, image = ImageMorph.MorphOp(
                patterns=patterns
            ).apply(image)

        return image

    @staticmethod
    def _image_multiply(image1, image2, iterations=1, **_kwargs):
        for _ in range(iterations):
            image1 = ImageChops.multiply(image1, image2)

        return image1

    @staticmethod
    def _image_process(image, image_operations):
        for operation in image_operations:
            method = operation.get('method')
            if not method:
                continue
            args = operation.get('args', [])
            kwargs = operation.get('kwargs', {})
            image = method(image, *args, **kwargs) or image

        return image

    @staticmethod
    def _image_resize(image, size, **_kwargs):
        if size and size != image.size:
            image = image.resize(
                size, resample=SETTINGS.detector_resize_method
            )

        return image

    @staticmethod
    def _image_save(image, filename, **_kwargs):
        image.save(filename)

    @classmethod
    def _generate_image_hash(cls, image):
        # Transform image to show absolute deviation from median pixel luma
        median_pixel = cls._calc_median(image.getdata())
        image = image.point([abs(i - median_pixel) for i in range(256)])

        # Calculate median absolute deviation from the median to represent
        # significant pixels and use transformed image as the hash of the
        # current video frame
        median_pixel = cls._calc_median(image.getdata())
        image = image.point([i > median_pixel for i in range(256)])

        return tuple(image.getdata())

    @classmethod
    def _print_hash(cls, hash1, hash2, size=None, prefix=None):
        """Method to print two image hashes, side by side, to the Kodi log"""

        if hash1 is None or hash2 is None:
            return
        num_pixels = len(hash1)
        if num_pixels != len(hash2):
            return

        if not size:
            size = int(num_pixels ** 0.5)
            size = (size, size)

        cls.log('\n\t\t\t'.join(
            [prefix if prefix else '-' * (7 + 4 * size[0])]
            + ['{0} | {1} | {2}'.format(
                size,
                UpNextHashStore.hash_to_int(hash1),
                UpNextHashStore.hash_to_int(hash2)
            )]
            + ['{0:>3} |{1}|{2}|'.format(
                row,
                ' '.join([
                    '+' if bit else '-' if bit is None else ' '
                    for bit in hash1[row:row + size[0]]
                ]),
                ' '.join([
                    '+' if bit else '-' if bit is None else ' '
                    for bit in hash2[row:row + size[0]]
                ])
            ) for row in range(0, num_pixels, size[0])]
        ))

    @classmethod
    def log(cls, msg, level=utils.LOGDEBUG):
        utils.log(msg, name=cls.__name__, level=level)

    def _evaluate_similarity(self, image_hash, filtered_hash=None):
        is_match = False
        possible_match = False

        stats = {
            # Similarity to representative end credits hash
            'credits': 0,
            # Similarity to previous frame hash
            'previous': 0,
            # Similarity to hash from other episodes
            'episodes': 0
        }

        # Calculate similarity between current hash and representative hash
        stats['credits'] = self._calc_similarity(
            self.hashes.data.get(self.hash_index['credits']),
            filtered_hash or image_hash
        ) - self._evaluate_uncertainty(5, image_hash, filtered_hash)
        # Match if current hash matches representative hash or if current hash
        # is blank
        is_match = (
            stats['credits'] >= SETTINGS.detect_level - 10
            or not any(image_hash)
        )
        # Unless debugging, return if match found, otherwise continue checking
        if is_match and not SETTINGS.detector_debug:
            self._hash_match_hit()
            return stats

        # Calculate similarity between current hash and previous hash
        stats['previous'] = self._calc_similarity(
            self.hashes.data.get(self.hash_index['previous']),
            image_hash
        )
        # Possible match if current hash matches previous hash
        possible_match = stats['previous'] >= SETTINGS.detect_level
        # Match if hash is also somewhat similar to representative hash
        is_match = is_match or (
            stats['credits'] >= SETTINGS.detect_level - 15
            and possible_match
        )
        # Unless debugging, return if match found, otherwise continue checking
        if is_match and not SETTINGS.detector_debug:
            self._hash_match_hit()
            return stats

        old_hashes = self.past_hashes.window(self.hash_index['current'], 60)
        for self.hash_index['episodes'], old_hash in old_hashes.items():
            stats['episodes'] = self._calc_similarity(
                old_hash,
                image_hash
            )
            # Match if current hash matches other episode hashes
            if stats['episodes'] >= SETTINGS.detect_level:
                is_match = True
                break

        # Increment the number of matches
        if is_match:
            self._hash_match_hit()
        # Otherwise increment number of mismatches
        elif not possible_match:
            self._hash_match_miss()

        return stats

    def _evaluate_uncertainty(self, scaling, image_hash, filtered_hash=None):
        if filtered_hash:
            weights = self._generate_mask(filtered_hash)
        else:
            weights = self.hashes.data.get(self.hash_index['mask'])

        significant_bits = sum(map(UpNextDetector._mul, image_hash, weights))
        significance = 100 * significant_bits / len(image_hash)
        delta = significance - self.significance_level

        return scaling * delta / self.significance_level

    def _hash_match_hit(self):
        with self._lock:
            self.match_counts['hits'] += 1
            self.match_counts['misses'] = 0
            self.match_counts['detected'] = (
                self.match_counts['hits'] >= self.match_number
            )

    def _hash_match_miss(self):
        with self._lock:
            self.match_counts['misses'] += 1
            if self.match_counts['misses'] < self.mismatch_number:
                return
        self._hash_match_reset()

    def _hash_match_reset(self):
        with self._lock:
            self.match_counts['hits'] = 0
            self.match_counts['misses'] = 0
            self.match_counts['detected'] = False

    def _init_hashes(self):
        # Limit captured data to increase processing speed
        self.capture_size, self.capture_ar = self._capture_resolution(
            max_size=SETTINGS.detector_data_limit
        )
        # Set minimum capture interval to decrease capture rate
        self.capture_interval = 0.5

        self.hash_index = {
            # Hash indexes are tuples containing the following data:
            # (time_to_end, time_from_start, episode_number)
            # Current hash
            'current': (0, 0, 0),
            # Previous hash
            'previous': None,
            # Representative end credits hash
            'credits': (0, 0, constants.UNDEFINED),
            # Significance mask of representative end credits hash
            'mask': (0, 1, constants.UNDEFINED),
            # Other episodes hash
            'episodes': None,
            # Detected end credits timestamp from end of file
            'detected_at': None
        }

        # Hash size as (width, height)
        hash_size = [8 * self.capture_ar, 8]
        # Round down width to multiple of 2
        hash_size[0] = int(hash_size[0] - hash_size[0] % 2)

        # Hashes for currently playing episode
        initial_hash = self._generate_initial_hash(*hash_size)
        self.hashes = UpNextHashStore(
            hash_size=hash_size,
            seasonid=self.state.get_season_identifier(),
            episode_number=self.state.get_episode_number(),
            # Representative hash of centred end credits text on a dark
            # background stored as first hash. Masked significance weights
            # stored as second hash.
            data={
                self.hash_index['credits']: initial_hash,
                self.hash_index['mask']: self._generate_mask(initial_hash)
            },
            timestamps={}
        )

        # Set max significance level to 25% as default
        self.significance_level = SETTINGS.detect_significance

        # Hashes from previously played episodes
        self.past_hashes = UpNextHashStore(hash_size=hash_size)
        if self.hashes.is_valid():
            self.past_hashes.load(self.hashes.seasonid)

        # Number of consecutive frame matches required for a positive detection
        # Set to 5s of captured frames as default
        self.match_number = int(
            SETTINGS.detect_matches / self.capture_interval
        )
        # Number of consecutive frame mismatches required to reset match count
        # Set to 3 frames to account for bad frame capture
        self.mismatch_number = SETTINGS.detect_mismatches
        self._hash_match_reset()

    def _push_frame_to_queue(self):
        capturer = self.queue.get()

        abort = False
        while not (abort or self._sigterm.is_set() or self._sigstop.is_set()):
            loop_start = timeit.default_timer()

            capturer.capture(*self.capture_size)
            image = capturer.getImage()

            # Capture failed or was skipped, retry with less data
            if not image or image[-1] != 255:
                if not self.player.isPlaying():
                    self.log('Stop capture: nothing playing')
                    break

                self.log('Capture failed using {0}kB data limit'.format(
                    SETTINGS.detector_data_limit
                ), utils.LOGWARNING)

                if SETTINGS.detector_data_limit > 8:
                    SETTINGS.detector_data_limit -= 8
                self.capture_size, self.capture_ar = self._capture_resolution(  # pylint: disable=attribute-defined-outside-init
                    max_size=SETTINGS.detector_data_limit
                )
                del capturer
                capturer = xbmc.RenderCapture()
                continue

            try:
                self.queue.put(image, timeout=self.capture_interval)
                loop_time = timeit.default_timer() - loop_start
                if loop_time >= self.capture_interval:
                    raise queue.Full
                abort = utils.wait(self.capture_interval - loop_time)
            except queue.Full:
                self.log('Capture/detection desync', utils.LOGWARNING)
                abort = utils.abort_requested()
                continue

        del capturer
        self.queue.task_done()

    def _worker(self):
        """Detection loop captures Kodi render buffer every 1s to create an
           image hash. Hash is compared to the previous hash to determine
           whether current frame of video is similar to the previous frame.

           Hash is also compared to hashes calculated from previously played
           episodes to detect common sequence of frames (i.e. credits).

           A consecutive number of matching frames must be detected to confirm
           that end credits are playing."""

        if SETTINGS.detector_debug:
            profiler = utils.Profiler()

        while not (self._sigterm.is_set() or self._sigstop.is_set()):
            try:
                image = self.queue.get(timeout=SETTINGS.detector_threads)
                if image is None:
                    self.queue.task_done()
                    raise queue.Empty
            except queue.Empty:
                self.log('Exiting: queue empty')
                break

            with self.player as check_fail:
                play_time = self.player.getTime()
                self.hash_index['current'] = (
                    int(self.player.getTotalTime() - play_time),
                    int(play_time),
                    self.hashes.episode_number
                )
                # Only capture if playing at normal speed
                # check_fail = self.player.get_speed() != 1
                check_fail = False
            if check_fail:
                self.log('No file is playing')
                self.queue.task_done()
                break

            image = self._image_format(image, self.capture_size)
            # Resize and generate median absolute deviation from median hash
            image_hash = self._generate_image_hash(self._image_process(
                image,
                image_operations=[
                    # {'method': self._image_save, 'args': [os.path.join(_SAVE_PATH, 'image.png')]},
                    {'method': self._image_resize,
                     'args': [self.hashes.hash_size]}
                ]
            ))
            filtered_hash = None

            if SETTINGS.detector_filter:
                filtered_image = self._image_process(
                    image,
                    image_operations=[
                        {'method': self._image_contrast,
                         'args': [100]},
                        {'method': self._image_brightness,
                         'args': [0.1]},
                        # {'method': self._image_save, 'args': [os.path.join(_SAVE_PATH, 'filter1.png')]},
                        {'method': self._image_find_edges},
                        # {'method': self._image_save, 'args': [os.path.join(_SAVE_PATH, 'filter2.png')]},
                        {'method': self._image_morph,
                         'args': [
                             ['4:(... .01 ...)->1',
                              '4:(... .0. ..1)->1'],
                             ['4:(... .10 ...)->0'],
                             ['4:(... .10 ...)->0'],
                         ]},
                        # {'method': self._image_save, 'args': [os.path.join(_SAVE_PATH, 'filter3.png')]},
                        {'method': self._image_multiply,
                         'args': [self._image_auto_contrast(image), 5]},
                        # {'method': self._image_save, 'args': [os.path.join(_SAVE_PATH, 'filtered.png')]},
                        {'method': self._image_resize,
                         'args': [self.hashes.hash_size]},
                    ]
                )
                filtered_hash = self._generate_image_hash(filtered_image)

            # Check if current hash matches with previous hash, typical end
            # credits hash, or other episode hashes
            stats = self._evaluate_similarity(image_hash, filtered_hash)

            if SETTINGS.detector_debug:
                self.log('Match: {0[hits]}/{1}, Miss: {0[misses]}/{2}'.format(
                    self.match_counts, self.match_number, self.mismatch_number
                ))

                self._print_hash(
                    self.hashes.data.get(self.hash_index['credits']),
                    filtered_hash or image_hash,
                    self.hashes.hash_size,
                    'Hash {0:2.1f}% similar to typical credits'.format(
                        stats['credits']
                    )
                )

                self._print_hash(
                    self.hashes.data.get(self.hash_index['previous']),
                    image_hash,
                    self.hashes.hash_size,
                    'Hash {0:2.1f}% similar to previous frame'.format(
                        stats['previous']
                    )
                )

                self._print_hash(
                    self.past_hashes.data.get(self.hash_index['episodes']),
                    image_hash,
                    self.hashes.hash_size,
                    'Hash {0:2.1f}% similar to other episodes'.format(
                        stats['episodes']
                    )
                )

                self.log(profiler.get_stats(reuse=True))

            # Store current hash for comparison with next video frame
            self.hashes.data[self.hash_index['current']] = image_hash
            self.hash_index['previous'] = self.hash_index['current']

            # Store timestamps if credits are detected
            if self.credits_detected():
                self.update_timestamp(play_time)

            self.queue.task_done()

    def is_alive(self):
        return self._running.is_set()

    def cancel(self):
        self.stop()

    def credits_detected(self):
        # Ignore invalidated hash data
        if not self.hashes.is_valid():
            return False

        return self.match_counts['detected']

    def reset(self):
        self._hash_match_reset()
        self.hashes.timestamps[self.hashes.episode_number] = None
        self.hash_index['detected_at'] = None

    def start(self, restart=False):
        """Method to run actual detection test loop in a separate thread"""

        if restart or self._running.is_set():
            self.stop()

        # Reset detector data if episode has changed
        if not self.hashes.is_valid(
                self.state.get_season_identifier(),
                self.state.get_episode_number()
        ):
            self._init_hashes()

        # If a previously detected timestamp exists then use it
        stored_timestamp = self.past_hashes.timestamps.get(
            self.hashes.episode_number
        )
        if stored_timestamp and not SETTINGS.detector_debug:
            self.log('Stored credits timestamp found')
            self.state.set_detected_popup_time(stored_timestamp)
            utils.event('upnext_credits_detected')
            return

        # Otherwise run the detector in a new thread
        self.log('Started')
        self._running.set()

        self.queue.put_nowait(xbmc.RenderCapture())
        self.workers = [utils.run_threaded(self._push_frame_to_queue)]
        self.workers += [
            utils.run_threaded(
                self._worker,
                delay=(start_delay * self.capture_interval)
            )
            for start_delay in range(SETTINGS.detector_threads - 1)
        ]
        self.queue.join()
        self.queue.put_nowait(None)

        if any(worker.is_alive() for worker in self.workers):
            self.stop()
        self.log('Stopped')
        self._running.clear()
        self._sigstop.clear()
        self._sigterm.clear()

    def stop(self, terminate=False):
        # Set terminate or stop signals if detector is running
        if self._running.is_set():
            if terminate:
                self._sigterm.set()
            else:
                self._sigstop.set()

            for idx, worker in enumerate(self.workers):
                if worker.is_alive():
                    worker.join(5)
                if worker.is_alive():
                    self.log('Worker {0}({1}) failed to stop cleanly'.format(
                        idx, worker.ident
                    ), utils.LOGWARNING)

        # Free references/resources
        with self._lock:
            del self.workers
            self.workers = []
            if terminate:
                # Invalidate collected hashes if not needed for later use
                self.hashes.invalidate()
                # Delete reference to instances if not needed for later use
                del self.player
                self.player = None
                del self.state
                self.state = None

    def store_data(self):
        # Only store data for videos that are grouped by season (i.e. same show
        # title, same season number)
        if not self.hashes.is_valid():
            return

        self.past_hashes.hash_size = self.hashes.hash_size
        # If credit were detected only store the previous +/- 5s of hashes to
        # reduce false positives when comparing to other episodes
        if self.match_counts['detected']:
            self.past_hashes.data.update(self.hashes.window(
                self.hash_index['detected_at'], 5, include_all=True
            ))
            self.past_hashes.timestamps.update(self.hashes.timestamps)
        # Otherwise store all hashes for comparison with other episodes
        else:
            self.past_hashes.data.update(self.hashes.data)

        self.past_hashes.save(self.hashes.seasonid)

    def update_timestamp(self, play_time):
        # Timestamp already stored
        if self.hash_index['detected_at']:
            return

        with self._lock:
            self.log('Credits detected')
            self.hash_index['detected_at'] = self.hash_index['current']
            self.hashes.timestamps[self.hashes.episode_number] = play_time
            self.state.set_detected_popup_time(play_time)
            utils.event('upnext_credits_detected')
