# Generates dwarfAlp-test-plan.json, a N.I.N.A. advanced-sequencer test plan for dwarfAlp.
# Run from this folder: python make_test_plan.py
import json, itertools
ids = itertools.count(1)
NS = "NINA.Sequencer"
COLL_ITEM = "System.Collections.ObjectModel.ObservableCollection`1[[NINA.Sequencer.SequenceItem.ISequenceItem, NINA.Sequencer]], System.ObjectModel"
COLL_COND = "System.Collections.ObjectModel.ObservableCollection`1[[NINA.Sequencer.Conditions.ISequenceCondition, NINA.Sequencer]], System.ObjectModel"
COLL_TRIG = "System.Collections.ObjectModel.ObservableCollection`1[[NINA.Sequencer.Trigger.ISequenceTrigger, NINA.Sequencer]], System.ObjectModel"

def container(cls, name, parent, items=(), conditions=(), extra=None):
    cid = str(next(ids))
    c = {"$id": cid, "$type": f"{NS}.Container.{cls}, {NS}",
         "Strategy": {"$type": f"{NS}.Container.ExecutionStrategy.SequentialStrategy, {NS}"},
         "Name": name}
    if extra: c.update(extra)
    c["Conditions"] = {"$id": str(next(ids)), "$type": COLL_COND, "$values": [f(cid) for f in conditions]}
    c["IsExpanded"] = True
    c["Items"] = {"$id": str(next(ids)), "$type": COLL_ITEM, "$values": [f(cid) for f in items]}
    c["Triggers"] = {"$id": str(next(ids)), "$type": COLL_TRIG, "$values": []}
    if parent: c["Parent"] = {"$ref": parent}
    c["ErrorBehavior"] = 0; c["Attempts"] = 1
    return c

def C(cls, name, items=(), conditions=(), extra=None):
    return lambda parent: container(cls, name, parent, items, conditions, extra)

def item(typ, **props):
    def f(parent):
        d = {"$id": str(next(ids)), "$type": f"{NS}.{typ}, {NS}"}
        d.update(props)
        d["Parent"] = {"$ref": parent}; d["ErrorBehavior"] = 0; d["Attempts"] = 1
        return d
    return f

def loop(n):
    def f(parent):
        return {"$id": str(next(ids)), "$type": f"{NS}.Conditions.LoopCondition, {NS}",
                "CompletedIterations": 0, "Iterations": n, "Parent": {"$ref": parent}}
    return f

def coords(rah, ram, ras, dd, dm, dsec, neg=False):
    return {"$id": str(next(ids)), "$type": "NINA.Astrometry.InputCoordinates, NINA.Astrometry",
            "RAHours": rah, "RAMinutes": ram, "RASeconds": ras, "NegativeDec": neg,
            "DecDegrees": dd, "DecMinutes": dm, "DecSeconds": dsec}

def dso(title, target, c, items):
    def extra():
        return {"Target": {"$id": str(next(ids)), "$type": "NINA.Astrometry.InputTarget, NINA.Astrometry",
                           "Expanded": True, "TargetName": target, "PositionAngle": 0.0,
                           "InputCoordinates": c}, "ExposureInfoListExpanded": False}
    return lambda parent: container("DeepSkyObjectContainer", title, parent, items, (), extra())

def msg(text): return item("SequenceItem.Utility.MessageBox", Text=text)
def exposure(seconds):
    return item("SequenceItem.Imaging.TakeExposure", ExposureTime=float(seconds), Gain=-1, Offset=-1,
                Binning={"$id": str(next(ids)), "$type": "NINA.Core.Model.Equipment.BinningMode, NINA.Core", "X": 1, "Y": 1},
                ImageType="LIGHT", ExposureCount=0)
def frames(n, seconds, name):
    return C("SequentialContainer", name, items=[exposure(seconds)], conditions=[loop(n)])
slew = item("SequenceItem.Telescope.SlewScopeToRaDec", Inherited=True)
center = item("SequenceItem.Platesolving.Center", Inherited=True)
def tracking_stopped(): return item("SequenceItem.Telescope.SetTracking", TrackingMode=5)  # Stopped
def wait(s): return item("SequenceItem.Utility.WaitForTimeSpan", Time=float(s))

root_id = str(next(ids))
root = {"$id": root_id, "$type": f"{NS}.Container.SequenceRootContainer, {NS}",
        "Strategy": {"$type": f"{NS}.Container.ExecutionStrategy.SequentialStrategy, {NS}"},
        "Name": "dwarfAlp DWARF 3 test plan",
        "Conditions": {"$id": str(next(ids)), "$type": COLL_COND, "$values": []},
        "IsExpanded": True}
start = C("StartAreaContainer", "Start", items=[
    msg("dwarfAlp test plan. Before you continue: dwarfAlp server running (Prepare calibration after start ticked if you test it), "
        "NINA connected to camera, filter wheel and mount, Options > Equipment > Telescope focal length 150 mm and focal ratio 4.3, "
        "ASTAP with the D50 database, plate solving exposure about 10 s. Keep the dwarfAlp Activity tab in view."),
])
targets = C("TargetAreaContainer", "Targets", items=[
    dso("Test 1 - M31: GOTO and 5 x 10 s", "M31", coords(0, 42, 44.3, 41, 16, 9.0), [
        slew, frames(5, 10, "5 x 10 s"),
        msg("Test 1 done. Activity should show: (DWARF calibrating... if it was the first GOTO), GOTO completed, "
            "5 frames delivered in order, 3856x2180. OK to continue."),
    ]),
    dso("Test 2 - M45: Slew and Center, 5 x 15 s", "M45", coords(3, 47, 24.0, 24, 7, 0.0), [
        center, frames(5, 15, "5 x 15 s"),
        msg("Test 2 done. Activity should show the slew, the session ending, a GOTO, the short plate-solve frame and "
            "maybe a second small GOTO; NINA should report the target centred. OK to continue."),
    ]),
    C("SequentialContainer", "Test 3 - Tracking off", items=[
        tracking_stopped(), wait(5),
        msg("Test 3 done. Activity should show: Tracking stopped by NINA. OK to continue."),
    ]),
    dso("Test 4 - M42: GOTO after tracking off, Stop and resume", "M42", coords(5, 35, 17.3, -5, 23, 28.0, neg=True), [
        slew,
        msg("Test 4: during the next 30 s exposures press Stop in NINA, wait a few seconds and press Start again. "
            "Activity should show the session ending (exposure aborted) and a new session starting."),
        frames(5, 30, "5 x 30 s"),
    ]),
])
end = C("EndAreaContainer", "End", items=[
    tracking_stopped(),
    msg("Test plan finished. Please send the Activity and Technical logs from dwarfAlp."),
])
root["Items"] = {"$id": str(next(ids)), "$type": COLL_ITEM, "$values": [start(root_id), targets(root_id), end(root_id)]}
root["Triggers"] = {"$id": str(next(ids)), "$type": COLL_TRIG, "$values": []}
root["Parent"] = None; root["ErrorBehavior"] = 0; root["Attempts"] = 1
json.dump(root, open("dwarfAlp-test-plan.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print("ok")
