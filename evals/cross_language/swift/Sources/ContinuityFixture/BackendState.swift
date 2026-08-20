public struct BackendState: Sendable {
    public let taskRunning: Bool
    public let loopbackOnly: Bool
    public let listenerPID: Int32
    public let ownerPID: Int32
    public let healthy: Bool

    public init(
        taskRunning: Bool,
        loopbackOnly: Bool,
        listenerPID: Int32,
        ownerPID: Int32,
        healthy: Bool
    ) {
        self.taskRunning = taskRunning
        self.loopbackOnly = loopbackOnly
        self.listenerPID = listenerPID
        self.ownerPID = ownerPID
        self.healthy = healthy
    }

    public var isReady: Bool {
        taskRunning && loopbackOnly && healthy
    }
}
