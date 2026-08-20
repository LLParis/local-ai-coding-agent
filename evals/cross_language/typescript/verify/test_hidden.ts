import assert from "node:assert/strict";
import test from "node:test";

import { isBackendReady } from "../src/backend_state.ts";

test("ready requires the listener to match the owner", () => {
  assert.equal(
    isBackendReady({
      taskState: "Running",
      localAddress: "127.0.0.1",
      listenerPid: 42,
      ownerPid: 42,
      health: "ok",
    }),
    true,
  );
  assert.equal(
    isBackendReady({
      taskState: "Running",
      localAddress: "127.0.0.1",
      listenerPid: 99,
      ownerPid: 42,
      health: "ok",
    }),
    false,
  );
});
