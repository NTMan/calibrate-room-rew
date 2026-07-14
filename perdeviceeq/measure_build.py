# -*- coding: utf-8 -*-
"""The wizard's 'Create profile' step, kept GTK-free so it can be tested.

Ties a finished measure_session (the accepted takes) to fit_peq (the fit)
and a ProfileStore (save + bind): finalize each measured channel with its
own cal, fit a profile, save it as a user profile, bind it to the sink
and return the new id. The window calls build_and_bind on a worker thread
and then switches the main editor to that id so the result is heard at
once. No GTK and no store construction here (the store is injected).
"""
import os

from . import fit_peq


def build_and_bind(session, channels, store, sink_node, name,
                   cal=None, bands=10, f_lo=20.0, f_hi=12000.0,
                   max_boost=6.0):
    """Finalize the measured channels, fit a profile, save and bind it.

    channels maps a capture-channel index to a profile channel key, e.g.
    {0: "FL", 1: "FR"}. cal maps a capture-channel index (int or str) to a
    cal-file path (from the mic profile); a channel absent from cal falls
    back to the session's cfg.cal. Each channel's result is written to the
    session's output dir as result_<key>.json (kept for later
    recompensate/re-fit), fit to flat, and assembled into one profile.
    Returns the new user-profile id, already bound to sink_node."""
    cal = cal or {}
    results = {}
    for ch_index, key in channels.items():
        c = cal.get(ch_index)
        if c is None:
            c = cal.get(str(ch_index))
        out = os.path.join(session.outdir, "result_%s.json" % key)
        results[key] = session.finalize(ch_index, out_path=out, cal=c)
    prof = fit_peq.fit_profiles(results, name=name, bands=bands,
                                f_lo=f_lo, f_hi=f_hi, max_boost=max_boost)
    pid = store.save_user(prof)
    store.set_binding(sink_node, pid)
    return pid
