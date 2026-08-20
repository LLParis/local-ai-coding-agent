# Swift edge qualification task

Objective: make `BackendState.isReady` require the listener PID to match the
owner PID while preserving the existing task, loopback, and health conditions.

- Mutable: `Sources/ContinuityFixture/BackendState.swift`
- Visible context: `Package.swift`, `Sources/`
- Verifier-only context: `Tests/`
- Authoritative command: `swift test`
- Model calls: one
- Automatic retries: zero

Run this on the Mac edge through PC Qwen, then have Devstral verify the accepted
diff. Do not change the test target.
