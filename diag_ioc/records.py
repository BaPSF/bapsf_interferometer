"""Record helpers for diag_ioc modules. Each takes softioc's `builder`, so this module never imports softioc."""

# EPICS menuAlarmSevr / menuAlarmStat values, the same as softioc.alarm. Mirrored so that importing diag_ioc
# never loads softioc, which reads the EPICS_* environment when first imported (diag_ioc.host sets it first).
NO_ALARM, MINOR_ALARM, MAJOR_ALARM, INVALID_ALARM = 0, 1, 2, 3
READ_ALARM, SOFT_ALARM, UDF_ALARM = 1, 15, 17

DESC_MAX = 40  # EPICS DESC field size


def _fields(desc, **fields):
	if desc is not None and len(desc) > DESC_MAX:
		raise ValueError(f"DESC {desc!r} is {len(desc)} characters, over {DESC_MAX}")
	return {key: value for key, value in dict(fields, DESC=desc).items() if value is not None}


def waveform(builder, name, nelm, egu=None, desc=None, prec=None, **fields):
	"""Passive float64 WaveformIn of up to `nelm` points with a device timestamp (TSE=-2): set() it with timestamp=.

	set() asserts len(value) <= nelm, so decimate before publishing.
	"""
	return builder.WaveformIn(name, length=nelm, datatype=float,
	                          **_fields(desc, EGU=egu, PREC=prec, TSE=-2, SCAN="Passive", **fields))


def scalar(builder, name, egu=None, desc=None, prec=None, **fields):
	"""Passive aIn with a device timestamp (TSE=-2) that posts every update, even an unchanged value (MDEL=-1)."""
	return builder.aIn(name, **_fields(desc, EGU=egu, PREC=prec, TSE=-2, SCAN="Passive", MDEL=-1, **fields))


def chain(makers):
	"""Records that process in list order, makers[0] the head; returns them in that order.

	Each maker is called as maker(FLNK=<next record>) (the last as maker()), last to first, because
	FLNK must name a record that already exists. Make the head I/O Intr and the rest Passive: a set()
	on the head then processes the whole chain, so publish() sets the head last.
	"""
	records, following = [], None
	for make in reversed(makers):
		following = make() if following is None else make(FLNK=following)
		records.append(following)
	return records[::-1]


def add_group(record, group_pv, mapping):
	"""Put fields of `record` into the PVXS group PV `group_pv` (full name: no prefix is added).

	mapping is {group field: {"+type": ..., "+trigger": ...}}; "+channel" defaults to "VAL". Call once per
	record: a second call adds a second Q:group info entry rather than merging.
	"""
	record.add_info("Q:group", {group_pv: {name: {"+channel": "VAL", **spec} for name, spec in mapping.items()}})


def fit_text(text, length):
	"""`text` cut to fit a string record of `length` bytes including the terminating NUL (set() asserts the fit)."""
	data = text.encode("utf-8")
	return text if len(data) < length else data[:length - 1].decode("utf-8", "ignore")
