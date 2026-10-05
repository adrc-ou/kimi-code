"use strict";

// Raise the per-object cap on abort-listener accounting to a floor of our own.
//
// Kimi Code's agent loop re-arms this cap on its own turn signal at the start of every step:
// loopService.ts holds MAX_STEP_SIGNAL_LISTENERS = 64 and calls
// EventEmitter.setMaxListeners(64, turn.controller.signal) inside the per-step gate. There is one
// AbortController per turn and every step is handed that same signal, so the cap is a deliberate
// upstream assertion - "a turn should not have this many cancellable things alive at once" - and
// not a Node default, since Node places no limit on an EventTarget unless one is set.
//
// Kimi also has registrations that release only when the signal fires, so a turn that keeps
// stepping while a compaction is in flight accumulates one live listener per step forever. Before
// the harness stopped bounding a turn by wall clock and by step count, and before the long lane
// became the launched default, no turn ran long enough to reach 64. The warning is the assertion
// working; it is not new damage.
//
// So the cap is not removed here, only floored, and only in the target-bearing form. Silence at
// the old threshold is what makes the line useless as a signal, and the number is still printed
// when it is genuinely passed. --disable-warning would have been one line and no code, and would
// have taken the assertion away entirely rather than re-scaling it.
//
// KIMI_ABORT_LISTENER_FLOOR unset or non-positive leaves Node and Kimi alone, which is why this
// file can sit in NODE_OPTIONS unconditionally: it is inert without the number.

const events = require("node:events");

function readFloor() {
  const raw = process.env.KIMI_ABORT_LISTENER_FLOOR;
  if (raw === undefined || raw === "") return 0;
  const value = Number.parseInt(raw, 10);
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

const floor = readFloor();

// Consumed rather than merely read. Every process the agent spawns from a shell inherits
// NODE_OPTIONS and this variable alike, so deleting it here bounds the change to the process that
// was launched with it and leaves the preload inert for everything else in the container.
if (floor > 0) delete process.env.KIMI_ABORT_LISTENER_FLOOR;

function isEventTarget(value) {
  return (
    value !== null &&
    (typeof value === "object" || typeof value === "function") &&
    typeof value.addEventListener === "function"
  );
}

if (floor > 0) {
  // The namespace export and the static method are one function, so each property is wrapped
  // separately and each ends up with exactly one layer in front of the original.
  for (const holder of [events, events.EventEmitter]) {
    const original = holder.setMaxListeners;
    if (typeof original !== "function") continue;
    Object.defineProperty(holder, "setMaxListeners", {
      configurable: true,
      writable: true,
      value: function floored(n, ...targets) {
        // Only an EventTarget is floored, never an EventEmitter. This container runs other
        // people's Node processes beside the agent - the language server, the MCP servers -
        // and they inherit NODE_OPTIONS, so a blanket floor would rescale their warnings too
        // on the strength of a judgement made about Kimi's turn signal.
        //
        // 0 and Infinity both mean "no limit" to this API, and the no-target form sets a
        // process-wide default. Raising either would invent a limit where none was asked for,
        // so a finite positive cap is lifted, never lowered, and only when every target named
        // in the call is one this file was written for.
        const applicable =
          targets.length > 0 &&
          Number.isFinite(n) &&
          n > 0 &&
          n < floor &&
          targets.every(isEventTarget);
        return original.call(holder, applicable ? floor : n, ...targets);
      },
    });
  }
}
