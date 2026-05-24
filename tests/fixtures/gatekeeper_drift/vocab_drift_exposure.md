# Vocab drift — exposure-class fixture

This doc references the exposure class `agent_public`. The
companion test passes a restricted `exposure_classes` set that
omits `agent_public`, simulating a future code change that drops
the class without updating the docs. The check must emit
`VOCAB_DRIFT_EXPOSURE_CLASS` against that line.
