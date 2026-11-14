"use strict";
// The checks every dialog loop moved onto UI.poll gets (tests/dialog-poll-moves-*.test.js), over
// tests/helpers/dialog-poll-world.js. A loop is described by:
//   name, files, path (its read; `exact` for an exact match), every (ms), host (the element it paints),
//   open(w) (starts it; returns the opener's promise), reopen(w) (default: open), reopenReads (reads
//   the opener itself makes on reopening, on the same path), later (first poll read one interval after
//   opening), running() / done() (answers; no done: it follows until closed), failed (what a failed
//   read paints; null: it shows nothing), firstFailed (the first read's own failure text),
//   failedAt (another element the failure is written to), failedShowsCause (false: a fixed text, not
//   the error), afterFailure ("retry" or "stop"), side(read) (answers for the opener's other reads),
//   background (it follows a dangerous operation and reads on in a hidden tab; decided 12 Nov),
//   readsOnClosed (closing the dialog does not stop it: the cluster shutdown reads on and opens its
//   dialog again when the shutdown moves on, as on dev; its own file tests that instead).
const assert = require("node:assert/strict");
const world = require("./dialog-poll-world");

// It returns [name, check] pairs; each test file calls test() itself, so a case is reported as its file's.
module.exports = function dialogLoopCases(LOOPS) {
  const cases = [];
  const test = (name, check) => cases.push([name, check]);
  for (const loop of LOOPS) {
    const isPoll = r => loop.exact ? r.path === loop.path : r.path.startsWith(loop.path);
    const pollReads = w => w.reads.filter(isPoll);
    const shown = w => (loop.failedAt ? w.fields[loop.failedAt]?.textContent : "") + (w.fields[loop.host]?.innerHTML || "");
    // Reads that are not the poll's are answered at once; the poll's wait for the test unless w.pollAuto answers them.
    async function opened() {
      const w = world(loop.files, loop.extra);
      w.answers.auto = r => isPoll(r) ? w.pollAuto?.(r) : r.answer(loop.side ? loop.side(r) : {});
      // Not awaited unless it reads later: an opener waits for its first read, which the test answers.
      const opening = loop.open(w);
      if (loop.later) await opening;
      await w.clock.settle();
      return w;
    }
    const firstRead = async w => { if (loop.later) await w.clock.advance(loop.every); };
    const failure = () => new Error("connection refused");
    const failedShown = (w, before) => loop.failed ? assert.match(shown(w), loop.failed) : assert.equal(shown(w), before, "a failed read paints nothing");

    test(`${loop.name}: reads ${loop.later ? `${loop.every / 1000} s after opening` : "at once"}, then ${loop.every / 1000} s after each answer`, async () => {
      const w = await opened();
      if (loop.later) {
        assert.equal(pollReads(w).length, 0);
        await w.clock.advance(loop.every - 1);
        assert.equal(pollReads(w).length, 0);
        await w.clock.advance(1);
      }
      assert.equal(pollReads(w).length, 1);
      await w.clock.advance(limit - 1000);
      assert.equal(pollReads(w).length, 1, "no second read beside one still on its way");
      pollReads(w)[0].answer(loop.running()); await w.clock.settle();
      await w.clock.advance(loop.every - 1);
      assert.equal(pollReads(w).length, 1);
      await w.clock.advance(1);
      assert.equal(pollReads(w).length, 2, `the next read is ${loop.every / 1000} s after the answer`);
    });

    test(`${loop.name}: a failed read is ${loop.failed ? "shown" : "not shown"}, and the loop ${loop.afterFailure === "retry" ? "tries again" : "stops"}`, async () => {
      const w = await opened();
      await firstRead(w);
      if (loop.firstFailed) {
        pollReads(w)[0].fail(failure()); await w.clock.settle();
        assert.match(shown(w), loop.firstFailed);
        await w.clock.advance(loop.every);
      }
      const before = shown(w);
      pollReads(w).at(-1).fail(failure()); await w.clock.settle();
      failedShown(w, before);
      const count = pollReads(w).length;
      await w.clock.advance(loop.every);
      assert.equal(pollReads(w).length, count + (loop.afterFailure === "retry" ? 1 : 0));
    });

    // New with the move: a read that never answers no longer holds the dialog for good.
    const limit = Math.max(5 * loop.every, 30000);   // UI.poll's give-up
    test(`${loop.name}: a read with no answer in ${limit / 1000} s is given up, and the loop ${loop.afterFailure === "retry" ? "tries again" : "stops"}`, async () => {
      const w = await opened();
      await firstRead(w);
      if (loop.firstFailed) { pollReads(w)[0].answer(loop.running()); await w.clock.advance(loop.every); }
      const stuck = pollReads(w).at(-1), count = pollReads(w).length, before = shown(w);
      await w.clock.advance(limit - 1);
      assert.equal(stuck.signal.aborted, false, "not before its limit");
      await w.clock.advance(1);
      assert.equal(stuck.signal.aborted, true);
      failedShown(w, before);
      if (loop.failed && loop.failedShowsCause !== false) assert.match(shown(w), new RegExp(`No response in ${limit / 1000} s`));
      await w.clock.advance(loop.every);
      assert.equal(pollReads(w).length, count + (loop.afterFailure === "retry" ? 1 : 0));
    });

    if (loop.done) test(`${loop.name}: a finished job ends the reads`, async () => {
      const w = await opened();
      await firstRead(w);
      pollReads(w)[0].answer(loop.done()); await w.clock.settle();
      await w.clock.advance(10 * loop.every);
      assert.equal(pollReads(w).length, 1);
      assert.equal(w.clock.pending(), 0, "no timer is left behind");
    });

    // Closing the dialog, in three parts, so each says which part an old loop did not do.
    const closedMidRead = async () => {
      const w = await opened();
      await firstRead(w);
      const read = pollReads(w).at(-1), before = shown(w);
      w.close();
      return { w, read, before };
    };
    if (!loop.readsOnClosed) {
    test(`${loop.name}: closing the dialog reads no more`, async () => {
      const { w, read } = await closedMidRead();
      read.answer(loop.running()); await w.clock.settle();
      await w.clock.advance(20 * loop.every);
      assert.equal(pollReads(w).length, 1, "nothing reads after the dialog closed");
    });
    test(`${loop.name}: closing the dialog cancels the read on its way`, async () => {
      const { read } = await closedMidRead();
      assert.equal(read.signal?.aborted, true);
    });
    test(`${loop.name}: closing the dialog drops the answer of the read on its way`, async () => {
      const { w, read, before } = await closedMidRead();
      read.answer(loop.running()); await w.clock.settle();
      assert.equal(shown(w), before, "its answer is not painted");
    });
    }

    test(`${loop.name}: closed and opened again, it runs one loop`, async () => {
      const w = await opened();
      w.pollAuto = r => r.answer(loop.running());
      if (loop.later) await w.clock.advance(loop.every); else pollReads(w)[0].answer(loop.running());
      await w.clock.advance(loop.every);
      w.close();
      const before = pollReads(w).length;
      const opening = (loop.reopen || loop.open)(w);
      if (loop.later) await opening;
      await w.clock.settle();
      await w.clock.advance(4 * loop.every);
      assert.equal(pollReads(w).length - before, (loop.reopenReads || 0) + (loop.later ? 4 : 5), "one read per interval, not two");
    });

    if (loop.background) test(`${loop.name}: a hidden tab goes on reading every ${loop.every / 1000} s, and showing it adds no read`, async () => {
      const w = await opened();
      w.pollAuto = r => r.answer(loop.running());
      if (loop.later) await w.clock.advance(loop.every); else pollReads(w)[0].answer(loop.running());
      await w.clock.settle();
      const before = pollReads(w).length;
      w.document.show(false);
      await w.clock.advance(3 * loop.every);
      assert.equal(pollReads(w).length, before + 3, "a read every interval while the tab is hidden");
      w.document.show(true); await w.clock.settle();
      assert.equal(pollReads(w).length, before + 3, "no extra read when it is shown again");
    });
    else test(`${loop.name}: a hidden tab reads nothing, and reads once when shown`, async () => {
      const w = await opened();
      w.pollAuto = r => r.answer(loop.running());
      if (loop.later) await w.clock.advance(loop.every); else pollReads(w)[0].answer(loop.running());
      await w.clock.settle();
      const before = pollReads(w).length;
      w.document.show(false);
      await w.clock.advance(20 * loop.every);
      assert.equal(pollReads(w).length, before, "no read while the tab is hidden");
      w.document.show(true); await w.clock.settle();
      assert.equal(pollReads(w).length, before + 1, "one read when it is shown again");
    });
  }
  return cases;
};
