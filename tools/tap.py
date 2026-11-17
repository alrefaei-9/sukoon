"""Reader for sukoon_engine.py's AudioTap (/dev/shm ring of the latest frames), plus per-bang measurements.

Used by tools/timed_test.py and tools/battery_log.py. Runs in their own low-priority process: the audio loop
only copies frames into shared memory and never waits for a reader.

  reader = TapReader('/dev/shm/sukoon_tap')
  for meta, inp, out in reader.poll():      # new frames since the last call, oldest first
"""
import math
import mmap
import os
from pathlib import Path

import numpy as np

FS = 48000
FRAME = 480
META = np.dtype([('frame', '<u8'), ('triggers', '<u4'), ('flags', '<u4'), ('vad', '<f4'), ('version', '<u4'),
                 ('time', '<f8')])
MAGIC = b'SSTAP1\0\0'
BANDS = [round(1000 * 2 ** (k / 3)) for k in range(-10, 11)]          # same 1/3-octave bands as SpectrumMeter
FLAG_VOICE, FLAG_PITCH, FLAG_HOLD, FLAG_ALARM, FLAG_BYPASS = 1, 2, 4, 8, 16
OUT_DELAY_FRAMES = 2                                                     # output lags input by 20 ms


class TapReader:
    def __init__(self, path):
        self.path = Path(path)
        self.mm = None
        self.next = None
        self.lost = 0
        self.pid = None

    def _open(self):
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return False
        try:
            size = os.fstat(fd).st_size
            if size < 64:
                return False
            self.mm = mmap.mmap(fd, size, prot=mmap.PROT_READ)
        finally:
            os.close(fd)
        if self.mm[:8] != MAGIC:
            self.mm = None
            return False
        self.slots, self.frame, self.rate, self.pid = (int(v) for v in np.frombuffer(self.mm, np.uint32, 4, 8))
        self.slot_bytes = META.itemsize + 2 * self.frame * 4
        self.count = np.frombuffer(self.mm, np.uint64, 1, 24)
        self.meta = np.ndarray((self.slots,), META, self.mm, 64, (self.slot_bytes,))
        self.inp = np.ndarray((self.slots, self.frame), np.float32, self.mm, 64 + META.itemsize, (self.slot_bytes, 4))
        self.out = np.ndarray((self.slots, self.frame), np.float32, self.mm, 64 + META.itemsize + 4 * self.frame,
                              (self.slot_bytes, 4))
        self.inode = os.stat(self.path).st_ino
        return True

    def alive(self):
        """True while the sukoon_engine.py that owns the tap is running."""
        return self.pid is not None and Path(f'/proc/{self.pid}').exists()

    def poll(self, start_at_now=False):
        """(meta array, input (n, 480), output (n, 480)) for frames written since the last poll. Empty when none.
        A new sukoon_engine.py run (new file) is followed automatically. Frames overwritten before they were read count in
        self.lost."""
        empty = (np.zeros(0, META), np.zeros((0, FRAME), np.float32), np.zeros((0, FRAME), np.float32))
        try:
            if self.mm is not None and os.stat(self.path).st_ino != self.inode:
                self.mm, self.next = None, None                     # sukoon_engine.py restarted: new ring
        except OSError:
            self.mm = None
            return empty
        if self.mm is None and not self._open():
            return empty
        end = int(self.count[0])
        if self.next is None or start_at_now:
            self.next = end
        if end < self.next:                                         # counter restarted
            self.next = end
            return empty
        first = max(self.next, end - self.slots + 8)               # leave a margin to the slot being written
        self.lost += first - self.next
        if end <= first:
            return empty
        idx = np.arange(first, end) % self.slots
        meta, inp, out = self.meta[idx].copy(), self.inp[idx].copy(), self.out[idx].copy()
        after = int(self.count[0])
        ok = np.arange(first, end) >= after - self.slots + 1        # not overwritten while copying
        if not ok.all():
            self.lost += int((~ok).sum())
            meta, inp, out = meta[ok], inp[ok], out[ok]
        self.next = end
        return meta, inp, out


def dbfs(value, power=False):
    return round((10.0 if power else 20.0) * math.log10(max(value, 1e-12 if power else 1e-6)), 1)


_edges = None


def band_powers(frames):
    """Mean-square power per 1/3-octave band of a stretch of audio (1D)."""
    global _edges
    n = len(frames)
    f = np.fft.rfftfreq(n, 1.0 / FS)
    edges = np.searchsorted(f, [fc / 2 ** (1 / 6) for fc in BANDS] + [BANDS[-1] * 2 ** (1 / 6)])
    w = np.hanning(n)
    spec = np.fft.rfft(frames * w)
    p = spec.real ** 2 + spec.imag ** 2
    scale = 2.0 / (n * float(np.sum(w ** 2)))
    return np.array([p[a:b].sum() for a, b in zip(edges[:-1], edges[1:])]) * scale + 1e-12


def bang_measure(inp, out, k):
    """A bang triggered at frame index k of the aligned arrays inp, out ((n, 480) as the tap gives them).

    Peak: loudest input sample from 3 frames before to 4 after (AttenuationMeter's span). Reduction: that peak
    against the loudest output over the same span shifted by the 20 ms delay. Bands: input vs output energy per
    1/3 octave over 8 frames (80 ms) from the frame before the trigger, as SpectrumMeter measures a bang."""
    n = len(inp)
    a, b = max(0, k - 3), min(n, k + 5)
    oa, ob = min(n, a + OUT_DELAY_FRAMES), min(n, b + OUT_DELAY_FRAMES)
    peak_in = float(np.max(np.abs(inp[a:b]))) if b > a else 0.0
    peak_out = float(np.max(np.abs(out[oa:ob]))) if ob > oa else 0.0
    s, e = max(0, k - 1), min(n, k + 7)
    so, eo = min(n, s + OUT_DELAY_FRAMES), min(n, e + OUT_DELAY_FRAMES)
    bands = None
    if e - s == eo - so and e > s:
        pi, po = band_powers(inp[s:e].ravel()), band_powers(out[so:eo].ravel())
        bands = np.round(np.clip(10 * np.log10(pi / po), -60, 60), 1).tolist()
    return {'peak_dbfs': dbfs(peak_in), 'out_peak_dbfs': dbfs(peak_out),
            'reduction_db': round(dbfs(peak_in) - dbfs(peak_out), 1), 'band_reduction_db': bands}
