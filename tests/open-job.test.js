"use strict";

/* A job in the bell, a notification or an alert opens the job itself - its
   own progress view, or its card in Jobs - not the page it is about; Open
   on that card still goes to the page. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const bell = fs.readFileSync("web/js/homestead-update.js", "utf8");
const ops = fs.readFileSync("web/js/operations.js", "utf8");

test("the bell's running and failed jobs open the job", () => {
  assert.match(bell, /const jobRow = op => `<button class="bell-job" onclick="closeNotifications\(\);openJob\(/);
  assert.match(bell, /failedJobs\.slice\(0, 3\)\.map\(op => row\(`openJob\(/);
  assert.doesNotMatch(bell, /openOperation\(\$\{jsq\(op\.href/, "not the page the job is about");
});

test("a job with its own progress view opens there; the rest open on their card", () => {
  const open = ops.slice(ops.indexOf("window.openJob = async id =>"), ops.indexOf("window.jobsDialog = () =>"));
  assert.match(open, /cluster-shutdown[^\n]*clusterShutdown\(\)/);
  assert.match(open, /disk-v2-convert[^\n]*diskV2Watch\(id\)/);
  assert.match(open, /jobsDialog\(\);\s*selectJob\(id\);/);
});

test("Open on a card still goes where the job's work is, failed or not", () => {
  const open = ops.slice(ops.indexOf("window.openOperation ="), ops.indexOf("window.openOperation =") + 2500);
  assert.doesNotMatch(open, /status === "failed"\) return openJob/);
});

test("only a finished job kept back is called a retained record, never a running one", () => {
  assert.match(ops, /selected\.dismissible === false && !operationActive\(selected\)/);
});
