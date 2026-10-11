"""diag_ioc test module: <prefix>:SEQ (chain head) -> VALUE -> WAVE, each stamped with the message's ts.

A message is {"seq", "value", "wave", "ts"} arrays; VALUE = value * options["scale"]. A message with a
"fail" key makes analyze() raise, to exercise STAT:ERROR.
"""
import numpy as np

from diag_ioc.module import DiagnosticModule
from diag_ioc.records import chain, scalar, waveform

WAVE_NELM = 16


class DummyModule(DiagnosticModule):
	def create_records(self, builder):
		self.seq, self.value, self.wave = chain([
			lambda **kw: builder.longIn("SEQ", TSE=-2, DESC="message seq", **kw),  # I/O Intr: set() runs the chain
			lambda **kw: scalar(builder, "VALUE", desc="value x scale", **kw),
			lambda **kw: waveform(builder, "WAVE", WAVE_NELM, desc="wave", **kw),
		])

	def analyze(self, variables):
		if "fail" in variables:
			raise ValueError("asked to fail")
		return (int(variables["seq"]), float(variables["value"]) * self.options["scale"],
		        np.asarray(variables["wave"], dtype=float), float(variables["ts"]))

	def publish(self, result):
		seq, value, wave, ts = result
		self.value.set(value, timestamp=ts)
		self.wave.set(wave, timestamp=ts)
		self.seq.set(seq, timestamp=ts)  # last: processing the head publishes the shot
