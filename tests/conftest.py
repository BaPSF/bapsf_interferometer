import struct
import time

import numpy as np
import pytest
from lab_scopes.lecroy import LeCroyWavedesc
from lab_scopes.lecroy.wavedesc import WAVEDESC_FMT

N_SAMPLES = 64
T0 = 1773173017.25  # a 2026 trigger time; .25 is exact in binary, so logged dt values are exact


def write_trc(path, samples, trigger_time):
	"""A LeCroy .trc file: "#9" block prefix, WAVEDESC, int16 samples."""
	lw = LeCroyWavedesc()
	lw.generate_test_data(len(samples))
	whole = int(trigger_time)
	t = time.gmtime(whole)
	wd = lw.wd._replace(comm_order=1, tt_year=t.tm_year, tt_months=t.tm_mon, tt_days=t.tm_mday,
	                    tt_hours=t.tm_hour, tt_minute=t.tm_min, tt_second=t.tm_sec + (trigger_time - whole))
	payload = struct.pack(WAVEDESC_FMT, *wd) + np.asarray(samples, dtype="<i2").tobytes()
	path.write_bytes(b"#9%09d" % len(payload) + payload)


@pytest.fixture
def make_trc_dir(tmp_path):
	"""make_trc_dir(counters) -> (directory, {(counter, ch): samples}); triggers `spacing` s apart in list order."""
	def make(counters, channels=("C1", "C2", "C3", "C4"), spacing=3.0):
		expected = {}
		for i, counter in enumerate(counters):
			for ch in channels:
				samples = np.random.default_rng([counter, int(ch[1:])]).integers(-2048, 2048, N_SAMPLES, dtype=np.int16)
				expected[counter, ch] = samples
				write_trc(tmp_path / f"{ch}-interf-shot{counter:05d}.trc", samples, T0 + spacing * i)
		return tmp_path, expected
	return make
