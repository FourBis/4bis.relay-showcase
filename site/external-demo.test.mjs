import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("./demo.js", import.meta.url), "utf8");
const start = source.indexOf("function initialExternalDemo()");
const end = source.indexOf("// End of the pure external-PR scenario state.");
assert(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const { initialExternalDemo, externalDemoAssessment: assessment, reduceExternalDemo: step } = context;

let state = initialExternalDemo();
assert.equal(state.policy, false);
assert.equal(state.issueClosed, false);
assert.equal(assessment(state), "unregistered");
assert.equal(step(state, "approve"), state, "unregistered work cannot acquire evidence");
state = step(state, "register");
state = step(state, "check");
state = step(state, "approve");
assert.equal(assessment(state), "missing_evidence", "checks and approval do not replace the declaration");
state = step(state, "declare");
assert.equal(assessment(state), "ready");
const previous = state.head;
state = step(state, "new_head");
assert.equal(state.declaration, previous, "previous declarations remain historical");
assert.equal(assessment(state), "missing_evidence", "a new head invalidates readiness");
state = step(step(state, "declare"), "check");
assert.equal(assessment(state), "review_pending", "an old approval does not approve a new head");
state = step(state, "approve");
assert.equal(assessment(state), "ready");
state = step(step(state, "merge_first"), "merge_second");
assert.equal(state.issueClosed, false, "merge does not enable synchronization");
state = step(state, "policy", true);
assert.equal(state.issueClosed, false, "a policy does not replace human resolution");
state = step(state, "complete", true);
assert.equal(state.issueClosed, true);
assert.equal(assessment(state), "merged");
assert.equal(step(state, "new_head").head, state.head, "a merged PR has no new head in the example");
state = step(step(state, "complete", false), "policy", false);
assert.equal(state.issueClosed, true, "policy changes never reopen a closed issue");

state = step(step(initialExternalDemo(), "register"), "policy", true);
state = step(step(state, "complete", true), "merge_first");
assert.equal(state.issueClosed, false, "every linked PR must have a verified merge");
assert.equal(assessment(state), "merged", "Relay does not block GitHub merges for missing evidence");

assert.doesNotMatch(source, /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(/,
  "the static demo has no remote data connection");
const html = readFileSync(new URL("./index.html", import.meta.url), "utf8");
assert.match(html, /connect-src 'none'/);
assert.match(html, /id="tab-external"[^>]*aria-controls="scene-external"/);
assert.match(html, /id="scene-external"[^>]*role="tabpanel"[^>]*aria-labelledby="tab-external"/);
assert.match(html, /id="external-result"[^>]*role="status"/);
assert.doesNotMatch(html, /href="https:\/\/github\.com\/ExampleOrg/,
  "fictional references are labels, not links to a real repository");
console.log("ok - external PR demo preserves SHA evidence, explicit closure, and offline limits");
