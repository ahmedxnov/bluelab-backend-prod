"""The attempt lifecycle — this module's published interface to `bluelab.calls`.

Admission (T-1) inserts the attempt, completion (T-2) inserts the transcript and
flips the status, interruption (T-6) voids it and reverses the allowance. The
call code calls in here; it does not touch `models.py`, because that would put
the concealment-bearing tables behind two doors instead of one.
"""
