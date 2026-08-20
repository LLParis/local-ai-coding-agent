import Testing
@testable import ContinuityFixture

@Test func readinessRequiresListenerOwnership() {
    let owned = BackendState(
        taskRunning: true,
        loopbackOnly: true,
        listenerPID: 42,
        ownerPID: 42,
        healthy: true
    )
    let mismatched = BackendState(
        taskRunning: true,
        loopbackOnly: true,
        listenerPID: 99,
        ownerPID: 42,
        healthy: true
    )

    #expect(owned.isReady)
    #expect(!mismatched.isReady)
}
